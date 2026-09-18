# Third-party notices

The Windows installer bundles open-source software and assets required for offline editing.
This notice is informational; each component remains subject to its own license.

## FFmpeg

The application bundles the immutable BtbN Windows snapshot
`FFmpeg n8.1.2-34-g9b6c8969e0` (`autobuild-2026-08-11-13-11`) with `libass`, `libfreetype`,
and `libfontconfig` enabled for subtitle rendering. Its archive is verified against SHA-256
`05eedc113542be39af5d0f78f0b1093bafb89c98cecf25b77e8644670293107f`. The selected build is
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
is included in the packaged runtime where required. Retired automatic video-analysis
and beat-detection libraries are no longer bundled.

## Local narration matching

BAAI/bge-small-zh-v1.5 is included under the MIT license, using Xenova's INT8 ONNX conversion
pinned at revision `75c43b0`. Model, tokenizer, and license downloads are verified by SHA-256 in
`scripts/prepare_assets.py`. Its license is bundled in `assets/semantic/bge-small-zh-v1.5/`.
NumPy (BSD), ONNX Runtime (MIT), tokenizers (Apache-2.0), and their dependencies retain their
respective license terms. The model only compares text; it does not analyze images or beats.
