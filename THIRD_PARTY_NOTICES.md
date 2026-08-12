# Third-party notices

The Windows installer bundles open-source software and assets required for offline editing.
This notice is informational; each component remains subject to its own license.

## FFmpeg

The application bundles a pinned Windows build of FFmpeg from BtbN/FFmpeg-Builds with
`libass`, `libfreetype`, and `libfontconfig` enabled for subtitle rendering. The selected build
is distributed under GNU GPL v3. FFmpeg source and build scripts are available from:

- https://github.com/FFmpeg/FFmpeg
- https://github.com/BtbN/FFmpeg-Builds

## Fonts

The application bundles Noto Sans SC and Noto Serif SC under the SIL Open Font License 1.1,
and Smiley Sans under its bundled open-font license. The complete license texts are included
inside the application beside the font assets under `automated_video_editing_backend/assets/font_licenses`.

## Application dependencies

Electron, Vue, Vite, FastAPI, Uvicorn, PyInstaller, OpenCV, PyAV, PySceneDetect, NumPy,
librosa, numba, llvmlite, and their transitive dependencies retain their respective licenses.
Their package metadata is included in the packaged runtime where required.
