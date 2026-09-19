// A source stays a recording root throughout library, pool and edit requests.
export function captureGroup(item) { return item?.metadata?.capture_group || item?.capture_group || null }

export function captureNodes(nodes) {
  return (nodes || []).flatMap(node => [node, ...captureNodes(node.children)])
}
export function captureLeaves(nodes) {
  return (nodes || []).flatMap(node => node.children?.length ? captureLeaves(node.children) : [node])
}
export function selectableCaptureSegments(group) {
  return captureNodes(group?.segments).filter(node => group.master_available || (node.available && node.path))
}
function coveredLeaves(group, selection) {
  const selected = new Set(selection?.segment_ids || [])
  function visit(nodes, inherited = false) {
    return (nodes || []).flatMap(node => {
      const on = inherited || selected.has(node.id) || selection?.include_full
      return node.children?.length ? visit(node.children, on) : (on ? [node] : [])
    })
  }
  return visit(group.segments)
}
export function normalizeCaptureSelection(group, selection) {
  if (!group || !selection) return null
  const selectable = selectableCaptureSegments(group)
  const includeFull = Boolean(group.master_available && selection.include_full)
  const ids = new Set(selection.segment_ids || [])
  const selected = (selection.include_full ? selectable : selectable.filter(node => ids.has(node.id))).map(node => node.id)
  if (!includeFull && !selected.length) return null
  return { capture_id: group.id, include_full: includeFull, segment_ids: selected }
}
export function fullCaptureSelection(group) {
  return normalizeCaptureSelection(group, { include_full: true, segment_ids: [] })
}
export function captureSelectionState(group, selection) {
  const normalized = normalizeCaptureSelection(group, selection)
  const leaves = captureLeaves(group.segments)
  const selected = coveredLeaves(group, normalized)
  const selectable = leaves.filter(node => group.master_available || (node.available && node.path))
  const full = Boolean(normalized?.include_full)
  const count = selected.length
  const checked = full || (selectable.length > 0 && count === selectable.length)
  return { checked, partial: count > 0 && !checked,
    whole: Boolean(group.master_available) && (full || (leaves.length > 0 && count === leaves.length)),
    count, selectableCount: selectable.length,
    duration: full ? group.duration : selected.reduce((sum, node) => sum + node.end - node.start, 0) }
}
export function captureNodeState(group, selection, node) {
  const selected = new Set(coveredLeaves(group, selection).map(n => n.id))
  const leaves = captureLeaves([node])
  const count = leaves.filter(n => selected.has(n.id)).length
  return { checked: count === leaves.length, partial: count > 0 && count < leaves.length }
}
export function changeCaptureChild(group, selection, id, checked) {
  if (id === 'full' && checked) return fullCaptureSelection(group)
  const normalized = normalizeCaptureSelection(group, selection)
  if (id === 'full') return normalizeCaptureSelection(group, { ...normalized, include_full: false })
  const selected = new Set(coveredLeaves(group, normalized).map(n => n.id))
  const node = captureNodes(group.segments).find(n => n.id === id)
  if (!node) return normalized
  for (const leaf of captureLeaves([node])) checked ? selected.add(leaf.id) : selected.delete(leaf.id)
  return normalizeCaptureSelection(group, { include_full: false, segment_ids: [...selected] })
}

/** Partial recordings must first be composed into one reviewed input. */
export function captureTuneSourceGroups(items) {
  return (items || []).flatMap(item => {
    const group = captureGroup(item)
    if (!group?.master_available) return []
    return [{ id: `capture-tune:${encodeURIComponent(group.id)}`, title: group.title,
      sources: [{ ...item, tune_label: `完整录制 · ${group.title}` }] }]
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
  return [item.path, item.name, group?.title, ...captureNodes(group?.segments).map((s) => s.label)].join(' ').toLowerCase()
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
  return { ready: '已生成', virtual: '原片区间', generating: '生成中', pending: '等待生成', failed: '生成失败', missing: '文件已移走', marker: '瞬时边界' }[status] || '等待生成'
}
