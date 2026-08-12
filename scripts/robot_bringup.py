#!/usr/bin/env python3
"""Staged hardware bring-up for cruise capture.

Drives the backend services directly against a real robot: no Electron, no bridge token.
Stages are deliberately separate because two of them move the robot.

    probe    read-only. Connect, inspect heartbeats, list maps and paths. Moves nothing.
    goal     one set_goal plus arrival detection. MOVES THE ROBOT. No recording.
    cruise   a full cruise run. MOVES THE ROBOT AND RECORDS.

Examples:
    python scripts/robot_bringup.py probe  --url ws://10.73.2.199:8765
    python scripts/robot_bringup.py goal   --url ws://10.73.2.199:8765 --path path1 --goal 1
    python scripts/robot_bringup.py cruise --url ws://10.73.2.199:8765 --path path1 --goals 1,2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from contextlib import suppress
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

from automated_video_editing_backend.core.events import EventHub  # noqa: E402
from automated_video_editing_backend.core.models import (  # noqa: E402
    CruisePoint,
    CruiseRequest,
    GimbalScanConfig,
    RobotGoalCommand,
)
from automated_video_editing_backend.services.capture import CaptureService  # noqa: E402
from automated_video_editing_backend.services.cruise import CruiseService  # noqa: E402
from automated_video_editing_backend.services.robot import RobotService  # noqa: E402

HEARTBEAT_KEYS = ("system", "map", "naviagtion", "navigation", "task", "gimbal")


def confirm(message: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    print(f"\n!! {message}")
    return input("   Type 'go' to continue: ").strip().lower() == "go"


async def connect(url: str) -> tuple[RobotService, EventHub]:
    events = EventHub()
    robot = RobotService(events, websocket_url=url)
    print(f"connecting to {url} ...")
    await robot.connect()
    state = await robot.status()
    print(f"  connected={state.connected} status={state.connection_status} error={state.error}")
    return robot, events


async def stage_probe(args: argparse.Namespace) -> int:
    robot, _ = await connect(args.url)
    adapter = robot.adapter

    raw: list[str] = []
    original = adapter._handle_message

    async def tee(message):
        raw.append(message if isinstance(message, str) else message.decode("utf-8", "replace"))
        await original(message)

    adapter._handle_message = tee

    state = await robot.status()
    if not state.connected:
        print("\nFAILED to connect. Check the IP, the port, and that the robot is powered.")
        return 1

    print(f"\nlistening {args.seconds}s for heartbeats (nothing is being commanded) ...")
    await asyncio.sleep(args.seconds)

    heartbeats = []
    for message in raw:
        with suppress(json.JSONDecodeError):
            payload = json.loads(message)
            if isinstance(payload, dict) and any(key in payload for key in HEARTBEAT_KEYS):
                heartbeats.append(payload)

    print(f"\n--- heartbeat: {len(heartbeats)} in {args.seconds}s ---")
    if not heartbeats:
        print("  NONE. The cruise loop depends on heartbeats to detect arrival; it cannot")
        print("  work until these appear. Everything below is unverified.")
        return 1

    sample = heartbeats[-1]
    print(f"  top-level keys: {sorted(sample)}")
    nav_key = "naviagtion" if "naviagtion" in sample else "navigation" if "navigation" in sample else None
    print(f"  navigation key: {nav_key or 'MISSING'}")

    task = sample.get("task") or {}
    print(f"  task.goal_status  : {task.get('goal_status', 'MISSING')}")
    print(f"  task.object_status: {task.get('object_status', 'MISSING')}")

    gimbal = sample.get("gimbal") or {}
    yaw = gimbal.get("yaw")
    print(f"  gimbal.yaw        : {yaw if yaw is not None else 'MISSING'}")
    if yaw is None:
        print("    -> the optional gimbal scan cannot confirm the camera returned to centre.")
        print("       It will fall back to a time budget, or skip if the angle is unknown.")

    print("\n  latest heartbeat:")
    print("   ", json.dumps(sample, ensure_ascii=False)[:600])

    state = await robot.status()
    print("\n--- parsed into RobotState ---")
    for field in ("battery", "map_name", "map_status", "navigation_status",
                  "goal_status", "object_status", "path_file", "goal_id", "yaw", "recording"):
        print(f"  {field:18} = {getattr(state, field)}")

    print("\n--- maps ---")
    try:
        maps = await robot.map_list()
        print(f"  {maps}")
    except Exception as exc:
        print(f"  FAILED: {exc}")
        return 1

    target_map = args.map or state.map_name or (maps[0] if maps else None)
    if target_map:
        print(f"\n--- paths on '{target_map}' ---")
        try:
            print(f"  {await robot.path_list(target_map)}")
        except Exception as exc:
            print(f"  FAILED: {exc}")

    print("\nprobe OK. Nothing was moved.")
    await robot.disconnect()
    return 0


async def stage_goal(args: argparse.Namespace) -> int:
    if not confirm(
        f"This will DRIVE THE ROBOT to {args.path}#{args.goal}. Clear the area first.",
        args.yes,
    ):
        print("aborted.")
        return 1

    robot, _ = await connect(args.url)
    if not (await robot.status()).connected:
        return 1

    command = RobotGoalCommand(
        path_name=args.path,
        goal_id=args.goal,
        goal_object=args.goal_object,
    )
    print(f"\nset_goal {command.model_dump()}")
    reply = await robot.set_goal(command)
    print(f"  reply: {reply}")

    if str(reply.get("goal_check", "true")).lower() == "false":
        print("\n  goal_check=false: that path/goal_id does not exist. Robot did not move.")
        print("  This is the inline id check working; pick a different goal_id.")
        await robot.disconnect()
        return 1

    print(f"\nwaiting up to {args.timeout}s for arrival ...")
    started = time.monotonic()
    try:
        outcome = await robot.wait_for_arrival(args.timeout)
    except TimeoutError:
        print(f"  TIMEOUT after {args.timeout}s. The robot never reported done/failed.")
        print("  Re-run 'probe' and check task.goal_status actually transitions.")
        await robot.disconnect()
        return 1

    print(f"  arrival: {outcome} after {time.monotonic() - started:.1f}s")
    state = await robot.status()
    print(f"  goal_status={state.goal_status} object_status={state.object_status} yaw={state.yaw}")
    await robot.disconnect()
    return 0 if outcome == "done" else 1


async def stage_cruise(args: argparse.Namespace) -> int:
    goals = [int(value) for value in args.goals.split(",") if value.strip()]
    if not confirm(
        f"This will DRIVE THE ROBOT through {args.path} goals {goals} AND RECORD. "
        "Clear the area first.",
        args.yes,
    ):
        print("aborted.")
        return 1

    robot, events = await connect(args.url)
    if not (await robot.status()).connected:
        return 1

    capture = CaptureService(events)
    cruise = CruiseService(events, robot, capture)

    async def watch():
        async for event in events.subscribe():
            if event["type"].startswith("CRUISE"):
                data = event["data"]
                segment = data.get("segment")
                if segment:
                    print(f"  [{event['type']}] #{segment['goal_id']} "
                          f"status={segment['status']} error={segment.get('error')}")
                else:
                    print(f"  [{event['type']}] status={data.get('status')}")

    watcher = asyncio.create_task(watch())

    request = CruiseRequest(
        title="bring-up cruise",
        map_name=args.map,
        points=[CruisePoint(path_name=args.path, goal_id=goal) for goal in goals],
        record=not args.no_record,
        dwell_min_seconds=args.dwell_min,
        dwell_max_seconds=args.dwell_max,
        gimbal_scan=GimbalScanConfig(enabled=args.scan),
    )
    print(f"\nstarting cruise: {len(goals)} points, record={request.record}, "
          f"dwell={args.dwell_min}-{args.dwell_max}s, scan={args.scan}")
    print("  (Ctrl-C cancels cleanly and stops the recording)\n")

    run = await cruise.start(request)
    try:
        await cruise._task
    except KeyboardInterrupt:
        print("\ninterrupted, cancelling ...")
        await cruise.cancel()

    watcher.cancel()
    with suppress(asyncio.CancelledError):
        await watcher

    print(f"\n--- run {run.status} ---")
    for segment in run.segments:
        print(f"  #{segment.goal_id:<3} {segment.status:<10} "
              f"arrived={segment.arrived_at_seconds} dwell={segment.dwell_seconds} "
              f"scanned={segment.scanned} error={segment.error}")
    print(f"\n  markers      : {[(m.label, round(m.timestamp, 1)) for m in run.markers]}")
    print(f"  usable spans : {[(s.goal_id, round(s.start, 1), round(s.end, 1)) for s in run.usable_spans]}")
    print(f"  discard spans: {[(s.goal_id, round(s.start, 1), round(s.end, 1)) for s in run.discard_spans]}")
    print(f"  media_url    : {run.media_url}")
    if run.error:
        print(f"  error        : {run.error}")

    await robot.disconnect()
    return 0 if run.status == "succeeded" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stage", choices=["probe", "goal", "cruise"])
    parser.add_argument("--url", required=True, help="ws://IP:PORT of the robot")
    parser.add_argument("--map", default=None)
    parser.add_argument("--path", default=None)
    parser.add_argument("--goal", type=int, default=1)
    parser.add_argument("--goals", default="1,2")
    parser.add_argument("--goal-object", default=None, help="omit for navigation-only (no gimbal align)")
    parser.add_argument("--seconds", type=int, default=6, help="probe: heartbeat listen window")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--dwell-min", type=float, default=5.0)
    parser.add_argument("--dwell-max", type=float, default=10.0)
    parser.add_argument("--scan", action="store_true", help="enable the optional gimbal scan")
    parser.add_argument("--no-record", action="store_true")
    parser.add_argument("--yes", action="store_true", help="skip the movement confirmation")
    args = parser.parse_args()

    if args.stage in {"goal", "cruise"} and not args.path:
        parser.error(f"--path is required for the '{args.stage}' stage")

    runner = {"probe": stage_probe, "goal": stage_goal, "cruise": stage_cruise}[args.stage]
    try:
        return asyncio.run(runner(args))
    except KeyboardInterrupt:
        print("\ninterrupted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
