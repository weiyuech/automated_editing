# Third-party notices

The Windows installer bundles open-source software and assets required for offline editing.
This notice is informational; each component remains subject to its own license.

## FFmpeg

The application bundles the immutable BtbN Windows snapshot
`FFmpeg n8.1.2-50-g1a748fe2cd` (`autobuild-2026-08-31-13-27`) with `libass`, `libfreetype`,
and `libfontconfig` enabled for subtitle rendering. Its archive is verified against SHA-256
`273abb45f3f9f76c303e35ff39f5bb6c23c163ae65f6244a32b7d4a7f6cf0616`. The selected build is
distributed under GNU GPL v3. FFmpeg source and build scripts are available from:

- https://github.com/FFmpeg/FFmpeg
- https://github.com/BtbN/FFmpeg-Builds

## Fonts

The application bundles Noto Sans SC and Noto Serif SC under the SIL Open Font License 1.1,
and Smiley Sans under its bundled open-font license. The complete license texts are included
inside the application beside the font assets under `automated_video_editing_backend/assets/font_licenses`.

## Application dependencies

Electron, Vue, Vite, FastAPI, Uvicorn, Pydantic, HTTPX, websockets, PyInstaller,
and their transitive dependencies retain their respective licenses. Their package metadata
is included in the packaged runtime where required. Retired scene-detection libraries
are not bundled. Music excerpt analysis uses librosa (ISC), NumPy and SciPy (BSD),
Numba (BSD), llvmlite (BSD), soundfile (BSD), and soxr (LGPL-2.1-or-later).
Bundled libsndfile and libsoxr retain their LGPL terms; package license texts accompany
their distribution metadata. This notice does not replace those licenses or the
corresponding-source obligations for redistributed GPL/LGPL binaries.

## Interface appearance

The lavender/pink/violet/blue gradients and shimmer are implemented locally in CSS.
The reference screenshots used to discuss colors are not included as application assets;
no third-party theme package or remote theme CDN was added. This statement concerns the
new theme, not a blanket clearance of unrelated artwork, trademarks, or user media.

## Local narration matching

BAAI/bge-small-zh-v1.5 is included under the MIT license, using Xenova's INT8 ONNX conversion
pinned at revision `75c43b0`. Model, tokenizer, and license downloads are verified by SHA-256 in
`scripts/prepare_assets.py`. Its license is bundled in `assets/semantic/bge-small-zh-v1.5/`.
NumPy (BSD), ONNX Runtime (MIT), tokenizers (Apache-2.0), and their dependencies retain their
respective license terms. The model only compares text; it does not analyze images or beats.
