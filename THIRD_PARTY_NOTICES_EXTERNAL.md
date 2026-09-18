# Third-party notices — external media tools edition

This edition does not include the standalone FFmpeg or FFprobe executables and does not include
PyAV or the FFmpeg libraries carried by PyAV binary wheels.  The operator supplies compatible
media tools and accepts their licence separately.  The application checks for FFmpeg, FFprobe,
the libass subtitle filter, and the libx264 encoder before it starts.

This notice is informational; each bundled component remains subject to its own licence.  It is
an engineering distribution audit, not legal advice and not a patent-licence opinion.

## Components retained in the application

- Electron and its Chromium/Node.js runtime, under their included BSD/MIT/LGPL-compatible
  notices.  Electron's Chromium media decoder is not the separately supplied FFmpeg tool.
- Vue, Vite, FastAPI, Uvicorn, Pydantic, HTTPX, websockets, PyInstaller, and their
  transitive dependencies, under their included licence terms.

## Fonts

Noto Sans SC, Noto Serif SC, and Smiley Sans are included under the SIL Open Font License 1.1.
Their complete licence texts are included beside the font assets under
`automated_video_editing_backend/assets/font_licenses`.

## Operator-supplied prerequisites

FFmpeg and FFprobe are not distributed in this installer.  FFmpeg's own project explains that
its licence depends on how the chosen binary was configured, and that enabling GPL components
changes the resulting FFmpeg binary to GPL.  The operator is responsible for choosing a build,
reviewing its licence and patent position, and complying with those terms.

- https://ffmpeg.org/legal.html
- https://ffmpeg.org/download.html

## Local narration matching

BAAI/bge-small-zh-v1.5 is included under the MIT license, using Xenova's INT8 ONNX conversion
pinned at revision `75c43b0`. Model, tokenizer, and license downloads are verified by SHA-256 in
`scripts/prepare_assets.py`. Its license is bundled in `assets/semantic/bge-small-zh-v1.5/`.
NumPy (BSD), ONNX Runtime (MIT), tokenizers (Apache-2.0), and their dependencies retain their
respective license terms. The model only compares text; it does not analyze images or beats.
