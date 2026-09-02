# Cruise Capture

Drives the robot through an ordered list of points while a single recording runs, so one
pass produces continuous footage of several locations.

This is deliberately **not** a port of `robot-live-system`'s tour engine. That engine exists
to narrate: it carries a knowledge base, per-point TTS/MP3 playback, and a repeat count.
None of that applies here, and the two projects stay isolated — no shared module, no shared
`tour-list.json`, no shared runtime state.

## What a cruise does

```text
switch_map (optional)
  -> capture session starts
  -> video_record { start: 0 }          one continuous recording
  -> for each point:
       set_goal { path_name, goal_id, goal_object? }
       wait for goal_status: done
       marker recorded at the arrival timestamp
       dwell (random, optionally with a gimbal scan)
  -> video_record { stop: 0 }
  -> recorded file syncs into the media vault
```

## Alignment is skipped by omission, not by a special mode

`CruisePoint.goal_object` defaults to `None`. A point with no `goal_object` is
navigation-only: the robot is never asked to aim at an object, so it never stalls waiting
for recognition, and arrival is decided on `goal_status` alone. Setting `goal_object` on a
point opts that point back into waiting for `object_status`.

Arrival detection guards against the two failure modes the hardware makes easy:

- A heartbeat carrying the **previous** goal's settled status is rejected until a non-done
  heartbeat proves the robot actually started moving (`_require_non_done`).
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

## Dwell

Without narration to pace against, a point would otherwise be left the instant it is
reached. Each arrival holds for a random `dwell_min_seconds`–`dwell_max_seconds`
(default 5–10s) so every point yields a usable shot.

## Optional gimbal scan

Off by default (`gimbal_scan.enabled = false`). When on, the camera pans slowly away from
its current angle and back again **while the robot is parked** — never while the base is
moving, which would add shake.

- The dwell is floored at the scan's worst-case duration, so a scan is never cut mid-return
  and the camera always ends on the angle it started from.
- Progress is confirmed by polling the heartbeat's gimbal yaw. A robot that does not report
  gimbal yaw falls back to the time budget for the same move.
- If the camera's position is unknown, the scan is skipped rather than risking a pan that
  cannot be returned to origin.

Navigation and gimbal are independent commands, so enabling the scan does not change how a
cruise drives, and manual camera control stays usable throughout a run.

## Automatic camerawork profile

`auto_camerawork` remains off by default. Enabling it on a cruise uses the single profile saved
under **镜头设置**; a cruise is refused when the operator has never saved that profile.

- Anchor and yaw/pitch/zoom limits are absolute poses inside the existing manual-control limits.
- Before recording, at every point, and when the cruise ends, the complete pose returns to the
  anchor. The old random temporary hold at an arbitrary pose no longer exists.
- While the base travels, the planner repeatedly chooses adaptive wander (50%), ping-pong (30%),
  or one slow return-to-anchor action (20%). Reaching the anchor immediately returns control to
  the planner; it does not turn the remainder of the transit into a static hold.
- From a left-side pose, adaptive wander chooses a broad rightward sweep 80% of the time or a
  small further-left move 20% of the time; the right-side case is mirrored. Every leg re-reads
  the physical heartbeat pose and all targets stay inside the configured limits.
- Zoom remains fixed during transit. After arrival, yaw and pitch first return to the anchor;
  zoom then moves on that safe composition and the complete pose settles visibly at the anchor.
- The separately configured parked scan is ignored when automatic camerawork is enabled, so two
  planners never issue competing gimbal commands.

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

Validation errors block `POST /cruise/routes/{id}/start` with 409. An unreachable robot or
an empty map/path list produces a **warning**, not an error — being unable to check is not
evidence the route is wrong.

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

Two sections split by job: **硬件与镜头** owns the connection, the map switch, and manual
gimbal aiming. **拍摄** owns recording and everything to do with points — the former 采集
section was folded into it so there is a single place that starts a recording.

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
- **巡游设置** — recording, dwell range, the off-by-default gimbal scan, and an
  automatic-camerawork switch that links back to its profile under 镜头设置. The internal
  per-point arrival timeout is fixed at 60 seconds and is not exposed in the UI. The scan's
  worst-case duration is shown so the dwell floor is not a surprise.
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
| `POST /api/cruise/start` | Start a run from a `CruiseRequest` |
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
