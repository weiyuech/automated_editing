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
  assert.equal(profile.angle_tolerance_degrees,20)
  assert.equal(profile.zoom_tolerance,0.1)
  assert.equal(cameraworkProfileWarning(profile),'')
})
test('operator framing tolerances are persisted and range checked', () => {
  const profile = normalizeCameraworkProfile({angle_tolerance_degrees:'7.5',zoom_tolerance:'0.08'})
  assert.equal(profile.angle_tolerance_degrees,7.5)
  assert.equal(profile.zoom_tolerance,0.08)
  assert.equal(cameraworkProfileWarning(profile),'')
  assert.match(cameraworkProfileWarning({...profile,angle_tolerance_degrees:0}),/角度偏差/)
  assert.match(cameraworkProfileWarning({...profile,zoom_tolerance:2}),/倍率偏差/)
})
test('four edges require bounds on both sides of physical zero', () => {
  assert.match(cameraworkProfileWarning(normalizeCameraworkProfile({yaw_min:5})),/原点两侧/)
  assert.match(cameraworkProfileWarning(normalizeCameraworkProfile({pitch_max:0})),/原点两侧/)
})
test('base and zoom magnifications are separate, editable and range checked', () => {
  const profile = normalizeCameraworkProfile({anchor_zoom:1.2, zoom_target:1.5})
  assert.equal(profile.anchor_zoom,1.2)
  assert.equal(profile.zoom_target,1.5)
  assert.equal(cameraworkProfileWarning(profile),'')
  assert.equal(normalizeCameraworkProfile({anchor_zoom:2}).zoom_target,1)
  assert.match(cameraworkProfileWarning({...profile,zoom_target:1.2}),/不同/)
  assert.match(cameraworkProfileWarning({...profile,zoom_target:3.6}),/缩放倍率/)
})
