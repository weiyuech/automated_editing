import test from 'node:test'
import assert from 'node:assert/strict'
import {
  linkedVoiceSelection,
  poolWithoutVoice,
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
    ['vb'],
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
