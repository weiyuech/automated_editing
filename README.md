# Automated Video Editing

A desktop operator console for robot-assisted filming and automated video editing.

This project is intentionally isolated from `/Users/user/robot-live-system`. All source, scripts, caches, logs, previews, media, and exports live under this folder.

## Architecture

- `frontend/`: Electron + Vue desktop UI with a vertical operator-console layout.
- `backend/`: Python FastAPI backend for robot control, capture sessions, media analysis, edit planning, and rendering.
- `scripts/`: root-safe scripts. Scripts refuse to write outside this project root.
- `data/`, `.cache/`, `logs/`, `exports/`, `previews/`: generated/runtime folders.

## Why Electron + Python

Electron gives the app a polished local desktop shell: native file pickers, video preview UI, process management, packaged installs, and future hardware control panels. Python owns the heavy lifting: robot adapters, video analysis, timeline planning, and FFmpeg rendering.

## Development

Backend:

```bash
cd /Users/user/Desktop/automated_video_editing
python3 -m venv .venv
source .venv/bin/activate
pip install -e 'backend[dev,beat]'
python scripts/run_backend.py
```

Frontend:

```bash
cd /Users/user/Desktop/automated_video_editing/frontend
/Users/user/.cache/codex-runtimes/codex-primary-runtime/dependencies/bin/pnpm install
/Users/user/.cache/codex-runtimes/codex-primary-runtime/dependencies/bin/pnpm run dev
```

The Electron main process generates a random bridge token and starts the Python backend with that token. The token is not shown in the UI or logs.

## Basic Workflow

1. Open the app with the frontend command above. Electron starts the Python backend automatically.
2. In Media Library, import local videos/music or paste a direct media URL, such as an `.mp4`, `.mov`, `.webm`, `.mp3`, or `.wav` URL. Streaming pages such as YouTube/TikTok/Bilibili need a later `yt-dlp` connector.
3. In Edit Studio, choose a music file, keep `Mute source video noise` enabled, keep `Cut to music beats` enabled, and create the edit job.
4. Render Queue tracks progress. Finished MP4 exports are written under `/Users/user/Desktop/automated_video_editing/exports`.

The Ping button is only a local connection check: it sends a WebSocket `PING` and expects `PONG`. It does not start robot motion, capture, or rendering.

## Stable Editing Stack

Beat detection is installed through the `backend[beat]` extra. It pins `librosa`, `numba`, and `llvmlite` to versions with prebuilt Python 3.12 macOS x86_64 wheels, avoiding local LLVM source builds. The backend sets `NUMBA_CACHE_DIR` to `.cache/numba` so compiled beat-analysis functions stay inside the app folder.


- FFmpeg: trimming, transitions, audio mixing, encoding, export.
- PySceneDetect: scene boundary detection.
- librosa: music beat and onset detection.
- OpenCV: optional frame-level inspection and thumbnail/proxy helpers.

The initial app is designed to work with mock robot control first. Real hardware can be added by implementing the `RobotAdapter` interface.

## Media Vault

Media is organized by role instead of treated as one flat pile. Downloads are raw source media or music, exports are rendered results, and previews/cache are cleanup candidates. Edit Studio only uses explicitly selected raw clips, so exported MP4s do not get accidentally folded back into later renders. See `docs/media_vault.md` for the design rules.

## Settings, LLM, And Timed TTS

Provider credentials are owned by the backend `SettingsService` and stored locally in `data/settings.local.json`. API routes return masked status only, and blank secret fields in the UI keep the existing local value. Provider changes apply immediately; restart is only needed after code changes.

The timed voiceover path uses Volcengine's fast sync TTS endpoint with `with_timestamp=1`. It writes the generated audio and matching word-timing JSON under `data/tts/`, then registers the audio as a voiceover asset. Render jobs keep background music and voiceover separate so the voice can sit above lowered music instead of replacing it.
