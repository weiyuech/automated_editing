import test from 'node:test'
import assert from 'node:assert/strict'
import { groupCompositions, selectCompositionGroup, selectCompositionChild, compositionGroupKey, compositionLabel } from '../src/renderer/src/composition-groups.js'

function item(id, origins, order) {
  return { id, path: `/${id}.mp4`, kind: 'video', metadata: { role: 'raw_video', composition_id: id, source_recording_ids: origins, composition_order: order } }
}

test('same recording groups together despite filenames; different recordings never merge by title', () => {
  const items = [item('b', ['capture:2'], 2), item('a2', ['capture:1'], 3), item('a1', ['capture:1'], 1)]
  const groups = groupCompositions(items)
  assert.deepEqual(groups.map(group => group.children.map(child => child.id)), [['a1', 'a2'], ['b']])
  assert.deepEqual(selectCompositionGroup(groups, ['b'], 'capture:1', true), ['a1', 'a2'])
  assert.deepEqual(selectCompositionChild(groups, ['a1', 'a2'], 'b', true), ['b'])
  assert.deepEqual(selectCompositionChild(groups, ['a1', 'a2'], 'a1', false), ['a2'])
  assert.deepEqual(selectCompositionGroup(groups, ['a2'], 'capture:1', false), [])
})

test('cross-recording combinations stay independent and legacy provenance is conservative', () => {
  assert.notEqual(compositionGroupKey(item('x', ['a', 'b'])), compositionGroupKey(item('y', ['a', 'b'])))
  const legacy = item('l', [], 1)
  legacy.metadata.composition_tree = [{ id: 'origin', kind: 'recording' }]
  assert.equal(compositionGroupKey(legacy), 'legacy:origin')
})

test('rename changes displayed title without changing recording provenance', () => {
  const renamed = item('abc12345', ['capture:1'], 1)
  renamed.metadata.title = '旧名称'
  renamed.path = '/新名称.mp4'
  assert.equal(compositionLabel(renamed), '新名称')
  renamed.path = '/初始名称 组合-abc12345.mp4'
  assert.equal(compositionLabel(renamed), '初始名称')
})
