import assert from 'node:assert/strict'
import test from 'node:test'

import {
  captureSelectionState,
  captureTuneClipIdentity,
  captureTuneSourceGroups,
  changeCaptureChild,
  fullCaptureSelection,
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
  const selection = changeCaptureChild(capture, fullCaptureSelection(capture), 'b', false)

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

  assert.deepEqual(fullCaptureSelection(capture), {
    capture_id: capture.id,
    include_full: false,
    segment_ids: ['a', 'c']
  })
  assert.deepEqual(captureSelectionState(capture, fullCaptureSelection(capture)), {
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

  assert.deepEqual(fullCaptureSelection(capture), {
    capture_id: capture.id,
    include_full: false,
    segment_ids: ['a', 'b', 'c']
  })
  assert.equal(captureSelectionState(capture, fullCaptureSelection(capture)).checked, true)
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
  assert.deepEqual(changeCaptureChild(capture, partial, 'c', true), fullCaptureSelection(capture))
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

  assert.equal(fullCaptureSelection(capture), null)
  assert.deepEqual(captureSelectionState(capture, null), {
    checked: false,
    partial: false,
    whole: false,
    count: 0,
    selectableCount: 0,
    duration: 0
  })
})

test('selecting the full row canonicalizes every child and child choices keep timeline order', () => {
  const capture = group()
  const partial = { capture_id: capture.id, include_full: false, segment_ids: ['c'] }

  assert.deepEqual(changeCaptureChild(capture, partial, 'full', true), fullCaptureSelection(capture))
  const firstClick = changeCaptureChild(capture, null, 'c', true)
  assert.deepEqual(changeCaptureChild(capture, firstClick, 'a', true)?.segment_ids, ['a', 'c'])
})

test('fine-tune capture children have unique UI ids but retain their root media id and real path', () => {
  const capture = group()
  const root = {
    id: 'root-media-id',
    name: 'capture.mp4',
    path: '/managed/capture.mp4',
    kind: 'video',
    metadata: { role: 'raw_video', capture_group: capture }
  }

  const [result] = captureTuneSourceGroups([root])
  assert.equal(result.title, capture.title)
  assert.equal(result.sources[0].id, root.id)
  assert.equal(result.sources[0].path, root.path)
  assert.deepEqual(
    result.sources.slice(1).map((source) => ({ id: source.id, media_id: source.media_id, path: source.path })),
    [
      { id: 'capture-segment:root-media-id:a', media_id: root.id, path: '/managed/a.mp4' },
      { id: 'capture-segment:root-media-id:c', media_id: root.id, path: '/managed/c.mp4' }
    ]
  )
  assert.equal(new Set(result.sources.map((source) => source.id)).size, result.sources.length)
  assert.deepEqual(captureTuneClipIdentity(result.sources[1]), {
    media_id: root.id,
    source_path: '/managed/a.mp4'
  })
})

test('fine-tune omits a missing master while keeping available child files', () => {
  const capture = { ...group(), master_available: false }
  const root = {
    id: 'offline-root',
    path: '/missing/master.mp4',
    kind: 'video',
    metadata: { role: 'raw_video', capture_group: capture }
  }

  const [result] = captureTuneSourceGroups([root])
  assert.deepEqual(result.sources.map((source) => source.path), ['/managed/a.mp4', '/managed/c.mp4'])
  assert.ok(result.sources.every((source) => source.media_id === root.id))
})
