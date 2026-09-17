import json

from automated_video_editing_backend.core import diagnostics


def test_diagnostics_write_asynchronously_with_timing_and_redaction(tmp_path):
    log_path = tmp_path / "diagnostics.log"
    diagnostics.configure_diagnostics(log_path)
    try:
        diagnostics.log_event(
            "info",
            "robot.heartbeat.received",
            payload={
                "gimbal": {"yaw": 1.5, "pitch": -2.0},
                "authorization": "do-not-log",
                "media_url": "https://robot.local/video.mp4?signature=secret",
            },
        )
    finally:
        # QueueListener.stop drains records before joining its writer thread.
        diagnostics._stop_diagnostic_listener()

    line = log_path.read_text(encoding="utf-8").strip()
    payload = json.loads(line.split("robot.heartbeat.received ", 1)[1])
    assert payload["at"].endswith("+00:00")
    assert payload["monotonic_seconds"] > 0
    assert payload["payload"]["gimbal"] == {"yaw": 1.5, "pitch": -2.0}
    assert payload["payload"]["authorization"] == "<redacted>"
    assert payload["payload"]["media_url"] == (
        "https://robot.local/video.mp4?<redacted>"
    )
