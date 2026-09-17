# Archived parked gimbal scan v1

This is an inert record of the retired `停留时云台缓慢扫视` feature. Production code no longer
offers, imports, serializes, or executes it. Legacy `gimbal_scan` input is accepted only so an
old saved route can be migrated; it is discarded before the active request is created.

## Former behavior

At each arrived point, a separate planner moved yaw away from the current heartbeat angle and
then back to that same angle. It had its own direction, offset, speed, tolerance, dwell-floor,
and `scanned` result fields. This duplicated ownership of the gimbal and did not fit the current
four-region-plus-anchor model, so the whole active path was removed rather than hidden.

## Archived source shape

The last complete implementation remains recoverable from Git commit `6d9f28e`:

```bash
git show 6d9f28e:backend/src/automated_video_editing_backend/core/models.py
git show 6d9f28e:backend/src/automated_video_editing_backend/services/cruise.py
```

Its request model and execution path were equivalent to:

```python
class GimbalScanConfig(BaseModel):
    enabled: bool = False
    direction: Literal["left", "right"] = "right"
    yaw_offset_deg: float = Field(default=15.0, ge=1.0, le=60.0)
    yaw_speed_deg_s: float = Field(default=5.0, ge=1.0, le=30.0)
    settle_tolerance_deg: float = Field(default=2.0, ge=0.1, le=15.0)

async def _scan(self, scan: GimbalScanConfig) -> bool:
    center = self.robot.heartbeat_yaw()
    if center is None:
        center = (await self.robot.status()).yaw
    if center is None:
        return False
    target = clamp(center + scan.signed_offset_deg, -90.0, 90.0)
    await self.robot.sweep_camera(target, scan.yaw_speed_deg_s)
    await self._await_yaw(target, scan)
    await self.robot.sweep_camera(center, scan.yaw_speed_deg_s)
    return await self._await_yaw(center, scan)
```

This excerpt is documentation, not importable code and not an operational fallback.
