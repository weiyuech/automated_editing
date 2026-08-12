#!/usr/bin/env python3
"""Standalone, READ-ONLY robot probe. Commands nothing, moves nothing.

Copy this single file to a machine on the robot's network. It has no project
dependencies -- only the websockets package.

    pip install websockets
    python robot_probe_standalone.py ws://10.73.2.199:8765

It answers three questions:
  1. Can this machine reach the robot at all?
  2. Does the heartbeat contain the fields cruise arrival detection relies on?
  3. Does the heartbeat report gimbal yaw? (decides whether the optional camera
     scan can verify a return to centre, or must fall back to a timer)
"""

from __future__ import annotations

import asyncio
import json
import sys

try:
    import websockets
except ImportError:
    print("Missing dependency. Run:  pip install websockets")
    raise SystemExit(1) from None

LISTEN_SECONDS = 8


async def probe(url: str) -> int:
    print(f"connecting to {url} ...")
    try:
        connection = websockets.connect(url, open_timeout=8, ping_interval=None)
    except Exception as exc:
        print(f"  FAILED: {type(exc).__name__}: {exc}")
        return 1

    try:
        async with connection as socket:
            print("  connected.\n")
            print(f"listening {LISTEN_SECONDS}s (sending nothing) ...")

            raw: list[str] = []
            deadline = asyncio.get_event_loop().time() + LISTEN_SECONDS
            while asyncio.get_event_loop().time() < deadline:
                remaining = deadline - asyncio.get_event_loop().time()
                try:
                    message = await asyncio.wait_for(socket.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                except Exception as exc:
                    print(f"  receive stopped: {type(exc).__name__}: {exc}")
                    break
                raw.append(message if isinstance(message, str) else message.decode("utf-8", "replace"))
    except Exception as exc:
        print(f"  FAILED: {type(exc).__name__}: {exc}")
        print("\n  'did not receive a valid HTTP response' usually means something accepted")
        print("  the connection but is not the robot -- often a VPN or proxy in the way.")
        return 1

    print(f"  received {len(raw)} message(s)\n")
    if not raw:
        print("NO MESSAGES. The robot connected but never sent a heartbeat.")
        print("Cruise arrival detection depends on heartbeats and cannot work without them.")
        return 1

    heartbeats = []
    for message in raw:
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and ("task" in payload or "system" in payload):
            heartbeats.append(payload)

    print(f"--- heartbeats: {len(heartbeats)} of {len(raw)} messages ---")
    if not heartbeats:
        print("  None looked like a heartbeat. Raw sample:")
        print("   ", raw[-1][:400])
        return 1

    sample = heartbeats[-1]
    print(f"  top-level keys : {sorted(sample)}")

    nav = "naviagtion" if "naviagtion" in sample else "navigation" if "navigation" in sample else None
    print(f"  navigation key : {nav or 'MISSING'}   (the contract misspells it 'naviagtion')")

    task = sample.get("task") or {}
    goal_status = task.get("goal_status", "MISSING")
    object_status = task.get("object_status", "MISSING")
    print(f"  task.goal_status   : {goal_status}   <- arrival is decided on this")
    print(f"  task.object_status : {object_status}   <- only waited on if a point sets goal_object")

    gimbal = sample.get("gimbal") or {}
    yaw = gimbal.get("yaw")
    print(f"  gimbal.yaw         : {yaw if yaw is not None else 'MISSING'}")
    if yaw is None:
        print("     -> optional camera scan cannot confirm a return to centre;")
        print("        it will use a time budget, or skip if the angle is unknown.")
    else:
        print("     -> optional camera scan can verify the camera returned to centre.")

    statuses = {hb.get("task", {}).get("goal_status") for hb in heartbeats}
    print(f"\n  goal_status values seen: {sorted(s for s in statuses if s)}")

    print("\n  latest heartbeat:")
    print("   ", json.dumps(sample, ensure_ascii=False)[:700])

    print("\nprobe OK. Nothing was commanded.")
    return 0


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    try:
        return asyncio.run(probe(sys.argv[1]))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
