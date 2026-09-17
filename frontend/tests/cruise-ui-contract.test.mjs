import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const appSource = readFileSync(new URL('../src/renderer/src/App.vue', import.meta.url), 'utf8')

test('cruise UI exposes only anchor dwell, not per-point dwell or internal selection details', () => {
  assert.match(appSource, /自动运镜（使用「镜头设置」中的配置）/)
  assert.match(appSource, /每次回到锚点后停留（秒）/)
  assert.doesNotMatch(appSource, /每点停留|四区域|四个画面区域|cruiseDwell/)
})

test('cruise launch requests do not submit a retired per-point dwell field', () => {
  const buildStart = appSource.indexOf('function buildCruiseRequest()')
  const buildEnd = appSource.indexOf('\nfunction applyCruiseRequest(', buildStart)
  const testStart = appSource.indexOf('async function testCruisePoint(')
  const testEnd = appSource.indexOf('\nasync function loadCruisePaths(', testStart)

  assert.ok(buildStart >= 0 && buildEnd > buildStart)
  assert.ok(testStart >= 0 && testEnd > testStart)
  assert.doesNotMatch(appSource.slice(buildStart, buildEnd), /dwell_seconds/)
  assert.doesNotMatch(appSource.slice(testStart, testEnd), /dwell_seconds/)
})
