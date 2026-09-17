# Cruise Capture

Drives the robot through an ordered list of points while a single recording runs, so one
pass produces continuous footage of several locations.

This is deliberately **not** a port of `robot-live-system`'s tour engine. That engine exists
to narrate: it carries a knowledge base, per-point TTS/MP3 playback, and a repeat count.
None of that applies here, and the two projects stay isolated — no shared module, no shared
`tour-list.json`, no shared runtime state.

## What a cruise does

```text
validate target map + every path without moving
  -> set_switch_map (required)
  -> wait up to 10s for a newer connected heartbeat reporting:
       system.ready + target map/localization/ready + navigation.ready
  -> capture session starts
  -> video_record { start: 0 }          one continuous recording
  -> for each point:
       set_goal { path_name, goal_id, goal_object: null }
       wait for goal_status: done
       marker recorded at the arrival timestamp
       short internal stationary capture window (recording runs only)
  -> video_record { stop: 0 }
  -> recorded file syncs into the media vault
```

## Product priority: cruise is always navigation-only

Cruise arrival is decided by the current point's `goal_status` alone. The app never waits for
`object_status` and never fails a point because object recognition/alignment failed. The
`goal_object` field remains readable only for compatibility with older saved routes and API
payloads; `CruiseService` strips it before dispatch, so legacy data cannot silently restore
robot-owned alignment. `object_status`, when present in a heartbeat, is diagnostic telemetry
only.

Arrival detection guards against the two failure modes the hardware makes easy:

- A heartbeat that explicitly names a different path or point is rejected. A matching path
  and point can report arrival directly; status-only reports must first show a non-terminal
  state (`_require_non_done`) so a previous point's cached result is not reused.
- A lost connection resolves any pending arrival as failed, so a cruise can never hang on
  an arrival that will not be reported.

## Markers, not scene detection

PySceneDetect decides cuts from pixel differences alone. When the robot glides between two
visually similar points there is no hard cut, so those boundaries are not reliably
recoverable from the video. The robot already knows exactly when it arrived, so each
arrival is written to the capture session as a `TimelineMarker` (`{path_name}#{goal_id}`)
timestamped from the start of the recording.

Markers and detected cuts are merged rather than one replacing the other: every marker
guarantees a boundary, and PySceneDetect's cuts subdivide the long stretches between them so
beat-syncing still has somewhere to cut. Each resulting segment inherits its point's label
and carries `from_marker`, so ground truth is distinguishable from a guess.

A marker alone is a single instant, so it can say when a point was reached and never when it
was left — a dwell with a start and no end. The run's `segments` are therefore written to the
sidecar too, carrying both edges of every span plus the point's outcome. Editing cuts along
those spans and tags each stretch `dwell`, `transit`, `failed`, `skipped`, or `unknown`,
which is what lets planning tell a parked shot from a moving one. See `docs/edit_diversity.md`.

## Failure policy

A blocked or unreachable point reports `failed`. The recording **keeps rolling** — stopping
and restarting per failure would fragment the file — and nothing is discarded automatically.

Footage shot while the robot was travelling, or while a point was failing, is still usable
material, so the failure is recorded as a labelled marker (`path1#3 失败`) for a human to
judge rather than as an instruction to cut.

A point that times out (`arrival_timeout_seconds`, default 60s) is treated the same as a
failure. A dropped robot connection aborts the run rather than failing every remaining
point one at a time.

## Route-point capture window

Without narration to pace against, a point would otherwise be left the instant it is
reached. A recording run therefore keeps the base stationary for an internal window sampled
independently around 7.5 seconds (70%–130%). This window is not a camera-target pause and is
not exposed in the request or UI. A `record=false` trial skips it completely.

`CruiseSegment.dwell_seconds` remains an observed output so editing can distinguish stationary
footage from transit. Legacy request keys `dwell_seconds`, `dwell_min_seconds`, and
`dwell_max_seconds` are accepted only for loading compatibility and discarded; they cannot
change execution.

## Automatic camerawork profile

`auto_camerawork` remains off by default. Enabling it on a cruise uses the single profile saved
under **镜头设置**; a cruise is refused when the operator has never saved that profile.

- Anchor and yaw/pitch/zoom limits are absolute poses inside the existing manual-control limits.
- The configured yaw and pitch ranges are each split at their midpoint, producing four non-empty
  integer regions. The first target region is random; each later target region is chosen uniformly
  from the other three. An intervening anchor state does not permit the same region to repeat.
  A sequence such as 1 → 4 → 1 → 4 is valid; 1 → 1 is not.
- Every gimbal leg re-reads the latest available heartbeat pose, falling back per axis to the
  previous target when feedback is absent. A normal target has no hold: after two fresh complete
  yaw/pitch samples are within 2°, or its full travel/velocity estimate has elapsed, the next target
  is selected immediately.
- The anchor is the only target with an explicit hold. `anchor_time_percent` defaults to 20;
  `anchor_dwell_seconds` is the nominal time to stay *after arriving* and defaults to 5 seconds.
  Each complete hold receives internal ±30% jitter. Return travel and pose confirmation happen
  first and never consume that hold; the UI exposes only the nominal value.
- For an anchor hold `A` and share `P`, the planned ordinary-camerawork budget is
  `A × (100 − P) / P`. A leg already in progress finishes before that budget transitions to the
  anchor. Anchor return travel is transition overhead, so real wall-clock share is approximate;
  complete moves and complete holds take priority. Startup never enters halfway through a hold.
  `P=0` never enters an in-recording anchor hold; `P=100` stays at the anchor and renews complete
  holds without repeatedly commanding the same move.
- Zoom never changes while the base is travelling. If an anchor phase overlaps a parked dwell,
  one zoom sequence may run inside the already-started anchor hold, and the complete pose returns
  to the anchor before the next navigation goal can be dispatched. Ordinary yaw/pitch camerawork
  can continue while parked without zoom.
- Safety returns before recording and after recording has stopped are outside the percentage
  schedule. An ambiguous Stop never triggers a final camera command that could contaminate a
  recording which may still be active.

The retired directional selector and parked scan are preserved only in
[`docs/archive/camerawork-directional-v1.md`](archive/camerawork-directional-v1.md) and
[`docs/archive/parked-gimbal-scan-v1.md`](archive/parked-gimbal-scan-v1.md). They are not fallback
paths and old saved settings cannot reactivate them. The standalone HTML console mirrors
the active yaw/pitch scheduler but deliberately omits zoom. Its **模拟到达点位** action changes
only the simulated base state from travelling to parked: the same schedule and previous target
history remain active. Only **停止并回锚点** ends that simulation and commands the anchor.

## Saved routes

`goal_id` is a row index into a CSV on the robot, and the protocol exposes no way to list
those rows — `get_path_list` returns path *names* only. The full command set is
`get_map_list`, `get_path_list`, `set_switch_map`, `set_goal`, `gimbal_control`,
`video_record`, `take_photo`; nothing enumerates points.

So ids are typed by hand, discovered by running, and then **saved**. A saved route is the
only durable record of which ids are real on a given path.

`CruiseRouteStore` keeps up to 10 routes in `data/cruise-routes.json`, most recently used
first, evicting the least recently used. Saving under an existing name replaces it.
Timestamps exist to order that list, not to reason about staleness. Unreadable or
schema-mismatched entries are dropped on load rather than crashing the store.

## What validation can and cannot check

| Field | Checked when? | How |
|---|---|---|
| `map_name` | On load, before anything moves | Against `get_map_list` |
| `path_name` | On load, before anything moves | Against `get_path_list` |
| `goal_id` | Inline, at that point's dispatch | `set_goal` reply `goal_check` |

`goal_id` cannot be pre-flighted. The only way to test an id is `set_goal`, and per the
contract the robot begins moving as soon as the check passes — so verifying a *valid* id
means driving to it. There is no abort either: `stop_motion` is not part of the hardware
protocol. Ids are therefore checked at dispatch, where a rejected one costs no movement,
marks the point failed, and lets the run continue.

The standalone validation action is read-only: an unreachable robot or an empty map/path
response is shown as an uncertainty warning rather than claiming that saved data is wrong.
Starting is deliberately stricter. Both `POST /cruise/start` and
`POST /cruise/routes/{id}/start` use the same fail-closed preflight, so any error, warning, or
unchecked map/path result blocks with 409 before recording or movement. A valid `map_name` is
required operationally; the model keeps it optional only so older mapless saved routes can be
loaded, assigned a map, and repaired in the UI.

`robot_switch_map: "true"` proves only that the map file exists. It never rewrites the app's
actual-map state. After that acknowledgement, startup waits at most 10 seconds for a newer
heartbeat that confirms the target map, localization mode, successful positioning, healthy
hardware, and ready navigation. A rejection, disconnect, failed localization, incomplete
heartbeat, or timeout stops startup without opening a capture session or dispatching a goal.

## Recording is one shared resource

The robot records into one file at a time and there is one active capture session. Both a
cruise and a manual capture want to own that, so the backend enforces mutual exclusion:

- Starting a cruise while a manual capture is live raises, rather than adopting the
  operator's session, sending a second `video_record start`, and closing a recording it
  did not open.
- Starting or stopping a manual capture while a cruise runs is refused; cancel the cruise
  instead.

Both guards live in the services and routes, not only in the UI, because the HTTP and
WebSocket surfaces can be driven independently.

## UI

Two sections split by job: **镜头设置** shows the robot-reported current map and provides a
manual map/path browser; it is not a cruise configuration source. **拍摄** owns the explicit
target map, ordered paths/points, recording, and the rest of the cruise request — the former
采集 section was folded into it so there is a single place that starts a recording. If the
current and target maps differ, startup switches automatically and confirms the target through
the heartbeat, so the operator never has to preselect the same map on both pages.

There is deliberately no manual "send one goal" panel. A one-point cruise with recording
off does the same job and reports arrival as 已到达 / 失败, which a fire-and-forget
`set_goal` cannot — so that is the 试跑 button on each row instead.

The **拍摄** section holds:

- **原地采集** — one status strip for the whole app: recording or not, elapsed time, and
  whether it came from a cruise or a manual start. The manual buttons grey out during a
  cruise, since a cruise always records and needs sole ownership of the recording.
- **巡游清单** — map and path come from dropdowns fed by the robot; `goal_id` is typed,
  because nothing can enumerate it. Each row has 试跑 (single point, no recording) for
  discovering whether an id is real. Rows reorder and delete.
- **巡游设置** — an automatic-camerawork switch that links back to its profile under 镜头设置.
  Point capture-window timing and the per-point 60-second arrival timeout are internal.
- **已保存清单** — save, load, validate, delete, or start a saved 清单 directly. Validation
  issues render per row. Named 清单 rather than 路线 because 路线 collides with 路径 in
  Chinese, and 清单 is the term robot-live-system already uses.
- **运行状态** — live per-point progress from the `CRUISE_*` events, plus the markers
  recorded for the run.
- **画面匹配备注** — one compact recording-level map such as
  `点位1：产品展示区；点位2：仓库`. It is saved beside that recording and may later align
  narration sentences to point footage. It is never sent to the LLM and never becomes spoken
  copy. Bare notes without an explicit point marker are stored but deliberately not guessed.

## API

| Route | Purpose |
|---|---|
| `POST /api/cruise/start` | Fail-closed validate/activate the request map, then start; 409 with issues on validation failure |
| `POST /api/cruise/cancel` | Stop the run, stop recording, mark remaining points skipped |
| `GET /api/cruise` | Current or most recent `CruiseRun` |
| `GET /api/cruise/routes` | Saved routes, most recently used first |
| `POST /api/cruise/routes` | Save under a name, replacing any route with that name |
| `DELETE /api/cruise/routes/{id}` | Remove a saved route |
| `POST /api/cruise/routes/{id}/validate` | Check map and paths without moving the robot |
| `POST /api/cruise/routes/{id}/start` | Validate, then start; 409 with the issues on error |

WebSocket commands `CRUISE_START` / `CRUISE_CANCEL` mirror those routes. Progress is
broadcast as `CRUISE_STARTED`, `CRUISE_POINT_DISPATCHED`, `CRUISE_POINT_ARRIVED`,
`CRUISE_POINT_DEPARTED`, `CRUISE_POINT_FAILED`, and one of `CRUISE_FINISHED` /
`CRUISE_CANCELED` / `CRUISE_FAILED`.
