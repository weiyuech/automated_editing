> 版本说明：本文保留改版前的设计记录。当前已改为固定镜头、树形精确拼接和显式旁白映射；旧自动选片、节拍与语义匹配规则不再执行。当前操作与实现以 [根 README](../README.md) 和 [改版说明](controlled_capture_concat_plan.md) 为准。

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
