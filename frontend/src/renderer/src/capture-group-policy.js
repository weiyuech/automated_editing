// A source stays a recording root throughout library, pool and edit requests.
export function captureGroup(item) { return item?.metadata?.capture_group || item?.capture_group || null }

export function selectableCaptureSegments(group) {
  const segments = group?.segments || []
  return group?.master_available
    ? segments
    : segments.filter((segment) => segment.available && segment.path)
}

export function normalizeCaptureSelection(group, selection) {
  if (!group || !selection) return null
  const selectable = selectableCaptureSegments(group)
  const requestedFull = Boolean(selection.include_full)
  const includeFull = Boolean(group.master_available && requestedFull)
  const selected = new Set(selection.segment_ids || [])
  // A persisted full-selection remains the user's request for all usable footage even if its
  // master disappears later. It must degrade to the available children, never to an empty pick.
  const segmentIds = (requestedFull ? selectable : selectable.filter((segment) => selected.has(segment.id)))
    .map((segment) => segment.id)
  if (!includeFull && !segmentIds.length) return null
  return { capture_id: group.id, include_full: includeFull, segment_ids: segmentIds }
}

export function fullCaptureSelection(group) {
  const selectable = selectableCaptureSegments(group)
  if (!group.master_available && !selectable.length) return null
  return {
    capture_id: group.id,
    include_full: Boolean(group.master_available),
    segment_ids: selectable.map((segment) => segment.id)
  }
}

export function captureSelectionState(group, selection) {
  const selectable = selectableCaptureSegments(group)
  const normalized = normalizeCaptureSelection(group, selection)
  const ids = new Set(normalized?.segment_ids || [])
  const count = selectable.filter((segment) => ids.has(segment.id)).length
  const full = Boolean(normalized?.include_full)
  const checked = full || (selectable.length > 0 && count === selectable.length)
  return {
    checked,
    partial: (full || count > 0) && !checked,
    whole: Boolean(group.master_available) && (full || (group.segments.length > 0 && count === group.segments.length)),
    count,
    selectableCount: selectable.length,
    duration: full ? group.duration : selectable.filter((segment) => ids.has(segment.id))
      .reduce((sum, segment) => sum + segment.end - segment.start, 0)
  }
}

export function changeCaptureChild(group, selection, id, checked) {
  const normalized = normalizeCaptureSelection(group, selection)
  const next = normalized ? { ...normalized, segment_ids: [...normalized.segment_ids] }
    : { capture_id: group.id, include_full: false, segment_ids: [] }
  if (id === 'full') {
    if (checked) return fullCaptureSelection(group)
    next.include_full = false
  }
  else {
    // A root-level full selection owns every child even though the backend also receives the
    // explicit ids. The first subtraction changes that meaning to "all except this child";
    // leaving include_full set would make the backend correctly ignore the subtraction.
    const selectable = selectableCaptureSegments(group)
    const selected = new Set(next.include_full ? selectable.map((segment) => segment.id) : next.segment_ids)
    if (!checked) next.include_full = false
    if (checked) selected.add(id)
    else selected.delete(id)
    next.segment_ids = selectable.filter((segment) => selected.has(segment.id)).map((segment) => segment.id)
  }
  return next.include_full || next.segment_ids.length ? next : null
}

/** Sources shown under one recorded session in the manual fine-tune picker.
 *
 * The child has its own UI identity so choosing it cannot collide with the recording root. The
 * timeline still claims the durable root media id and carries the child's real path separately;
 * the backend can then verify that exact pair against the capture manifest.
 */
export function captureTuneSourceGroups(items) {
  return (items || []).flatMap((item) => {
    const group = captureGroup(item)
    if (!group) return []
    const sources = []
    if (group.master_available) {
      sources.push({
        ...item,
        tune_label: `完整录制 · ${item.name || String(item.path || '').split(/[\\/]/).pop() || group.title}`
      })
    }
    for (const segment of group.segments || []) {
      if (!segment.available || !segment.path) continue
      sources.push({
        ...item,
        id: `capture-segment:${encodeURIComponent(item.id)}:${encodeURIComponent(segment.id)}`,
        media_id: item.id,
        path: segment.path,
        name: segment.label,
        tune_label: `${segment.label} · ${formatCaptureTime(segment.start)}–${formatCaptureTime(segment.end)}`,
        capture_segment: segment
      })
    }
    return sources.length
      ? [{ id: `capture-tune:${encodeURIComponent(group.id)}`, title: group.title, sources }]
      : []
  })
}

export function captureTuneClipIdentity(source) {
  return {
    media_id: source.media_id || source.id,
    source_path: source.path
  }
}

export function captureSearchText(item) {
  const group = captureGroup(item)
  return [item.path, item.name, group?.title, ...(group?.segments || []).map((s) => s.label)].join(' ').toLowerCase()
}

export function captureVaultRow(asset) {
  const group = captureGroup(asset)
  if (!group) return null
  const full = { ...asset, id: `${group.id}:full`, variant_label: '完整录制', can_preview: group.master_available, can_delete: group.master_available }
  const members = [full, ...group.segments.map((s) => ({
    ...asset, id: `${group.id}:${s.id}`, path: s.path || '', name: `${formatCaptureTime(s.start)}–${formatCaptureTime(s.end)} · ${captureStatus(s.status)}`,
    variant_label: s.label, size_bytes: s.size_bytes || 0, can_rename: false, can_delete: Boolean(s.available),
    can_preview: Boolean(s.available || group.master_available), capture_segment: s,
    master_path: group.master_path, modified_at: group.started_at || asset.modified_at
  }))]
  return { type: 'group', key: `capture:${group.id}`, role: 'raw_video', name: group.title, capture: group,
    modified_at: group.started_at || asset.modified_at, members,
    size_bytes: members.reduce((sum, member) => sum + member.size_bytes, 0) }
}

export function formatCaptureTime(seconds) {
  const value = Math.max(0, Number(seconds) || 0)
  return `${Math.floor(value / 60).toString().padStart(2, '0')}:${(value % 60).toFixed(1).padStart(4, '0')}`
}

export function captureStatus(status) {
  return { ready: '已生成', generating: '生成中', pending: '等待生成', failed: '生成失败', missing: '文件已移走', marker: '瞬时边界' }[status] || '等待生成'
}
