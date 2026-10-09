#!/usr/bin/env python3
"""
c2x-tools mirror — downloads pinned upstream builds of FFmpeg, repackages them into small per-platform archives and publishes them as
GitHub releases of this repo, together with a manifest (tools.json) that the
c2x Studio desktop app reads.

    python3 scripts/mirror.py prepare --ffmpeg-branch 9.0
    python3 scripts/mirror.py publish

Release layout (one release per tool version, plus one "manifest" release):
    ffmpeg-<version>   ffmpeg-windows-x64.zip, ffmpeg-linux-x64.tar.gz, …
    manifest           tools.json  (overwritten on every publish)

Sources (all redistributable; FFmpeg builds are GPL — see NOTICE files):
    Windows / Linux FFmpeg : BtbN/FFmpeg-Builds  (gpl variant, release branch)
    macOS FFmpeg           : ffmpeg.martin-riedl.de (signed static builds)

Only FFmpeg is published here. yt-dlp / Deno are deliberately NOT mirrored
(legal caution: download tools are only offered to admins / trusted testers,
and the app fetches them from their official upstream releases).
"""
import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

REPO = os.environ.get("GITHUB_REPOSITORY", "gunterhschubert/c2x-tools")
DIST = Path("dist")
UA = {"User-Agent": "c2x-tools-mirror"}

BTBN_LATEST = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest"
RIEDL = "https://ffmpeg.martin-riedl.de/redirect/latest/macos/{arch}/release/{file}"


# ── helpers ──────────────────────────────────────────────────────────────────

def log(*a):
    print(*a, flush=True)


def fetch(url, dest=None):
    """Downloads url → dest (or returns bytes). Returns (data|path, final_url)."""
    req = urllib.request.Request(url, headers=UA)
    tok = os.environ.get("GITHUB_TOKEN")
    if tok and url.startswith("https://api.github.com/"):
        req.add_header("Authorization", f"Bearer {tok}")
    with urllib.request.urlopen(req, timeout=600) as r:
        final = r.geturl()
        if dest is None:
            return r.read(), final
        with open(dest, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
        return dest, final


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def gh(*args, check=True, capture=False):
    r = subprocess.run(["gh", *args], check=False, text=True,
                       capture_output=capture)
    if check and r.returncode != 0:
        sys.exit(f"gh {' '.join(args)} failed: {r.stderr if capture else ''}")
    return r


def release_exists(tag):
    if shutil.which("gh") is None:   # local dry runs without the gh CLI
        return False
    return gh("release", "view", tag, "--repo", REPO, check=False,
              capture=True).returncode == 0


def make_zip(dest, files):
    """files: list of (source_path, name_in_archive). Executables get 0755."""
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for src, name in files:
            info = zipfile.ZipInfo.from_file(src, name)
            # Fixed timestamp → identical bytes on re-runs (the app detects
            # updates by SHA-256, so a re-packaged archive must not differ).
            info.date_time = (2020, 1, 1, 0, 0, 0)
            info.external_attr = (0o100755 if not name.endswith(".txt")
                                  else 0o100644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(src, "rb") as f:
                z.writestr(info, f.read(), compresslevel=9)


def make_targz(dest, files):
    # gzip header mtime=0 and fixed tar mtimes → reproducible bytes.
    with open(dest, "wb") as raw, \
            gzip.GzipFile(filename="", mode="wb", fileobj=raw,
                          compresslevel=9, mtime=0) as gz, \
            tarfile.open(fileobj=gz, mode="w") as t:
        for src, name in files:
            ti = t.gettarinfo(src, name)
            ti.mode = 0o644 if name.endswith(".txt") else 0o755
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            ti.mtime = 1577836800  # 2020-01-01
            with open(src, "rb") as f:
                t.addfile(ti, f)


def asset(key, path, archive, files, version):
    return {
        "key": key, "file": path.name, "archive": archive,
        "files": files, "version": version,
        "sha256": sha256(path), "size": path.stat().st_size,
    }


def notice(tool, version, builder, source, scripts):
    return (
        f"{tool} {version}\n\n"
        f"Built by: {builder}\n"
        f"License: GNU General Public License v3 (FFmpeg built with GPL "
        f"components such as x264 and x265).\n"
        f"Corresponding source code: {source}\n"
        f"Build scripts / configuration: {scripts}\n\n"
        f"Redistributed unmodified by WauwTec UG for c2x Studio. c2x Studio "
        f"runs this program as a separate process; it is not linked into "
        f"c2x Studio.\n"
    )


# ── FFmpeg ───────────────────────────────────────────────────────────────────

def ffmpeg_version_of(binary):
    out = subprocess.run([binary, "-hide_banner", "-version"], text=True,
                         capture_output=True).stdout
    m = re.search(r"ffmpeg version (\S+)", out)
    return m.group(1) if m else "unknown"


def clean_version(v):
    # "n9.0.2-3-gabcdef-20261007" → "9.0.2" ; "9.0.2" → "9.0.2"
    m = re.match(r"n?(\d+\.\d+(?:\.\d+)?)", v)
    return m.group(1) if m else v


def prepare_ffmpeg(branch, work):
    out = DIST / "ffmpeg"
    out.mkdir(parents=True, exist_ok=True)
    assets = []

    # Windows x64 + Linux x64/arm64 from BtbN (release branch "n<branch>").
    # linux-x64 first: it is the one we can execute here to read the exact
    # version string, which the other NOTICE files then reuse.
    btbn = {
        "linux-x64":   f"ffmpeg-n{branch}-latest-linux64-gpl-{branch}.tar.xz",
        "linux-arm64": f"ffmpeg-n{branch}-latest-linuxarm64-gpl-{branch}.tar.xz",
        "windows-x64": f"ffmpeg-n{branch}-latest-win64-gpl-{branch}.zip",
    }
    sums_raw, _ = fetch(f"{BTBN_LATEST}/checksums.sha256")
    sums = {l.split()[-1].lstrip("*"): l.split()[0]
            for l in sums_raw.decode().splitlines() if l.strip()}
    linux_version = None
    for key, name in btbn.items():
        log(f"▶ BtbN {name}")
        src = work / name
        fetch(f"{BTBN_LATEST}/{name}", src)
        want = sums.get(name)
        if not want or want != sha256(src):
            sys.exit(f"checksum mismatch for {name}")
        ex = work / f"x-{key}"
        ex.mkdir()
        if name.endswith(".zip"):
            with zipfile.ZipFile(src) as z:
                z.extractall(ex)
        else:
            subprocess.run(["tar", "-xJf", str(src), "-C", str(ex)], check=True)
        root = next(ex.iterdir())
        exe = ".exe" if key.startswith("windows") else ""
        lic = root / "LICENSE.txt"
        if key == "linux-x64":
            os.chmod(root / "bin" / "ffmpeg", 0o755)
            linux_version = ffmpeg_version_of(str(root / "bin" / "ffmpeg"))
        ver = clean_version(linux_version or branch)
        nt = work / f"NOTICE-{key}.txt"
        nt.write_text(notice("FFmpeg", linux_version or f"n{branch}",
                             "BtbN/FFmpeg-Builds (gpl)",
                             f"https://ffmpeg.org/releases/ffmpeg-{ver}.tar.xz",
                             "https://github.com/BtbN/FFmpeg-Builds"))
        files = [(root / "bin" / f"ffmpeg{exe}", f"ffmpeg{exe}"),
                 (root / "bin" / f"ffprobe{exe}", f"ffprobe{exe}"),
                 (nt, "NOTICE.txt")]
        if lic.exists():
            files.append((lic, "LICENSE.txt"))
        if key.startswith("windows"):
            dest = out / f"ffmpeg-{key}.zip"
            make_zip(dest, files)
            arch = "zip"
        else:
            dest = out / f"ffmpeg-{key}.tar.gz"
            make_targz(dest, files)
            arch = "tar.gz"
        assets.append(asset(key, dest, arch,
                            [f"ffmpeg{exe}", f"ffprobe{exe}"],
                            linux_version or f"n{branch}"))

    # macOS arm64 + x64 from Martin Riedl (signed static release builds).
    mac_version = None
    for key, arch in (("macos-arm64", "arm64"), ("macos-x64", "amd64")):
        parts = []
        for prog in ("ffmpeg", "ffprobe"):
            z = work / f"{key}-{prog}.zip"
            _, final = fetch(RIEDL.format(arch=arch, file=f"{prog}.zip"), z)
            want, _ = fetch(final + ".sha256")
            if want.decode().split()[0] != sha256(z):
                sys.exit(f"checksum mismatch for {final}")
            m = re.search(r"/\d+_([^/]+)/", final)
            mac_version = m.group(1) if m else mac_version
            ex = work / f"x-{key}-{prog}"
            with zipfile.ZipFile(z) as zz:
                zz.extractall(ex)
            parts.append((next(p for p in ex.rglob(prog) if p.is_file()), prog))
        log(f"▶ macOS {arch}: {mac_version}")
        nt = work / f"NOTICE-{key}.txt"
        nt.write_text(notice("FFmpeg", mac_version, "ffmpeg.martin-riedl.de",
                             f"https://ffmpeg.org/releases/ffmpeg-{mac_version}.tar.xz",
                             "https://gitlab.com/martinr92/ffmpeg"))
        dest = out / f"ffmpeg-{key}.zip"
        make_zip(dest, parts + [(nt, "NOTICE.txt")])
        assets.append(asset(key, dest, "zip", ["ffmpeg", "ffprobe"],
                            mac_version))

    win_linux = clean_version(linux_version or branch)
    if not (mac_version or "").startswith(branch + ".") and mac_version != branch:
        sys.exit(f"macOS build {mac_version} does not belong to branch {branch} "
                 f"— the latest macOS release build is from another FFmpeg "
                 f"branch. Use --ffmpeg-branch {'.'.join((mac_version or '?').split('.')[:2])} "
                 f"or wait for a matching macOS build.")
    version = win_linux if win_linux == mac_version else f"{win_linux}+mac{mac_version}"
    return {"tag": f"ffmpeg-{version}", "version": version,
            "license": "GPL-3.0-or-later", "assets": assets}


# ── commands ─────────────────────────────────────────────────────────────────

def cmd_prepare(a):
    DIST.mkdir(exist_ok=True)
    plan = {}
    tools = [t.strip() for t in a.tools.split(",") if t.strip()]
    if any(t != "ffmpeg" for t in tools):
        sys.exit("Only ffmpeg is mirrored in this public repository.")
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        if "ffmpeg" in tools:
            plan["ffmpeg"] = prepare_ffmpeg(a.ffmpeg_branch, work)
    for name, p in list(plan.items()):
        if release_exists(p["tag"]) and not a.force:
            log(f"= {p['tag']} already published — skipping")
            del plan[name]
    (DIST / "plan.json").write_text(json.dumps(plan, indent=2))
    log(json.dumps({k: v["tag"] for k, v in plan.items()}, indent=2))


def cmd_publish(a):
    plan = json.loads((DIST / "plan.json").read_text())
    folder = {"ffmpeg": "ffmpeg"}

    # Current manifest (if any) → merge.
    manifest = {"schema": 1, "repo": REPO, "tools": {}}
    if release_exists("manifest"):
        with tempfile.TemporaryDirectory() as td:
            r = gh("release", "download", "manifest", "--repo", REPO,
                   "--pattern", "tools.json", "--dir", td, check=False,
                   capture=True)
            f = Path(td) / "tools.json"
            if r.returncode != 0 or not f.exists():
                # Never publish a manifest that would drop the other tools.
                sys.exit(f"could not read the current tools.json: {r.stderr}")
            manifest = json.loads(f.read_text())

    for name, p in plan.items():
        files = [str(DIST / folder[name] / x["file"]) for x in p["assets"]]
        notes = (f"{name} {p['version']} for c2x Studio — license "
                 f"{p['license']}. Checksums: see tools.json in the "
                 f"'manifest' release. Each FFmpeg archive contains a "
                 f"NOTICE.txt with the source-code links.")
        if release_exists(p["tag"]):
            gh("release", "upload", p["tag"], *files, "--clobber",
               "--repo", REPO)
        else:
            gh("release", "create", p["tag"], *files, "--repo", REPO,
               "--title", f"{name} {p['version']}", "--notes", notes,
               "--latest=false")
        base = f"https://github.com/{REPO}/releases/download/{p['tag']}"
        manifest["tools"][name] = {
            "version": p["version"], "license": p["license"],
            "release": p["tag"],
            "assets": {x["key"]: {
                "url": f"{base}/{x['file']}", "sha256": x["sha256"],
                "size": x["size"], "archive": x["archive"],
                "files": x["files"], "version": x["version"],
            } for x in p["assets"]},
        }

    if not plan:
        log("Nothing new to publish.")
        return
    import datetime
    manifest["updated"] = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    mf = DIST / "tools.json"
    mf.write_text(json.dumps(manifest, indent=2))
    if not release_exists("manifest"):
        gh("release", "create", "manifest", str(mf), "--repo", REPO,
           "--title", "Manifest (tools.json)", "--latest=false", "--notes",
           "Read by the c2x Studio app. Always the current tool versions.")
    else:
        gh("release", "upload", "manifest", str(mf), "--clobber",
           "--repo", REPO)
    log(mf.read_text())


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--tools", default="ffmpeg")
    p.add_argument("--ffmpeg-branch", default="9.0")
    p.add_argument("--force", action="store_true")
    sub.add_parser("publish")
    a = ap.parse_args()
    {"prepare": cmd_prepare, "publish": cmd_publish}[a.cmd](a)


if __name__ == "__main__":
    main()
