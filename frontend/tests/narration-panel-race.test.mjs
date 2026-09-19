import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { compileScript, parse } from 'vue/compiler-sfc'
import { computed, effectScope, nextTick, ref, watch } from 'vue'
import { narrationMediaName, compositionDuration } from '../src/renderer/src/narration-context.js'
import { useAutomaticRewrite } from '../src/renderer/src/use-automatic-rewrite.js'

// Exercise the real component's asynchronous setup logic without a browser or
// paid services. Vue's SFC compiler exposes the same setup used by production.
const source = readFileSync(new URL('../src/renderer/src/components/NarrationPanel.vue', import.meta.url), 'utf8')
const script = compileScript(parse(source).descriptor, { id: 'narration-races' }).content
  .replace(/^import .*\n/gm, '').replace('export default', 'return')
function deferred() {
  let resolve
  const promise = new Promise(r => { resolve = r })
  return { promise, resolve }
}
async function harness() {
  const scope = effectScope(), cleanup = []
  let handle = async () => ({ narration: null })
  const deps = { computed, nextTick, ref, watch, narrationMediaName, compositionDuration, useAutomaticRewrite,
    onMounted: () => {}, onUnmounted: fn => cleanup.push(fn), setTimeout: () => 0, clearTimeout: () => {} }
  const component = Function(...Object.keys(deps), script)(...Object.values(deps))
  const state = scope.run(() => component.setup({ api: (...args) => handle(...args), active: true }, { expose() {}, emit() {} }))
  state.composition.value = 'one'
  await nextTick(); await nextTick()
  return { state, setApi: fn => { handle = fn }, close: () => { cleanup.forEach(fn => fn()); scope.stop() } }
}

test('a poll begun before adjustment cannot overwrite the new synthesis attempt', async () => {
  const h = await harness(), oldPoll = deferred(), adjustment = deferred()
  h.state.result.value = { status: 'running', attempt_id: 'old' }
  h.setApi(path => path.endsWith('/adjust') ? adjustment.promise : oldPoll.promise)
  const polling = h.state.poll()
  const adjusting = h.state.adjust()
  adjustment.resolve({ status: 'running', attempt_id: 'new' })
  await adjusting
  oldPoll.resolve({ narration: { status: 'ready', attempt_id: 'old' } })
  await polling
  assert.equal(h.state.result.value.attempt_id, 'new')
  assert.equal(h.state.result.value.status, 'running')
  h.close()
})

test('poll ignores a response for a different backend attempt', async () => {
  const h = await harness()
  h.state.result.value = { status: 'running', attempt_id: 'current' }
  h.setApi(async () => ({ narration: { status: 'ready', attempt_id: 'previous' } }))
  await h.state.poll()
  assert.equal(h.state.result.value.attempt_id, 'current')
  h.close()
})

test('generation response cannot populate a different composition', async () => {
  const h = await harness(), synthesis = deferred()
  h.state.source.value = '测试文案'
  h.setApi(path => path.endsWith('/narration') ? synthesis.promise : Promise.resolve({ narration: null }))
  const generating = h.state.generate()
  h.state.composition.value = 'two'
  await nextTick(); await nextTick()
  synthesis.resolve({ status: 'running', attempt_id: 'wrong-composition' })
  await generating
  assert.equal(h.state.result.value, null)
  h.close()
})

test('generation response cannot update an unmounted panel', async () => {
  const h = await harness(), synthesis = deferred()
  h.setApi(() => synthesis.promise)
  const generating = h.state.generate()
  h.close()
  synthesis.resolve({ status: 'running', attempt_id: 'late' })
  await generating
  assert.equal(h.state.result.value, null)
})

test('single-recording rewrite starts when mandatory instructions lose focus', async () => {
  const h = await harness(), rewrite = deferred(), calls = []
  h.state.items.value = [{ id: 'video', kind: 'video', metadata: { role: 'raw_video', composition_id: 'one' } }]
  h.state.contextData.value = { mode: 'single_recording', capture_notes: [] }
  h.state.source.value = '介绍产品和仓库'
  h.state.sourceEditing.value = true
  await nextTick()
  h.setApi((path, options) => { calls.push({ path, options }); return rewrite.promise })
  // Moving from the source field to the switch triggers blur even if a browser
  // autofill tool does not emit a native change event.
  h.state.sourceEditing.value = false
  h.state.useLlm.value = true
  await nextTick(); await nextTick()
  assert.equal(calls.length, 0)
  h.state.instructionsEditing.value = true
  h.state.instructions.value = '先介绍产品，再介绍仓库'
  await nextTick()
  assert.equal(calls.length, 0)
  h.state.instructionsEditing.value = false
  await nextTick(); await nextTick()
  assert.equal(calls.length, 1)
  assert.equal(calls[0].path, '/compositions/one/narration/draft')
  rewrite.resolve({ text: '这里展示产品，随后介绍仓库。', sections: [], note_assignments: [], general_notes: [] })
  await nextTick(); await nextTick(); await nextTick()
  assert.equal(h.state.draft.value, '这里展示产品，随后介绍仓库。')
  assert.equal(h.state.busy.value, false)
  h.close()
})
