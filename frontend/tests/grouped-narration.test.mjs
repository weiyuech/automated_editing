import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { computed, effectScope, reactive, ref, watch, nextTick } from 'vue'
import { narrationRequest, draftIdentity, batchNarrationItem, needsNarrationInstructions } from '../src/renderer/src/grouped-narration.js'
import { groupCompositions, savedCompositions } from '../src/renderer/src/composition-groups.js'

function item(id, notes = ['整段备注']) {
  return { id, kind: 'video', metadata: { composition_id: id, role: 'raw_video', visual_signature: `sig-${id}`,
    source_recording_ids: ['capture:one'], composition_tree: [{ id: 'one', kind: 'recording', notes }] } }
}
const defaults = { source: '原文事实', instructions: '', custom: true, system: '我的规则', llm: true, applyStyle: true, style: 'auto' }

test('automatic styles cycle in displayed order and optional style preserves custom system', () => {
  assert.deepEqual(Array.from({ length: 8 }, (_, index) => narrationRequest(defaults, index).narration_style),
    ['natural', 'professional', 'concise', 'humorous', 'poetic', 'classical', 'natural', 'professional'])
  assert.equal(narrationRequest(defaults, 2).system_prompt, '我的规则')
  assert.equal('narration_style' in narrationRequest({ ...defaults, applyStyle: false }, 2), false)
  assert.equal('narration_style' in narrationRequest({ ...defaults, llm: false }, 2), false)
})

test('only same-recording missing comments require instructions; paragraphs stay intact', () => {
  assert.equal(needsNarrationInstructions(item('a')), false)
  assert.equal(needsNarrationInstructions(item('a', [])), true)
  assert.equal(needsNarrationInstructions(item('a', '一整段，不按标点拆分。')), false)
  const cross = item('cross', [])
  cross.metadata.source_recording_ids.push('capture:two')
  assert.equal(needsNarrationInstructions(cross), false)
})

test('batch snapshot cannot synthesize a stale or unreviewed draft', () => {
  const video = item('a'), request = narrationRequest(defaults, 0)
  const draft = { key: draftIdentity(video, request), text: '用户已编辑的稿子', style: 'natural', version: 'draft', sections: [] }
  const options = { source: defaults.source, llm: true, rate: 1.1, autoTempo: true }
  const snapshot = batchNarrationItem(video, draft, request, options)
  assert.equal(snapshot.narration.text, draft.text)
  assert.equal(snapshot.narration.narration_style, 'natural')
  assert.equal(snapshot.narration.playback_rate, 1.1)
  assert.equal(snapshot.narration.direct_narration, false)
  assert.throws(() => batchNarrationItem(video, { ...draft, version: '' }, request, options), /审阅/)
  assert.throws(() => batchNarrationItem(video, draft, { ...request, instructions: '新要求' }, options), /审阅/)
  const direct = batchNarrationItem(video, draft, request, { ...options, llm: false })
  assert.equal(direct.narration.text, defaults.source)
  assert.equal(direct.narration.direct_narration, true)
  assert.equal('narration_style' in direct.narration, false)
})

const code = readFileSync(new URL('../src/renderer/src/use-grouped-narration.js', import.meta.url), 'utf8')
  .replace(/^import .*\r?\n/gm, '').replace('export function useGroupedNarration', 'function useGroupedNarration') + '\nreturn useGroupedNarration'
function harness(api) {
  const deps = { computed, reactive, ref, watch, groupCompositions, savedCompositions,
    narrationRequest, draftIdentity, batchNarrationItem, needsNarrationInstructions,
    onMounted: () => {}, onUnmounted: () => {}, setTimeout: () => 0, clearTimeout: () => {} }
  const create = Function(...Object.keys(deps), code)(...Object.values(deps))
  const scope = effectScope(), props = reactive({ api, active: true, initialCompositionIds: [] })
  const state = scope.run(() => create(props, () => {}))
  return { state, props, close: () => scope.stop() }
}
async function settle() { for (let i = 0; i < 16; i++) await nextTick() }

test('rewriting happens once per input in canonical order; inspecting does not duplicate calls', async () => {
  const calls = []
  const h = harness(async (path, options) => {
    calls.push({ path, request: JSON.parse(options.body) })
    return { text: '逐条改写稿', sections: [] }
  })
  h.state.items.value = [item('a'), item('b')]
  h.state.chosen.value = ['b', 'a']
  h.state.source.value = '事实原文'
  h.state.applyStyle.value = true
  h.state.style.value = 'auto'
  h.state.llm.value = true
  await settle()
  assert.equal(calls.length, 2)
  assert.match(calls[0].path, /a\/narration\/draft$/)
  assert.equal(calls[0].request.narration_style, 'natural')
  assert.equal(calls[1].request.narration_style, 'professional')
  h.state.inspect.value = 'b'
  h.state.showPrompt.value = true
  h.state.items.value = [...h.state.items.value]
  await settle()
  assert.equal(calls.length, 2)
  h.close()
})

test('missing input and hidden page block LLM; failures never retry by themselves', async () => {
  let calls = 0
  const h = harness(async (path) => {
    if (!path.endsWith('/draft')) return path === '/media' ? [item('a', [])] : []
    calls++; throw new Error('服务暂不可用')
  })
  h.state.items.value = [item('a', [])]
  h.state.chosen.value = ['a']
  h.state.source.value = '用户事实'
  h.state.llm.value = true
  await settle()
  assert.equal(calls, 0)
  h.props.active = false
  h.state.instructions.value = '先介绍用途'
  await settle()
  assert.equal(calls, 0)
  h.props.active = true
  await settle()
  assert.equal(calls, 1)
  h.state.items.value = [...h.state.items.value]
  await settle()
  assert.equal(calls, 1)
  h.close()
})

function deferred() {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}

test('late LLM response cannot become a draft for changed source text', async () => {
  const response = deferred()
  let calls = 0
  const h = harness(async () => { calls++; return response.promise })
  h.state.items.value = [item('a')]
  h.state.chosen.value = ['a']
  h.state.source.value = '原来的事实'
  h.state.llm.value = true
  await settle()
  h.state.editing.value = true
  h.state.source.value = '新的事实'
  response.resolve({ text: '旧请求返回稿', sections: [] })
  await settle()
  assert.equal(calls, 1)
  assert.equal(h.state.inspectedDraft.value, null)
  assert.equal(h.state.canGenerate.value, false)
  h.close()
})

test('a second generation click cannot dispatch another batch while the first is pending', async () => {
  const response = deferred(), calls = []
  const h = harness(async (path, options) => { calls.push({ path, body: JSON.parse(options.body) }); return response.promise })
  h.state.items.value = [item('a'), item('b')]
  h.state.chosen.value = ['a', 'b']
  h.state.source.value = '完整文案'
  await settle()
  const first = h.state.generate()
  await h.state.generate()
  assert.equal(calls.length, 1)
  assert.equal(calls[0].path, '/narration/batches')
  assert.equal(calls[0].body.items.length, 2)
  assert.equal(calls[0].body.items[0].narration.direct_narration, true)
  response.resolve({ id: 'batch', status: 'running', items: [] })
  await first
  assert.equal(h.state.locked.value, true)
  h.close()
})

test('speed adjustment reuses the existing attempt without dispatching paid synthesis', async () => {
  const calls = [], video = item('a')
  const old = { id: 'a', visual_signature: 'sig-a', narration: {
    attempt_id: 'already-paid', status: 'ready', source_seconds: 14, actual_seconds: 14,
    playback_rate: 1, auto_tempo: true, text: '已有语音正文',
  } }
  const h = harness(async (path, options) => {
    if (options?.method === 'POST') {
      calls.push({ path, body: JSON.parse(options.body) })
      return { ...old.narration, status: 'running', attempt_id: 'new-speed-attempt' }
    }
    return path === '/media' ? [video] : path === '/compositions' ? [old] : []
  })
  h.props.active = false
  await settle()
  h.props.active = true
  await settle()
  h.state.chosen.value = ['a']
  h.state.rate.value = 1.1
  await settle()
  assert.equal(Boolean(h.state.canAdjust.value), true)
  await h.state.adjust()
  assert.deepEqual(calls, [{ path: '/compositions/a/narration/adjust', body: {
    attempt_id: 'already-paid', playback_rate: 1.1, auto_tempo: true,
  } }])
  assert.equal(h.state.running.value, true)
  h.close()
})
