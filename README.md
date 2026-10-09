# c2x-tools

FFmpeg builds that **c2x Studio** (by WauwTec UG) downloads on the desktop
(macOS, Windows, Linux) — no package manager, no admin rights.

| Tool | Source of the binaries | License |
|---|---|---|
| FFmpeg + ffprobe (Windows, Linux) | [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds), `gpl` variant | GPL v3 |
| FFmpeg + ffprobe (macOS) | [ffmpeg.martin-riedl.de](https://ffmpeg.martin-riedl.de) (signed static builds) | GPL v3 |

The binaries are redistributed **unmodified**; they are only repackaged
(ffmpeg + ffprobe in one archive per platform). Every FFmpeg archive contains a
`NOTICE.txt` with the exact version, the link to the corresponding FFmpeg
source code and to the build scripts used by the builder.

c2x Studio starts these programs as separate processes; they are not linked
into c2x Studio.

## How it works

* `manifest` release → `tools.json`: current version, download URL, SHA-256 and
  size of every archive. The app reads
  `https://github.com/gunterhschubert/c2x-tools/releases/download/manifest/tools.json`.
* One release per FFmpeg version, e.g. `ffmpeg-9.0.2`.
* Old releases stay available, so older app versions keep working.

## Updating

* Actions → *Mirror FFmpeg* → *Run workflow*
  (choose the FFmpeg branch, e.g. `9.0`). The workflow checks the builds on
  Linux, macOS and Windows (encoders, signature, c2x's hidden-argument syntax)
  and only publishes if everything passes. Recommended: every 3–6 months, or
  right away for security fixes — after testing the new version in the app.


## Source code requests

FFmpeg source: https://ffmpeg.org/releases/ (version in `NOTICE.txt`).
For any further source-code request concerning the binaries in this
repository, contact WauwTec UG via https://c2x.app.
