# Licence audit — for commercial distribution to customers

September 21, 2026 release note: the detailed audit below is historical, not a clearance
for 0.1.12. PyAV/OpenCV/scene detection are now excluded; librosa and its audio dependencies
are included again. The full Windows package still ships GPL FFmpeg. Its pinned version
and current dependencies are documented in `THIRD_PARTY_NOTICES.md`. The new interface
theme uses local CSS only; the color-reference screenshots are not redistributed.

Scope: everything the packaged Windows application would put on a customer's machine. Findings
come from inspecting the actual binaries — configure strings, exported symbols, bundled shared
libraries — not from reading package metadata. That distinction turned out to be the whole point;
see "Why metadata was not enough" below.

Date of audit: 2026-08-10. Versions as resolved in `backend/.venv` and `frontend/node_modules`.

**This is an engineering audit, not legal advice.** The patent section in particular needs a
lawyer.

---

## Summary

| Severity | Component | Issue |
|---|---|---|
| **Blocking** | `av` (PyAV) 18.0.0 | Wheel bundles **libx264 + libx265** → GPL, and it is **linked in-process** |
| **Blocking** | vendored `ffmpeg` (evermeet 9.0) | GPL build (x264/x265). Separate process, so lower risk |
| Attribution | FFmpeg core, libsndfile, soxr, LAME, FriBidi, libiconv | LGPL — fine, needs notices + source offer |
| Attribution | libgcc / libgfortran / libstdc++ | GPL-3 **with** GCC Runtime Library Exception — fine |
| Attribution | certifi, tqdm | MPL-2.0 — file-level copyleft, fine |
| Clean | opencv-python-headless | Verified: no GPL, uses openh264 |
| Clean | Electron 30.5.1 | Verified: no GPL in bundled FFmpeg |
| Clean | 135 npm packages | All MIT / ISC / BSD / Apache / CC0 / CC-BY-4.0 |
| Clean | 3 bundled fonts | SIL OFL 1.1 — commercial bundling explicitly permitted |

---

## Blocking finding 1 — PyAV ships GPL, and links it into your process

`av` declares `License: BSD-3-Clause`. That describes **PyAV's own binding code**, which is
genuinely BSD. It does not describe the prebuilt FFmpeg and ~18 other libraries packed into the
wheel alongside it.

Verified contents of the wheels:

**macOS** (`av/.dylibs/`) and **Windows** (`av.libs/`) both contain:

```
libx264.165.dylib      /  libx264-165-….dll       (2.2 MB)   GPL
libx265.216.dylib      /  libx265-….dll          (12.7 MB)   GPL
```

FFmpeg cannot be compiled with libx264 unless `--enable-gpl` is set, so the bundled FFmpeg
libraries are themselves GPL, not LGPL. Confirmed independently at runtime — `libx264` and
`libx265` are present as working **encoders** in `av.codecs_available`.

**Why this is the more serious of the two GPL findings.** The vendored `ffmpeg.exe` is invoked as
a separate process over a generic command line, which is ordinarily treated as aggregation — your
application stays proprietary. `import av`, by contrast, loads GPL FFmpeg **into your own
process** and links against it. The FSF's position is that linking creates a derivative work,
which would place your application under the GPL. That position is contested, but it is a
materially weaker position to defend than the subprocess case.

Everything else in the PyAV wheel is fine: SVT-AV1, dav1d, opus, vpx, webp, sharpyuv (BSD),
opencore-amr (Apache-2.0), LAME and libiconv (LGPL), and the MinGW runtime (GPL-3 with the
Runtime Library Exception).

### Remedies

1. **Build PyAV from source against an LGPL FFmpeg.** `av-18.0.0.tar.gz` is published on PyPI, so
   this is supported. Needs FFmpeg development libraries at build time, which CI can supply.
   Keeps all current behaviour and is the smaller change.
2. **Drop PyAV; read frames through the vendored CLI binary.** Removes the class of problem
   entirely, but is a real refactor and must not reintroduce the NaN-timestamp bug that drove this
   project off OpenCV's scene-detection backend in the first place.

## Blocking finding 2 — the vendored FFmpeg is a GPL build

`backend/vendor/ffmpeg/darwin-x86_64/ffmpeg` (evermeet 9.0) is built `--enable-gpl` with libx264
and libx265. This was added for libass and is straightforward to replace.

**Remedy:** BtbN publishes LGPL builds, verified to contain everything the subtitle feature needs
and nothing GPL:

```
enabled:  libass libfreetype libharfbuzz libfribidi fontconfig
          h264_nvenc (ffnvcodec), h264_qsv (libvpl), h264_amf, libopenh264
absent:   --enable-gpl, libx264, libx265, nonfree
```

The cost is losing x264 as the encoder. `render.py` currently hardcodes `-c:v libx264`, so it
needs an encoder ladder chosen by a **real test encode** — `ffmpeg -encoders` lists `h264_nvenc`
on machines with no NVIDIA hardware.

---

## Verified clean

### opencv-python-headless 5.0.0.93

- **macOS**: reports `FFMPEG: NO` in `cv2.getBuildInformation()` — it uses the system AVFoundation
  framework and bundles no FFmpeg at all.
- **Windows**: bundles `opencv_videoio_ffmpeg500_64.dll` (30 MB). Its FFmpeg configure line is
  `--enable-libaom --enable-libopenh264 --enable-libvpx --enable-static --enable-w32threads …`
  with **no** `--enable-gpl`. The symbols `ff_libx264_encoder`, `x264_encoder_open` and
  `x265_api_get` are all absent.

Notably, OpenCV ships exactly the LGPL + openh264 combination proposed as the remedy above — which
is reassuring evidence that the combination is viable in production.

### Electron 30.5.1

`libffmpeg.dylib` (2.4 MB) contains no `x264`/`x265` symbols and no `--enable-gpl`. It is
Chromium's LGPL FFmpeg with decoders only. Standard for every Electron application.

### Frontend

135 unique packages across the pnpm tree: 114 MIT, 10 ISC, 4 BSD-3, 3 BSD-2, 2 Apache-2.0,
1 (MIT OR CC0-1.0), 1 CC-BY-4.0. No copyleft, no unknowns.

### Fonts

Noto Sans SC, Noto Serif SC, Smiley Sans — all SIL OFL 1.1, which explicitly permits bundling and
sale. Reserved Font Names were checked before any modification; see `docs/subtitles.md`.

---

## Requires notices, not code changes

These are all fine to ship, but belong in an attribution / third-party-licences screen, and LGPL
components need a written offer for their source.

| Component | Licence | Arrives via |
|---|---|---|
| FFmpeg (LGPL build) | LGPL-2.1+ | vendored binary, OpenCV |
| libsndfile (+ LAME) | LGPL-2.1 | `soundfile` ← librosa |
| soxr | LGPL-2.1+ | librosa |
| FriBidi, libiconv, LAME | LGPL-2.1+ | FFmpeg builds |
| libgcc, libgfortran, libquadmath, libstdc++ | GPL-3 **with** GCC Runtime Library Exception | scipy, PyAV Windows |
| certifi, tqdm | MPL-2.0 | transitive |
| libomp | Apache-2.0 with LLVM exception | scikit-learn |

The GCC Runtime Library Exception is the relevant one for the GPL-3 entries: it is an additional
permission under section 7 that explicitly allows proprietary distribution. Confirmed in scipy's
own `LICENSE.txt`.

---

## Patents — independent of every licence decision above

H.264/AVC is patent-pooled (Via LA, formerly MPEG LA). **Software licensing says nothing about
patents**, so this applies equally to the GPL, LGPL and openh264 routes.

Distributing software that encodes H.264 makes you a supplier of an "AVC Product". Historically
there is a royalty-free tier below 100,000 units per year, but a signed licence may still be
required. Two specifics worth raising with counsel:

- **Cisco's OpenH264 royalty coverage does not transfer to a statically linked build.** It applies
  only when an application downloads Cisco's own prebuilt binary module at runtime. OpenCV's DLL
  has libopenh264 compiled in, so it does not inherit that coverage.
- **Hardware encoders are a cleaner position.** When the encode runs on the customer's NVENC/QSV/AMF
  silicon, the vendor has already licensed the patents, and you have a much better argument that
  you are not supplying an encoder at all.

Electron also ships H.264 *decoders*, as every Chromium-based application does.

---

## Why metadata was not enough

Standard licence scanners — `pip-licenses`, most SCA tools, and whatever a customer's procurement
team runs — read the `License` field in package metadata. For the package at the centre of this
report they would all report:

```
av    18.0.0    BSD-3-Clause    ✓ clean
```

The declaration is not wrong. Python packaging simply has no field in which to declare the
licences of bundled native libraries, so a wheel can be accurately labelled BSD while containing
15 MB of GPL binaries. Any future audit has to inspect the artefacts, not the manifest.

**Recommended:** add a CI check that scans shipped binaries for `libx264`/`libx265`/`--enable-gpl`
symbols and fails the build. That is the only form of this check that keeps working when a
dependency is upgraded.

---

## Incidental finding

Both `opencv-python` **and** `opencv-python-headless` (5.0.0.93) are installed. The non-headless
build pulls in GUI dependencies and is redundant for a headless backend — it is most likely
dragged in by `scenedetect[opencv]`. Removing it shrinks the installer and the audit surface.
Not a licence problem; both are Apache-2.0.
