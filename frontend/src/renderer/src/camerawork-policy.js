export const DEFAULT_CAMERAWORK_PROFILE = Object.freeze({
  anchor_yaw: 0, anchor_pitch: 0, anchor_zoom: 1, zoom_target: 2,
  yaw_min: -60, yaw_max: 60, pitch_min: -15, pitch_max: 15,
  speed_max: 5, angle_tolerance_degrees: 20, zoom_tolerance: 0.1,
  settled_delta_degrees: 0.5, settled_delta_zoom: 0.03,
  point_mode: 4, piece_ids: null
})
export function normalizeCameraworkProfile(saved = {}) {
  const known = Object.fromEntries(Object.keys(DEFAULT_CAMERAWORK_PROFILE).map(key => [key, key === 'piece_ids' ? (saved?.[key] ?? null) : Number(saved?.[key] ?? DEFAULT_CAMERAWORK_PROFILE[key])]))
  return { ...known, anchor_yaw:0, anchor_pitch:0,
    zoom_target: Number(saved?.zoom_target ?? (Number(saved?.anchor_zoom) === 2 ? 1 : 2)),
    point_mode:Number(saved?.point_mode || 4),
    piece_ids:saved?.piece_ids ?? null }
}
export function cameraworkProfileWarning(form) {
  if (![form.yaw_min,form.yaw_max,form.pitch_min,form.pitch_max,form.speed_max].every(Number.isInteger)) return '角度和速度必须是整数。'
  if (!(form.yaw_min >= -90 && form.yaw_min < 0 && form.yaw_max > 0 && form.yaw_max <= 90)) return '水平范围必须在 −90°~90° 内，并分布在原点两侧。'
  if (!(form.pitch_min >= -60 && form.pitch_min < 0 && form.pitch_max > 0 && form.pitch_max <= 15)) return '俯仰范围必须在 −60°~15° 内，并分布在原点两侧。'
  if (!(form.anchor_zoom >= 1 && form.anchor_zoom <= 3.5)) return '基础倍率必须在 1~3.5 内。'
  if (!(form.zoom_target >= 1 && form.zoom_target <= 3.5)) return '缩放倍率必须在 1~3.5 内。'
  if (form.zoom_target === form.anchor_zoom) return '缩放倍率需与基础倍率不同。'
  if (!(form.speed_max >= 2 && form.speed_max <= 5)) return '速度必须在 2~5°/秒内。'
  if (!(form.angle_tolerance_degrees >= 0.1 && form.angle_tolerance_degrees <= 30)) return '可接受角度偏差必须在 0.1°~30° 内。'
  if (!(form.zoom_tolerance >= 0.01 && form.zoom_tolerance <= 1.5)) return '可接受倍率偏差必须在 0.01~1.5 倍内。'
  if (!(form.settled_delta_degrees > 0)) return '停稳阈值角度必须是正数。'
  if (!(form.settled_delta_zoom > 0)) return '停稳阈值倍率必须是正数。'
  return ''
}
