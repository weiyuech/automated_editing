import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, symlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'

import {
  ensureDeletableManagedPath,
  ensureInspectableMediaPath
} from '../src/main/path-policy.js'
import {
  alignAudioBedToTimeline,
  hasBurnedSubtitleSource,
  renderableAudioBed
} from '../src/renderer/src/tune-policy.js'

function fixture() {
  const base = mkdtempSync(join(tmpdir(), 'ave-path-policy-'))
  const root = join(base, 'app')
  const exports = join(root, 'exports')
  const downloads = join(root, 'data', 'downloads')
  const previews = join(root, 'previews')
  const cache = join(root, '.cache')
  const seedanceCache = join(root, 'data', 'seedance', 'cache')
  const external = join(base, 'operator-video.mp4')
  for (const path of [exports, downloads, previews, cache, seedanceCache]) {
    mkdirSync(path, { recursive: true })
  }
  writeFileSync(external, 'operator-owned')
  return { base, root, exports, downloads, previews, cache, seedanceCache, external }
}

test('external imported media can be inspected but never physically deleted', () => {
  const { root, external } = fixture()
  assert.equal(ensureInspectableMediaPath(root, external), external)
  assert.throws(
    () => ensureDeletableManagedPath(root, external),
    /Only app-managed media files/
  )
})

test('generated media and its exact companion can be deleted', () => {
  const { root, exports } = fixture()
  const video = join(exports, '成片.mp4')
  const sidecar = join(exports, '成片.subtitles.json')
  writeFileSync(video, 'generated')
  writeFileSync(sidecar, '{}')
  assert.equal(ensureDeletableManagedPath(root, video), video)
  assert.equal(ensureDeletableManagedPath(root, sidecar, true), sidecar)
  assert.throws(() => ensureDeletableManagedPath(root, sidecar), /Only app-managed media files/)
})

test('ordinary files in managed preview and cache roots can be deleted', () => {
  const { root, previews, cache, seedanceCache } = fixture()
  const files = [
    join(previews, 'frame.tmp'),
    join(cache, 'analysis.bin'),
    join(seedanceCache, 'source-frame.tmp')
  ]
  for (const path of files) {
    writeFileSync(path, 'generated')
    assert.equal(ensureDeletableManagedPath(root, path), path)
  }
})

test('a capture sidecar cannot be deleted as a primary asset', () => {
  const { root, downloads } = fixture()
  const sidecar = join(downloads, 'capture.mp4.capture.json')
  writeFileSync(sidecar, '{}')

  assert.throws(
    () => ensureDeletableManagedPath(root, sidecar),
    /Only app-managed media files/
  )
  assert.equal(ensureDeletableManagedPath(root, sidecar, true), sidecar)
})

test('a symlink under exports cannot escape the managed delete boundary', (context) => {
  const { root, exports, external } = fixture()
  const link = join(exports, 'linked.mp4')
  try {
    symlinkSync(external, link)
  } catch (error) {
    if (error?.code === 'EPERM') {
      context.skip('this Windows host does not grant symlink creation')
      return
    }
    throw error
  }
  assert.throws(
    () => ensureDeletableManagedPath(root, link),
    /Only app-managed media files/
  )
})

test('a retained soundtrack keeps a burned export blocked after its picture is removed', () => {
  const burned = {
    id: 'delivery',
    path: '/managed/exports/delivery.mp4',
    metadata: { has_burned_subtitles: true }
  }
  const bed = { source_path: burned.path }

  assert.equal(hasBurnedSubtitleSource([], bed, [burned]), true)
  assert.equal(
    hasBurnedSubtitleSource([], bed, [{ ...burned, metadata: { has_burned_subtitles: false } }]),
    false
  )
})

test('the retained soundtrack follows its anchor when clips are inserted, removed, or reordered', () => {
  const source = { uid: 1, duration: 8 }
  const intro = { uid: 2, duration: 3 }
  const bed = {
    source_path: '/managed/exports/master.mp4',
    source_start: 0,
    timeline_start: 0,
    has_voiceover: true,
    _anchorUid: source.uid,
    _anchorOffset: 0
  }

  const afterInsert = alignAudioBedToTimeline([intro, source], bed, 3)
  assert.equal(afterInsert.timeline_start, 3)

  const afterRemove = alignAudioBedToTimeline([source], afterInsert, 0)
  assert.equal(afterRemove.timeline_start, 0)

  const afterReorder = alignAudioBedToTimeline([intro, source], afterRemove, 3)
  assert.equal(afterReorder.timeline_start, 3)
  assert.equal(alignAudioBedToTimeline([source, intro], afterReorder, 3).timeline_start, 0)
})

test('deleting the soundtrack anchor never leaves retained audio parked at end of timeline', () => {
  const intro = { uid: 2, duration: 3 }
  const bed = {
    source_path: '/managed/exports/master.mp4',
    source_start: 0,
    timeline_start: 3,
    has_voiceover: true,
    _anchorUid: 1,
    _anchorOffset: 0
  }

  const aligned = alignAudioBedToTimeline([intro], bed, 3)
  assert.equal(aligned.timeline_start, 0)
  assert.equal(aligned._anchorUid, intro.uid)
  assert.deepEqual(renderableAudioBed(aligned), {
    source_path: bed.source_path,
    source_start: 0,
    timeline_start: 0,
    has_voiceover: true
  })
})
