import test from 'node:test'
import assert from 'node:assert/strict'
import {
  linkedVoiceSelection,
  poolWithoutVoice,
  poolWithItems,
  poolWithoutSources,
  activeVoiceSource,
} from '../src/shared/composition-selection.js'
import { isMediaPoolEligible } from '../src/shared/media-policy.js'

const sources = [
  { id: 'a', metadata: { bound_voice_id: 'va' } },
  { id: 'b', metadata: { bound_voice_id: 'vb' } },
]
const voices = [
  { id: 'va', metadata: { binding_id: 'one', bound_source_id: 'a' } },
  { id: 'vb', metadata: { binding_id: 'two', bound_source_id: 'b' } },
  { id: 'old', metadata: { binding_id: 'old' } },
  { id: 'ordinary', metadata: {} },
]
test('checking and unchecking combinations derives exactly their active narration, without flattening', () => {
  assert.deepEqual(linkedVoiceSelection(['a', 'b'], sources, voices, []), [
    'va',
    'vb',
  ])
  assert.deepEqual(
    linkedVoiceSelection(['b'], sources, voices, [
      'va',
      'vb',
      'old',
      'ordinary',
    ]),
    ['ordinary', 'vb'],
  )
  assert.deepEqual(linkedVoiceSelection([], sources, voices, ['va', 'vb']), [])
  assert.deepEqual(
    linkedVoiceSelection(
      ['a'],
      [{ id: 'a', metadata: { bound_voice_id: 'vb' } }],
      voices,
      ['va'],
    ),
    [],
  )
})
test('removing a bound voice removes its source membership but preserves unrelated inputs', () => {
  assert.deepEqual(
    poolWithoutVoice(
      {
        source_media_ids: ['a', 'b'],
        voiceover_media_ids: ['va', 'vb', 'ordinary'],
      },
      ['va'],
      voices,
      sources,
    ),
    { source_media_ids: ['b'], voiceover_media_ids: ['vb', 'ordinary'] },
  )
})
test('flat pool accepts saved combinations but excludes recording trees and superseded voices', () => {
  assert.equal(
    isMediaPoolEligible('source', {
      kind: 'video',
      metadata: { role: 'raw_video', capture_group: { id: 'capture' } },
    }),
    false,
  )
  assert.equal(
    isMediaPoolEligible('source', {
      kind: 'video',
      metadata: {
        role: 'raw_video',
        composition_id: 'combo',
        composition_tree: [{}],
      },
    }),
    true,
  )
  assert.equal(
    isMediaPoolEligible('voiceover', {
      kind: 'audio',
      metadata: { role: 'tts_voice', binding_id: 'old' },
    }),
    false,
  )
  assert.equal(
    isMediaPoolEligible('voiceover', {
      kind: 'audio',
      metadata: {
        role: 'tts_voice',
        binding_id: 'active',
        bound_source_id: 'a',
      },
    }),
    true,
  )
})

test('either asset adds its active counterpart; removing sources removes only their paired voice', () => {
  const empty = { source_media_ids: [], voiceover_media_ids: [] }
  const paired = { source_media_ids: ['a'], voiceover_media_ids: ['va'] }
  assert.deepEqual(poolWithItems(empty, 'source', ['a'], sources, voices), paired)
  assert.deepEqual(poolWithItems(empty, 'voiceover', ['va'], sources, voices), paired)
  assert.deepEqual(poolWithoutSources(paired, ['a'], sources, voices), empty)
  assert.equal(activeVoiceSource(voices[0], sources)?.id, 'a')
  assert.equal(activeVoiceSource(voices[2], sources), null)
})
test('regeneration swaps selected voice by reciprocal binding without reviving historical audio', () => {
  const nextSources = [{ id: 'a', metadata: { bound_voice_id: 'new' } }]
  const nextVoices = [
    { id: 'va', metadata: { binding_id: 'old' } },
    { id: 'new', metadata: { binding_id: 'new', bound_source_id: 'a' } },
    { id: 'pending', metadata: { binding_id: 'pending' } },
  ]
  assert.deepEqual(linkedVoiceSelection(['a'], nextSources, nextVoices, ['va']), ['new'])
  assert.equal(activeVoiceSource(nextVoices[0], nextSources), null)
  assert.deepEqual(poolWithoutVoice(
    { source_media_ids: ['a'], voiceover_media_ids: ['new'] }, ['va'], nextVoices, nextSources,
  ), { source_media_ids: ['a'], voiceover_media_ids: ['new'] })
})
