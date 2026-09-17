export const DEFAULT_CAMERAWORK_PROFILE = Object.freeze({
  anchor_yaw: 0,
  anchor_pitch: 0,
  anchor_zoom: 1,
  anchor_time_percent: 20,
  anchor_dwell_seconds: 5,
  yaw_min: -60,
  yaw_max: 60,
  pitch_min: -15,
  pitch_max: 15,
  zoom_min: 1,
  zoom_max: 1.5,
  speed_min: 2,
  speed_max: 5
})

export function normalizeCameraworkProfile(saved = {}) {
  return Object.fromEntries(
    Object.entries(DEFAULT_CAMERAWORK_PROFILE).map(([key, fallback]) => [
      key,
      Number(saved?.[key] ?? fallback)
    ])
  )
}

export function cameraworkProfileWarning(form) {
  if (Object.values(form).some((value) => !Number.isFinite(Number(value)))) {
    return '所有自动运镜参数都必须是数字。'
  }
  if (![form.anchor_yaw, form.anchor_pitch, form.yaw_min, form.yaw_max, form.pitch_min, form.pitch_max].every(Number.isInteger)) {
    return '水平和俯仰角度必须是整数。'
  }
  if (!Number.isInteger(form.anchor_time_percent) || form.anchor_time_percent < 0 || form.anchor_time_percent > 100) {
    return '回到锚点的时间占比必须是 0~100% 内的整数。'
  }
  if (form.anchor_dwell_seconds < 0.5 || form.anchor_dwell_seconds > 120) {
    return '每次回到锚点后停留时间必须在 0.5~120 秒内。'
  }
  if (form.yaw_min < -90 || form.yaw_max > 90 || form.yaw_max <= form.yaw_min) {
    return '水平范围必须在 −90°~90° 内，且左边界大于右边界。'
  }
  if (form.pitch_min < -60 || form.pitch_max > 15 || form.pitch_max <= form.pitch_min) {
    return '俯仰范围必须在 −60°~15° 内，且下边界大于上边界。'
  }
  if (form.zoom_min < 1 || form.zoom_max > 3.5 || form.zoom_max <= form.zoom_min) {
    return '变焦范围必须在 1~3.5 内，且最大值大于最小值。'
  }
  if (!Number.isInteger(form.speed_min) || !Number.isInteger(form.speed_max) || form.speed_min < 2 || form.speed_max > 5 || form.speed_max < form.speed_min) {
    return '速度必须是 2~5°/秒内的整数，且最快速度不小于最慢速度。'
  }
  if (form.anchor_yaw < form.yaw_min || form.anchor_yaw > form.yaw_max) {
    return '水平锚点必须位于水平范围内。'
  }
  if (form.anchor_pitch < form.pitch_min || form.anchor_pitch > form.pitch_max) {
    return '俯仰锚点必须位于俯仰范围内。'
  }
  if (form.anchor_zoom < form.zoom_min || form.anchor_zoom > form.zoom_max) {
    return '变焦锚点必须位于变焦范围内。'
  }
  return ''
}
