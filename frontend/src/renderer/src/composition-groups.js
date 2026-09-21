import { compositionDuration, narrationMediaName } from './narration-context.js'

/** Provenance, not a place label or a filename, determines a recording group. */
export function recordingIds(item) {
  const metadata = item.metadata || {}
  if (metadata.source_recording_ids?.length) return [...new Set(metadata.source_recording_ids)].sort()
  function visit(node) {
    if (node.source_recording_ids?.length) return node.source_recording_ids
    const nested = (node.children || []).flatMap(visit)
    return nested.length ? nested : node.kind === 'recording' ? [`legacy:${node.id}`] : []
  }
  return [...new Set((metadata.composition_tree || []).flatMap(visit))].sort()
}

export function compositionGroupKey(item) {
  const origins = recordingIds(item)
  return origins.length === 1 ? origins[0] : `composition:${item.metadata.composition_id}`
}

export function savedCompositions(items) {
  return items.filter(item => item.kind === 'video' && item.metadata?.role === 'raw_video' && item.metadata.composition_id)
}

export function groupCompositions(items, originals = items) {
  const labels = new Map(originals.filter(item => item.metadata?.capture_group)
    .map(item => [`capture:${item.metadata.capture_group.id}`, item.metadata.capture_group.title]))
  const groups = new Map()
  // Explicit chronological order also makes automatic style assignment repeatable.
  const ordered = [...items].sort((a, b) => Number(a.metadata?.composition_order || 0) - Number(b.metadata?.composition_order || 0)
    || String(a.created_at || '').localeCompare(String(b.created_at || '')) || a.id.localeCompare(b.id))
  for (const item of ordered) {
    const key = compositionGroupKey(item)
    if (!groups.has(key)) {
      const roots = item.metadata?.composition_tree || []
      groups.set(key, {
        id: key,
        name: labels.get(key) || (recordingIds(item).length === 1 ? roots[0]?.label : null) || '跨录制组合',
        children: [],
      })
    }
    groups.get(key).children.push(item)
  }
  return [...groups.values()]
}

/** Selecting a new parent replaces the old selection; a parent never concatenates children. */
export function selectCompositionGroup(groups, selected, groupId, checked) {
  const ids = new Set(groups.find(group => group.id === groupId)?.children.map(item => item.id) || [])
  return checked ? [...ids] : selected.filter(id => !ids.has(id))
}

export function selectCompositionChild(groups, selected, id, checked) {
  if (!checked) return selected.filter(value => value !== id)
  const group = groups.find(group => group.children.some(item => item.id === id))
  if (!group) return selected
  const siblings = new Set(group.children.map(item => item.id))
  return [...new Set([...selected.filter(value => siblings.has(value)), id])]
}

export function compositionLabel(item) {
  // The on-disk filename follows Rename; the immutable creation title may be older.
  const filename = item?.name || item?.path?.split(/[\\/]/).pop()
  if (!filename) return narrationMediaName(item)
  const stem = filename.replace(/\.(mp4|mov|mkv|webm|avi|m4v)$/i, '')
  const suffix = ` 组合-${String(item.metadata?.composition_id || '').slice(0, 8)}`
  return stem.endsWith(suffix) ? stem.slice(0, -suffix.length) : stem
}
export function durationLabel(item) {
  const duration = compositionDuration(item)
  return duration === null ? '时长待读取' : `${duration.toFixed(1)} 秒`
}

export function compositionPieces(item) {
  const leaves = nodes => nodes.flatMap(node => node.children?.length ? leaves(node.children) : [node])
  return leaves(item.metadata?.composition_tree || [])
}
