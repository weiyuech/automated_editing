# Automated Video Editing — external media tools source delivery

Version: 0.1.7

This directory contains the application source, tests, build manifests, authored interface
assets, and third-party notices for the external media tools edition.  It contains no version
control database, commit history, local settings, credentials, dependency caches, downloaded
models or fonts, compiled backend, installer, user media, logs, or exports.

The robot camerawork and arrival logic are described in `docs/cruise_capture.md`.
The standalone robot console is `tools/robot-control-console.html`; its regression checks
run with `node --test tools/tests/robot-control-console.test.mjs`.

## Prerequisites

- Windows 10/11 x64
- Python 3.12
- Node.js 22 and pnpm 11
- 64-bit FFmpeg and FFprobe available in one folder, with the `ass`/`subtitles` filter and the
  `libx264` encoder

The application does not download FFmpeg or accept its licence for the operator.  On first
launch, the external-media-tools installer checks the prerequisites and asks the operator to
select the tools folder when it is not on `PATH`.

## Recreate fetched assets

From the delivery root:

```powershell
python scripts/prepare_assets.py --fonts-only
python scripts/prepare_assets.py --semantic-only
```

These commands fetch pinned, checksum-verified fonts and the pinned semantic model described in
`THIRD_PARTY_NOTICES.md`.  These two source-preparation commands do not fetch FFmpeg.

## Build the external-media-tools Windows installer

Install the backend without the optional `pyav` extra, prepare the retained assets, freeze with
`AVE_EXTERNAL_MEDIA_TOOLS=1`, and run the external Electron Builder target:

```powershell
python -m pip install --upgrade pip pyinstaller
python -m pip install -e "backend[beat,assets,dev]"
python scripts/prepare_assets.py --fonts-only
python scripts/prepare_assets.py --semantic-only
$env:AVE_EXTERNAL_MEDIA_TOOLS = "1"
python -m PyInstaller --noconfirm --clean --distpath frontend/build-external --workpath .cache/pyinstaller-external backend/windows_backend.spec
Set-Location frontend
pnpm install --frozen-lockfile
pnpm run dist:win:external
```

Review `THIRD_PARTY_NOTICES.md` before distribution.  Licence compliance and codec-patent review
remain the distributor's responsibility.
