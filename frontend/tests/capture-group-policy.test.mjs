import assert from 'node:assert/strict'
import test from 'node:test'

import {
  captureSelectionState,
  captureTuneClipIdentity,
  captureTuneSourceGroups,
  changeCaptureChild,
  defaultCaptureSelection,
  normalizeCaptureSelection
} from '../src/renderer/src/capture-group-policy.js'

function group() {
  return {
    id: 'capture-1',
    title: '展厅巡游',
    duration: 30,
    master_available: true,
    segments: [
      { id: 'a', label: '开始准备', start: 0, end: 10, available: true, path: '/managed/a.mp4' },
      { id: 'b', label: '起点 → A', start: 10, end: 20, available: false, path: '/managed/b.mp4' },
      { id: 'c', label: 'A · 停留', start: 20, end: 30, available: true, path: '/managed/c.mp4' }
    ]
  }
}

test('subtracting a child from a full capture becomes an actual partial selection', () => {
  const capture = group()
  const selection = changeCaptureChild(capture, defaultCaptureSelection(capture), 'b', false)

  assert.deepEqual(selection, {
    capture_id: capture.id,
    include_full: false,
    segment_ids: ['a', 'c']
  })
  assert.deepEqual(captureSelectionState(capture, selection), {
    checked: false,
    partial: true,
    whole: false,
    count: 2,
    selectableCount: 3,
    duration: 20
  })
})

test('default selection uses available children rather than a missing master', () => {
  const capture = { ...group(), master_available: false }

  assert.deepEqual(defaultCaptureSelection(capture), {
    capture_id: capture.id,
    include_full: false,
    segment_ids: ['a', 'c']
  })
  assert.deepEqual(captureSelectionState(capture, defaultCaptureSelection(capture)), {
    checked: true,
    partial: false,
    whole: false,
    count: 2,
    selectableCount: 2,
    duration: 20
  })
})

test('missing master with all child files available selects every child', () => {
  const capture = {
    ...group(),
    master_available: false,
    segments: group().segments.map((segment) => ({ ...segment, available: true }))
  }

  assert.deepEqual(defaultCaptureSelection(capture), {
    capture_id: capture.id,
    include_full: false,
    segment_ids: ['a', 'b', 'c']
  })
  assert.equal(captureSelectionState(capture, defaultCaptureSelection(capture)).checked, true)
})

test('missing master root state is partial until every selectable child is selected', () => {
  const capture = { ...group(), master_available: false }
  const partial = { capture_id: capture.id, include_full: false, segment_ids: ['a'] }

  assert.deepEqual(captureSelectionState(capture, partial), {
    checked: false,
    partial: true,
    whole: false,
    count: 1,
    selectableCount: 2,
    duration: 10
  })
  assert.deepEqual(changeCaptureChild(capture, partial, 'c', true), defaultCaptureSelection(capture))
})

test('stale full selection is normalized when its master has gone missing', () => {
  const capture = { ...group(), master_available: false }
  const stale = { capture_id: capture.id, include_full: true, segment_ids: [] }

  assert.deepEqual(normalizeCaptureSelection(capture, stale), {
    capture_id: capture.id,
    include_full: false,
    segment_ids: ['a', 'c']
  })
  assert.equal(captureSelectionState(capture, stale).checked, true)
})

test('missing master without usable children has no default selection', () => {
  const capture = {
    ...group(),
    master_available: false,
    segments: group().segments.map((segment) => ({ ...segment, available: false }))
  }

  assert.equal(defaultCaptureSelection(capture), null)
  assert.deepEqual(captureSelectionState(capture, null), {
    checked: false,
    partial: false,
    whole: false,
    count: 0,
    selectableCount: 0,
    duration: 0
  })
})

test('default selection excludes preparation intervals but keeps shots and travel', () => {
  const capture = {
    ...group(),
    segments: [
      { id: 'a', kind: 'preparation', label: '开始准备', start: 0, end: 10, available: true, path: '/managed/a.mp4' },
      { id: 'b', kind: 'transit', label: '起点 → A', start: 10, end: 20, available: true, path: '/managed/b.mp4' },
      {
        id: 'c', kind: 'dwell', label: 'A · 停留', start: 20, end: 30, available: true, path: '/managed/c.mp4',
        children: [
          { id: 'c1', kind: 'shot', label: '原点 → 左', start: 20, end: 25, available: true, path: '/managed/c1.mp4' },
          { id: 'c2', kind: 'preparation', label: '镜头准备', start: 25, end: 30, available: true, path: '/managed/c2.mp4' }
        ]
      }
    ]
  }

  assert.deepEqual(defaultCaptureSelection(capture), {
    capture_id: capture.id,
    include_full: false,
    segment_ids: ['b', 'c1']
  })
})

test('child choices keep timeline order', () => {
  const capture = group()
  const firstClick = changeCaptureChild(capture, null, 'c', true)
  assert.deepEqual(changeCaptureChild(capture, firstClick, 'a', true)?.segment_ids, ['a', 'c'])
})

test('fine-tune keeps one whole input per recording; subsets go through composition', () => {
  const capture = group()
  const root = { id: 'root', path: '/managed/capture.mp4', metadata: { capture_group: capture } }
  const [result] = captureTuneSourceGroups([root])
  assert.equal(result.sources.length, 1)
  assert.deepEqual(captureTuneClipIdentity(result.sources[0]), { media_id: 'root', source_path: root.path })
  assert.deepEqual(captureTuneSourceGroups([{ ...root, metadata: { capture_group: { ...capture, master_available: false } } }]), [])
})
