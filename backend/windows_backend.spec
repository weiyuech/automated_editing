from pathlib import Path

from PyInstaller.utils.hooks import collect_all, copy_metadata


BACKEND_ROOT = Path(SPECPATH).resolve()
if BACKEND_ROOT.is_file():
    BACKEND_ROOT = BACKEND_ROOT.parent
PROJECT_ROOT = BACKEND_ROOT.parent

datas = []
binaries = []
hiddenimports = [
    "automated_video_editing_backend.services.scene_detect_worker",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]

# These libraries discover codecs, compiled extensions or analysis backends dynamically. The
# explicit collection makes the Windows artifact independent of whichever modules PyInstaller
# happened to observe while importing main.py on the CI runner.
for package in (
    "av",
    "cv2",
    "scenedetect",
    "librosa",
    "numba",
    "llvmlite",
    "soundfile",
    "sklearn",
):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

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
binaries += [
    (
        str(BACKEND_ROOT / "vendor" / "ffmpeg" / "win64" / "ffmpeg.exe"),
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
    excludes=["pytest", "matplotlib", "tkinter"],
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
