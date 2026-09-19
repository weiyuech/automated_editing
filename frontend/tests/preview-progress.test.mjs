import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { compileScript, parse } from 'vue/compiler-sfc'
import { computed, effectScope, ref, watch } from 'vue'
import { isPreviewPending, previewProgress, previewStageLabel, previewTime } from '../src/renderer/src/preview-progress.js'
import { captureGroup, fullCaptureSelection, formatCaptureTime } from '../src/renderer/src/capture-group-policy.js'

function deferred() {
  let resolve
  const promise = new Promise(r => { resolve = r })
  return { promise, resolve }
}
function harness(name, api) {
  const source = readFileSync(new URL(`../src/renderer/src/components/${name}.vue`, import.meta.url), 'utf8')
  const script = compileScript(parse(source).descriptor, { id: 'preview-races' }).content
    .replace(/^import[\s\S]*?from ['"][^'"]+['"]\n/gm, '').replace('export default', 'return')
  const scope = effectScope(), cleanup = [], events = []
  const deps = { computed, ref, watch, isPreviewPending, captureGroup, fullCaptureSelection, formatCaptureTime,
    PreviewProgress: {}, SubtitleFontPicker: {}, CaptureGroupPicker: {}, CompositionTree: {},
    onMounted: () => {}, onUnmounted: fn => cleanup.push(fn), setTimeout: () => 0, clearTimeout: () => {} }
  const component = Function(...Object.keys(deps), script)(...Object.values(deps))
  const state = scope.run(() => component.setup({ api, active: true, sources: [{id:'video'}], voices: [], music: [], intro: [], outro: [] }, {
    expose() {}, emit(...args) { events.push(args) },
  }))
  return { state, events, close: () => { cleanup.forEach(fn => fn()); scope.stop() } }
}
function preview(id, status = 'building') {
  return { id, status, title: id, request: { purpose: 'library', media_ids: [], capture_selections: [] } }
}

test('progress handles unknown measurements without fabricated elapsed or percentage', () => {
  assert.equal(previewProgress({}), undefined)
  assert.equal(previewProgress({progress:NaN}), undefined)
  assert.equal(previewProgress({progress:1.2}), 1)
  assert.equal(previewProgress({progress:-1}), 0)
  assert.equal(previewTime(undefined), '—')
  assert.equal(previewTime(125), '2 分 5 秒')
  assert.equal(previewStageLabel({status:'cancelled',stage:'encoding'}), '已取消')
  assert.equal(isPreviewPending({status:'cancelled'}), false)
})

test('studio preview creation returns to UI immediately with queued records and no export event', async () => {
  const calls = []
  const h = harness('StudioWorkbench', async path => { calls.push(path); return [preview('one','queued')] })
  await h.state.build()
  assert.deepEqual(calls, ['/studio/previews'])
  assert.equal(h.state.records.value[0].status, 'queued')
  assert.equal(h.state.busy.value, false)
  assert.deepEqual(h.events, [])
  h.close()
})

test('studio cancellation rejects stale polling per preview while other previews update', async () => {
  const one = deferred(), two = deferred()
  const h = harness('StudioWorkbench', path => path.endsWith('/cancel')
    ? Promise.resolve(preview('one','cancelled')) : path.endsWith('/one') ? one.promise : two.promise)
  h.state.records.value = [preview('one'),preview('two')]
  const polling = h.state.poll()
  await h.state.cancel(h.state.records.value[0])
  one.resolve(preview('one','building')); two.resolve(preview('two','ready'))
  await polling
  assert.equal(h.state.records.value[0].status, 'cancelled')
  assert.equal(h.state.records.value[1].status, 'ready')
  assert.deepEqual(h.events, [])
  h.close()
})

test('composer creation does not wait on scanning the media library', async () => {
  const calls = []
  const h = harness('ControlledComposer', async path => { calls.push(path); return preview('one','queued') })
  await h.state.build()
  assert.deepEqual(calls, ['/compositions'])
  assert.equal(h.state.record.value.status, 'queued')
  assert.equal(h.state.busy.value, false)
  h.close()
})

test('composer polling cannot switch back to a previously selected record', async () => {
  const pending = deferred()
  const h = harness('ControlledComposer', () => pending.promise)
  h.state.load(preview('one'))
  const polling = h.state.poll()
  h.state.load(preview('two','ready'))
  pending.resolve(preview('one','ready'))
  await polling
  assert.equal(h.state.record.value.id, 'two')
  h.close()
})

test('composer cancellation cannot be undone by a prior poll', async () => {
  const pending = deferred()
  const h = harness('ControlledComposer', path => path.endsWith('/cancel')
    ? Promise.resolve(preview('one','cancelled')) : pending.promise)
  h.state.load(preview('one'))
  const polling = h.state.poll()
  await h.state.cancel()
  pending.resolve(preview('one','building'))
  await polling
  assert.equal(h.state.record.value.status, 'cancelled')
  assert.equal(h.state.records.value[0].status, 'cancelled')
  h.close()
})
