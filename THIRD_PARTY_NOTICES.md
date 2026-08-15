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

## BAAI BGE semantic model

The application bundles the INT8 ONNX conversion of `BAAI/bge-small-zh-v1.5` from the pinned
`Xenova/bge-small-zh-v1.5` revision `75c43b0`. The base model and FlagEmbedding project are
released under the MIT License. The complete license text is bundled beside the model under
`automated_video_editing_backend/assets/semantic/bge-small-zh-v1.5`.

- https://huggingface.co/BAAI/bge-small-zh-v1.5
- https://huggingface.co/Xenova/bge-small-zh-v1.5
- https://github.com/FlagOpen/FlagEmbedding

## Application dependencies

Electron, Vue, Vite, FastAPI, Uvicorn, PyInstaller, OpenCV, PyAV, PySceneDetect, NumPy,
librosa, numba, llvmlite, ONNX Runtime, tokenizers, and their transitive dependencies retain
their respective licenses.
Their package metadata is included in the packaged runtime where required.
