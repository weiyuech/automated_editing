import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, readFileSync, symlinkSync, writeFileSync } from 'node:fs'
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
import {
  captureLifecycleDecision,
  resolveCaptureStoppedMedia
} from '../src/renderer/src/capture-policy.js'
import {
  eligibleMediaIds,
  formatMediaImportOutcome,
  isMediaPoolEligible,
  MEDIA_IMPORT_DIALOG_BUTTONS,
  MEDIA_PICKER_EXTENSIONS,
  mediaImportModeForDialogResponse,
  mediaPoolEmptyMessage,
  mediaPoolInventory
} from '../src/shared/media-policy.js'

const appSource = readFileSync(new URL('../src/renderer/src/App.vue', import.meta.url), 'utf8')

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

test('desktop media picker advertises only formats supported by the in-app preview route', () => {
  assert.ok(MEDIA_PICKER_EXTENSIONS.includes('m4v'))
  assert.ok(MEDIA_PICKER_EXTENSIONS.includes('aac'))
  assert.ok(MEDIA_PICKER_EXTENSIONS.includes('flac'))
  assert.ok(MEDIA_PICKER_EXTENSIONS.includes('webp'))
  assert.ok(!MEDIA_PICKER_EXTENSIONS.includes('avi'), 'AVI has no supported in-app preview path')
})

test('copy/reference dialog keeps all three outcomes distinct', () => {
  assert.deepEqual([...MEDIA_IMPORT_DIALOG_BUTTONS], ['复制到应用（推荐）', '仅引用原文件', '取消'])
  assert.equal(mediaImportModeForDialogResponse(0), 'copy')
  assert.equal(mediaImportModeForDialogResponse(1), 'reference')
  assert.equal(mediaImportModeForDialogResponse(2), null)
  assert.equal(mediaImportModeForDialogResponse(-1), null)
  assert.equal(mediaImportModeForDialogResponse('0'), null)
})

test('images remain previewable but cannot enter automatic media pools', () => {
  const sourceVideo = { id: 'video', kind: 'video', path: '/media/input.mp4', metadata: { role: 'raw_video' } }
  const sourceImage = { id: 'image', kind: 'image', path: '/media/frame.webp', metadata: { role: 'image' } }
  const effectVideo = { id: 'effect-video', kind: 'video', path: '/data/seedance/effects/opening.mp4' }
  const effectImage = { id: 'effect-image', kind: 'image', path: '/data/seedance/effects/reference.png' }

  assert.equal(isMediaPoolEligible('source', sourceVideo), true)
  assert.equal(isMediaPoolEligible('source', sourceImage), false)
  assert.equal(isMediaPoolEligible('effect', effectVideo), true)
  assert.equal(isMediaPoolEligible('effect', effectImage), false)
  assert.deepEqual(
    eligibleMediaIds(
      ['video', 'image', 'video'],
      [sourceVideo, sourceImage],
      'source'
    ),
    ['video']
  )
  assert.match(appSource, /<img v-else-if="previewItem\.kind === 'image'"/)
  assert.match(
    appSource,
    /:disabled="isItemInPool\(mediaLibraryTab, item\.id\) \|\| !isMediaPoolEligible\(mediaLibraryTab, item\)"/
  )
})

test('library refresh warnings are not counted as failed imports', () => {
  const refreshedLate = formatMediaImportOutcome({
    successCount: 2,
    failures: [],
    storageMode: 'copy',
    refreshWarning: '服务暂不可用'
  })
  assert.equal(refreshedLate.kind, 'warn')
  assert.match(refreshedLate.text, /已导入 2 个文件/)
  assert.match(refreshedLate.text, /列表刷新失败/)
  assert.doesNotMatch(refreshedLate.text, /导入失败 1 个|均未导入/)

  const partial = formatMediaImportOutcome({
    successCount: 1,
    failures: ['broken.mov：格式错误'],
    storageMode: 'reference',
    refreshWarning: '服务暂不可用'
  })
  assert.equal(partial.kind, 'warn')
  assert.match(partial.text, /已导入 1 个，导入失败 1 个/)
})

test('no-record cruise capture lifecycle never becomes a manual recording or resurrects stale media', () => {
  const started = captureLifecycleDecision({
    type: 'CAPTURE_STARTED',
    data: { id: 'trial-capture' },
    nonRecordingCruisePending: true
  })
  assert.deepEqual(started, {
    ignore: true,
    nonRecordingCaptureSessionId: 'trial-capture'
  })

  const stopped = captureLifecycleDecision({
    type: 'CAPTURE_STOPPED',
    data: { id: 'trial-capture', active: false },
    // The terminal cruise event may arrive before the capture stop. The tracked id must
    // continue to own this event even though the run is no longer marked running.
    cruiseRun: { status: 'finished', recording: false },
    nonRecordingCaptureSessionId: started.nonRecordingCaptureSessionId
  })
  assert.deepEqual(stopped, {
    ignore: true,
    nonRecordingCaptureSessionId: ''
  })

  const explicitUnsaved = resolveCaptureStoppedMedia(
    { active: true, media_local_path: null, media_sync_error: null },
    { media_local_path: '/old/recording.mp4', media_sync_error: 'old error' }
  )
  assert.deepEqual(explicitUnsaved, {
    localPath: null,
    syncError: null,
    savePending: true
  })
})

test('pool inventory retains offline ids, explains the empty state, and keeps all clear buttons enabled', () => {
  const inventory = mediaPoolInventory(
    ['offline-a', 'available-a', 'offline-a'],
    [{ id: 'available-a' }]
  )
  assert.deepEqual(inventory, { total: 2, available: 1, offline: 1 })

  const offlineOnly = mediaPoolInventory(['offline-a', 'offline-b'], [])
  assert.equal(
    mediaPoolEmptyMessage(offlineOnly, '媒体池为空。'),
    '当前无可用素材，已保留 2 个离线素材，重连后恢复'
  )
  assert.equal(
    mediaPoolEmptyMessage(mediaPoolInventory([], []), '媒体池为空。'),
    '媒体池为空。'
  )

  for (const field of [
    'source_media_ids',
    'music_media_ids',
    'voiceover_media_ids',
    'effect_media_ids'
  ]) {
    assert.match(
      appSource,
      new RegExp(`:disabled="mediaPoolSaving \\|\\| !mediaPool\\.${field}\\.length"`)
    )
  }

  assert.match(appSource, /已入池 \{\{ mediaPool\.music_media_ids\.length }}/)
  assert.match(appSource, /已入池 \{\{ mediaPool\.voiceover_media_ids\.length }}/)
  assert.match(appSource, /已入池 \{\{ mediaPool\.effect_media_ids\.length }}/)
})
