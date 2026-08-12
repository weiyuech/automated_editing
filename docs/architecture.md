# Architecture

The app is split into a desktop UI and a local Python backend.

```text
Electron/Vue UI
  | HTTP + WebSocket on 127.0.0.1 with per-launch token
Python FastAPI Backend
  |-- RobotService -> HardwareRobotAdapter over robot-side WebSocket
  |-- CaptureService -> capture sessions and shot markers
  |-- CruiseService -> multi-point filming runs over RobotService + CaptureService
  |-- MediaService -> imported raw clips and metadata
  |-- AnalysisService -> scene detection and beat detection
  |-- EditPlanner -> timeline JSON (internal format)
  |-- RenderService -> FFmpeg command construction and export jobs
  |-- JobService -> queue, progress, status, logs
```

The backend is the source of truth for robot state, capture state, edit jobs, render progress, and logs. The UI is a control surface.

## Cruise Capture

`CruiseService` drives the robot through an ordered list of points while one continuous
recording runs, marking each arrival so edit planning knows where every point's footage
starts. See `docs/cruise_capture.md`.

## Planned Review And Publish Layer

The app should later include a client-facing review workspace for in-app video playback, human approval, timestamp/span editing, and Seedance 2 effects before publishing. See `docs/review_publish_seedance2_requirements.md`.

## Security Boundary

The backend binds to `127.0.0.1` only. Electron generates `APP_BRIDGE_TOKEN` per launch and passes it to the backend process. HTTP requests use `x-bridge-token`; WebSocket connects use `?token=...`. Missing or invalid tokens are rejected before command handling.
