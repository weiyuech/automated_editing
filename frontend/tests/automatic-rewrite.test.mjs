import test from 'node:test'
import assert from 'node:assert/strict'
import { effectScope, nextTick, ref } from 'vue'
import { useAutomaticRewrite } from '../src/renderer/src/use-automatic-rewrite.js'

function setup(readyInitially = true) {
  const enabled = ref(false), ready = ref(readyInitially), scope = effectScope()
  const calls = []
  const state = scope.run(() => useAutomaticRewrite({ enabled, ready, run: () => new Promise(resolve => calls.push(resolve)) }))
  return { enabled, ready, scope, calls, ...state }
}

test('off by default; toggle starts exactly once despite readiness changes', async () => {
  const s = setup()
  await nextTick()
  assert.equal(s.calls.length, 0)
  s.enabled.value = true
  await nextTick()
  assert.equal(s.calls.length, 1)
  s.ready.value = false
  await nextTick()
  s.ready.value = true
  s.calls[0]()
  await nextTick(); await nextTick()
  assert.equal(s.calls.length, 1)
  s.scope.stop()
})

test('missing instructions/context wait without requiring another toggle', async () => {
  const s = setup(false)
  s.enabled.value = true
  await nextTick()
  assert.equal(s.calls.length, 0)
  assert.equal(s.pending.value, true)
  s.ready.value = true
  await nextTick()
  assert.equal(s.calls.length, 1)
  s.calls[0]()
  s.scope.stop()
})

test('turning off cancels pending work, and disposal cannot start a request', async () => {
  const s = setup(false)
  s.enabled.value = true
  s.enabled.value = false
  s.ready.value = true
  await nextTick()
  assert.equal(s.calls.length, 0)
  s.scope.stop()
  s.enabled.value = true
  await nextTick()
  assert.equal(s.calls.length, 0)
})

test('a new explicit toggle can start one further request after completion', async () => {
  const s = setup()
  s.enabled.value = true
  await nextTick()
  s.calls[0]()
  await nextTick(); await nextTick()
  s.enabled.value = false
  await nextTick()
  s.enabled.value = true
  await nextTick()
  assert.equal(s.calls.length, 2)
  s.calls[1]()
  s.scope.stop()
})
