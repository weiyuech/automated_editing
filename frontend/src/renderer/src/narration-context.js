export function narrationMediaName(item) {
  return item?.metadata?.title || item?.name || item?.path?.split(/[\\/]/).pop() || '未命名素材'
}

export function compositionDuration(item) {
  for (const value of [item?.metadata?.duration_seconds, item?.duration]) {
    const seconds = Number(value)
    if (Number.isFinite(seconds) && seconds > 0) return seconds
  }
  const end = Math.max(0, ...(item?.metadata?.composition_tree || []).map(node => Number(node.end) || 0))
  return end > 0 ? end : null
}
