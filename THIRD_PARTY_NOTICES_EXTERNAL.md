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
- Vue, Vite, FastAPI, Uvicorn, PyInstaller, NumPy, OpenCV, PySceneDetect, librosa, numba,
  llvmlite, ONNX Runtime, tokenizers, and their transitive dependencies, under their included
  MIT, ISC, BSD, Apache, MPL, LGPL, and runtime-exception terms.
- OpenCV's Windows video-I/O component uses an LGPL-compatible FFmpeg build without x264/x265
  GPL symbols, as verified by the release binary audit.

## Fonts

Noto Sans SC, Noto Serif SC, and Smiley Sans are included under the SIL Open Font License 1.1.
Their complete licence texts are included beside the font assets under
`automated_video_editing_backend/assets/font_licenses`.

## BAAI BGE semantic model

The application includes the pinned INT8 ONNX conversion of `BAAI/bge-small-zh-v1.5` from
`Xenova/bge-small-zh-v1.5` revision `75c43b0`.  The base model and FlagEmbedding project are
released under the MIT License.  The licence text is included beside the model.

- https://huggingface.co/BAAI/bge-small-zh-v1.5
- https://huggingface.co/Xenova/bge-small-zh-v1.5
- https://github.com/FlagOpen/FlagEmbedding

## Operator-supplied prerequisites

FFmpeg and FFprobe are not distributed in this installer.  FFmpeg's own project explains that
its licence depends on how the chosen binary was configured, and that enabling GPL components
changes the resulting FFmpeg binary to GPL.  The operator is responsible for choosing a build,
reviewing its licence and patent position, and complying with those terms.

- https://ffmpeg.org/legal.html
- https://ffmpeg.org/download.html
