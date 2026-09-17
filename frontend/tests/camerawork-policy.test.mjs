import assert from 'node:assert/strict'
import test from 'node:test'

import {
  DEFAULT_CAMERAWORK_PROFILE,
  cameraworkProfileWarning,
  normalizeCameraworkProfile
} from '../src/renderer/src/camerawork-policy.js'

test('legacy camerawork settings gain the new anchor rhythm defaults', () => {
  const normalized = normalizeCameraworkProfile({ anchor_yaw: 12, speed_max: 4 })

  assert.equal(normalized.anchor_yaw, 12)
  assert.equal(normalized.speed_max, 4)
  assert.equal(normalized.anchor_time_percent, 20)
  assert.equal(normalized.anchor_dwell_seconds, 5)
  assert.equal(cameraworkProfileWarning(normalized), '')
})

test('anchor rhythm settings are normalized as numbers and validated at their bounds', () => {
  const minimum = normalizeCameraworkProfile({
    anchor_time_percent: '0',
    anchor_dwell_seconds: '0.5'
  })
  const maximum = normalizeCameraworkProfile({
    anchor_time_percent: '100',
    anchor_dwell_seconds: '120'
  })

  assert.equal(cameraworkProfileWarning(minimum), '')
  assert.equal(cameraworkProfileWarning(maximum), '')
  assert.match(
    cameraworkProfileWarning({ ...DEFAULT_CAMERAWORK_PROFILE, anchor_time_percent: 20.5 }),
    /回到锚点的时间占比必须是 0~100%/
  )
  assert.match(
    cameraworkProfileWarning({ ...DEFAULT_CAMERAWORK_PROFILE, anchor_dwell_seconds: 0.49 }),
    /每次回到锚点后停留时间必须在 0.5~120/
  )
  assert.match(
    cameraworkProfileWarning({ ...DEFAULT_CAMERAWORK_PROFILE, anchor_dwell_seconds: 121 }),
    /0.5~120/
  )
})

test('normalization only keeps supported profile fields', () => {
  const normalized = normalizeCameraworkProfile({
    anchor_time_percent: 30,
    obsolete_mode_percentage: 50
  })

  assert.equal(normalized.anchor_time_percent, 30)
  assert.equal('obsolete_mode_percentage' in normalized, false)
})
