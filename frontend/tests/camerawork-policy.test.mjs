import assert from 'node:assert/strict'
import test from 'node:test'
import { cameraworkProfileWarning, normalizeCameraworkProfile } from '../src/renderer/src/camerawork-policy.js'
test('legacy anchors migrate to the fixed origin and unsupported controls are discarded', () => {
  const profile = normalizeCameraworkProfile({ anchor_yaw:12, anchor_pitch:-8, anchor_time_percent:20, speed_max:'4' })
  assert.equal(profile.anchor_yaw,0)
  assert.equal(profile.anchor_pitch,0)
  assert.equal(profile.speed_max,4)
  assert.equal('anchor_time_percent' in profile,false)
  assert.equal(profile.point_mode,4)
  assert.equal(cameraworkProfileWarning(profile),'')
})
test('four edges require bounds on both sides of physical zero', () => {
  assert.match(cameraworkProfileWarning(normalizeCameraworkProfile({yaw_min:5})),/原点两侧/)
  assert.match(cameraworkProfileWarning(normalizeCameraworkProfile({pitch_max:0})),/原点两侧/)
})
