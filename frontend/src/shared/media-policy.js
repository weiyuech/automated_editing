export const MEDIA_PICKER_EXTENSIONS = Object.freeze([
  'mp4', 'mov', 'm4v', 'mkv', 'webm',
  'mp3', 'wav', 'aac', 'm4a', 'flac',
  'jpg', 'jpeg', 'png', 'webp'
])

export const MEDIA_IMPORT_DIALOG_BUTTONS = Object.freeze([
  '复制到应用（推荐）',
  '仅引用原文件',
  '取消'
])

export function mediaImportModeForDialogResponse(response) {
  if (response === 0) return 'copy'
  if (response === 1) return 'reference'
  return null
}

function pathContains(path, fragment) {
  return String(path || '').replaceAll('\\', '/').includes(fragment)
}

export function itemRole(item) {
  if (item?.metadata?.role) return item.metadata.role
  if (
    ['video', 'image'].includes(item?.kind)
    && pathContains(item.path, '/data/seedance/effects/')
  ) return 'seedance_effect'
  if (item?.kind === 'audio' && pathContains(item.path, '/data/tts/')) return 'tts_voice'
  if (item?.kind === 'video') return 'raw_video'
  if (item?.kind === 'audio') return 'music'
  if (item?.kind === 'image') return 'image'
  return 'unknown'
}

export function isMediaPoolEligible(poolKind, item) {
  const role = itemRole(item)
  if (poolKind === 'source') return item?.kind === 'video' && role === 'raw_video'
  if (poolKind === 'music') return item?.kind === 'audio' && role === 'music'
  if (poolKind === 'voiceover') return item?.kind === 'audio' && role === 'tts_voice'
  if (poolKind === 'effect') return item?.kind === 'video' && role === 'seedance_effect'
  return false
}

export function eligibleMediaIds(pendingIds, items, poolKind) {
  const byId = new Map((items || []).map((item) => [item.id, item]))
  return [...new Set(pendingIds || [])].filter((id) => {
    const item = byId.get(id)
    return Boolean(item && isMediaPoolEligible(poolKind, item))
  })
}

export function mediaPoolInventory(poolIds, availableItems) {
  const ids = [...new Set(poolIds || [])]
  const availableIds = new Set((availableItems || []).map((item) => item.id))
  const available = ids.filter((id) => availableIds.has(id)).length
  return {
    total: ids.length,
    available,
    offline: Math.max(0, ids.length - available)
  }
}

export function mediaPoolEmptyMessage(inventory, emptyMessage) {
  if ((inventory?.available || 0) === 0 && (inventory?.offline || 0) > 0) {
    return `当前无可用素材，已保留 ${inventory.offline} 个离线素材，重连后恢复`
  }
  return emptyMessage
}

export function formatMediaImportOutcome({
  successCount,
  failures = [],
  storageMode,
  refreshWarning = ''
}) {
  const imported = Math.max(0, Number(successCount) || 0)
  const storageText = storageMode === 'copy'
    ? '副本已保存到应用媒体库，原文件未改动'
    : '当前仅引用原文件；移动、改名或断开磁盘后会失效'
  const failureText = failures.slice(0, 3).join('；')
  const refreshText = refreshWarning
    ? `列表刷新失败，请点“重新扫描”：${refreshWarning}`
    : ''

  if (!failures.length) {
    return {
      kind: refreshWarning ? 'warn' : 'success',
      text: [`已导入 ${imported} 个文件。${storageText}。`, refreshText].filter(Boolean).join(' ')
    }
  }
  if (imported) {
    return {
      kind: 'warn',
      text: [
        `已导入 ${imported} 个，导入失败 ${failures.length} 个。${storageText}。失败详情：${failureText}`,
        refreshText
      ].filter(Boolean).join(' ')
    }
  }
  return {
    kind: 'danger',
    text: [`${failures.length} 个文件均未导入：${failureText}`, refreshText].filter(Boolean).join(' ')
  }
}
