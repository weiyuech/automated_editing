import assert from 'node:assert/strict'
import test from 'node:test'

import {
  cruiseMapChangeDecision,
  cruiseMapContext,
  inspectCruisePlan,
  isCurrentMapRequest,
  isValidCruiseGoalId,
  isSavedCruiseRequestReady
} from '../src/renderer/src/cruise-map-policy.js'

test('map context distinguishes the robot map from the cruise target', () => {
  assert.deepEqual(cruiseMapContext('map-a', 'map-a'), {
    kind: 'success',
    text: '机器人当前：map-a · 本次巡游：map-a'
  })
  assert.deepEqual(cruiseMapContext('map-a', 'map-b'), {
    kind: 'muted',
    text: '机器人当前：map-a · 本次巡游：map-b · 开始时自动切换'
  })
  assert.equal(cruiseMapContext('map-a', '').text, '机器人当前：map-a · 请选择本次巡游地图')
})

test('late path responses cannot overwrite a newer map selection', () => {
  assert.equal(isCurrentMapRequest('map-a', 'map-b', 1, 2), false)
  assert.equal(isCurrentMapRequest('map-a', 'map-a', 1, 2), false)
  assert.equal(isCurrentMapRequest('map-b', 'map-b', 2, 2), true)
})

test('assigning the first map repairs a mapless legacy route without discarding its points', () => {
  assert.deepEqual(cruiseMapChangeDecision('', 'map-a', 3), {
    preservePoints: true,
    requiresConfirmation: false,
    clearPoints: false
  })
  assert.deepEqual(cruiseMapChangeDecision('map-a', 'map-b', 3), {
    preservePoints: false,
    requiresConfirmation: true,
    clearPoints: true
  })
  assert.deepEqual(cruiseMapChangeDecision('map-a', '', 3), {
    preservePoints: false,
    requiresConfirmation: true,
    clearPoints: true
  })
})

test('blank and null goal ids are never coerced to point zero', () => {
  assert.equal(isValidCruiseGoalId(''), false)
  assert.equal(isValidCruiseGoalId('   '), false)
  assert.equal(isValidCruiseGoalId(null), false)
  assert.equal(isValidCruiseGoalId(false), false)
  assert.equal(isValidCruiseGoalId(0), true)
  assert.equal(isValidCruiseGoalId('12'), true)
})

test('a cruise cannot start until its map paths have been loaded', () => {
  const points = [{ path_name: 'route-a', goal_id: 1 }]

  assert.equal(inspectCruisePlan({ cruiseMap: '', loadedMap: '', availablePaths: [], points }).code, 'missing-map')
  assert.equal(inspectCruisePlan({ cruiseMap: 'map-a', loadedMap: '', availablePaths: [], points }).code, 'paths-unverified')
})

test('a refreshed map list can invalidate a removed cruise map without rewriting the route', () => {
  const result = inspectCruisePlan({
    cruiseMap: 'map-a',
    mapsVerified: true,
    availableMaps: ['map-b'],
    loadedMap: 'map-a',
    availablePaths: ['route-a'],
    points: [{ path_name: 'route-a', goal_id: 1 }]
  })

  assert.equal(result.ready, false)
  assert.equal(result.code, 'unavailable-map')
  assert.match(result.message, /map-a/)
})

test('a point from another map is rejected instead of being submitted', () => {
  const result = inspectCruisePlan({
    cruiseMap: 'map-b',
    loadedMap: 'map-b',
    availablePaths: ['route-b'],
    points: [{ path_name: 'route-a', goal_id: 1 }]
  })

  assert.equal(result.ready, false)
  assert.equal(result.code, 'stale-paths')
  assert.deepEqual(result.stalePaths, ['route-a'])
})

test('a cruise is ready only when every point belongs to the verified map', () => {
  const result = inspectCruisePlan({
    cruiseMap: 'map-a',
    loadedMap: 'map-a',
    availablePaths: ['route-a', 'route-b'],
    points: [
      { path_name: 'route-a', goal_id: 0 },
      { path_name: 'route-b', goal_id: 12 }
    ]
  })

  assert.equal(result.ready, true)
  assert.equal(result.code, 'ready')
})

test('saved route start requires a map and structurally valid points', () => {
  assert.equal(isSavedCruiseRequestReady({ map_name: '', points: [{ path_name: 'route-a', goal_id: 1 }] }), false)
  assert.equal(isSavedCruiseRequestReady({ map_name: 'map-a', points: [] }), false)
  assert.equal(isSavedCruiseRequestReady({ map_name: 'map-a', points: [{ path_name: '', goal_id: 1 }] }), false)
  assert.equal(isSavedCruiseRequestReady({ map_name: 'map-a', points: [{ path_name: 'route-a', goal_id: '' }] }), false)
  assert.equal(isSavedCruiseRequestReady({ map_name: 'map-a', points: [{ path_name: 'route-a', goal_id: -1 }] }), false)
  assert.equal(isSavedCruiseRequestReady({ map_name: 'map-a', points: [{ path_name: 'route-a', goal_id: 1 }] }), true)
})
