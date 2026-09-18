import os
from pathlib import Path

from PyInstaller.utils.hooks import copy_metadata


BACKEND_ROOT = Path(SPECPATH).resolve()
if BACKEND_ROOT.is_file():
    BACKEND_ROOT = BACKEND_ROOT.parent
PROJECT_ROOT = BACKEND_ROOT.parent
EXTERNAL_MEDIA_TOOLS = os.environ.get("AVE_EXTERNAL_MEDIA_TOOLS") == "1"

datas = []
binaries = []
hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]

for distribution in (
    "fastapi",
    "pydantic",
    "uvicorn",
    "python-multipart",
):
    datas += copy_metadata(distribution)

datas += [
    (
        str(BACKEND_ROOT / "src" / "automated_video_editing_backend" / "assets"),
        "automated_video_editing_backend/assets",
    ),
]
if not EXTERNAL_MEDIA_TOOLS:
    binaries += [
        (
            str(BACKEND_ROOT / "vendor" / "ffmpeg" / "win64" / "ffmpeg.exe"),
            "vendor/ffmpeg/win64",
        ),
        (
            str(BACKEND_ROOT / "vendor" / "ffmpeg" / "win64" / "ffprobe.exe"),
            "vendor/ffmpeg/win64",
        ),
    ]

a = Analysis(
    [str(BACKEND_ROOT / "src" / "automated_video_editing_backend" / "main.py")],
    pathex=[str(BACKEND_ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "matplotlib", "tkinter", "av", "cv2", "librosa", "numba", "llvmlite", "scenedetect"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="automated-video-editing-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    # Electron Builder copies this complete onedir bundle to resources/backend. Keeping the
    # support DLLs beside the executable avoids extracting a very large ML/media stack on each
    # launch and makes startup considerably faster than PyInstaller's onefile mode.
    name="backend-dist",
)
