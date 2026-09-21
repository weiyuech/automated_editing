export const DEFAULT_MUSIC_LABELS = Object.freeze(['轻快', '舒缓'])
export function musicLabels(item) {
  const tags = item?.metadata?.music_labels ?? item?.music_labels ?? []
  return [...new Set(tags.filter(tag => typeof tag === 'string' && tag.trim()))]
}
export function groupMusic(items) {
  const groups = new Map()
  for (const item of items || []) {
    for (const label of musicLabels(item).length ? musicLabels(item) : ['未分类']) {
      if (!groups.has(label)) groups.set(label, [])
      if (!groups.get(label).some(existing => existing.id === item.id)) groups.get(label).push(item)
    }
  }
  const order = [...DEFAULT_MUSIC_LABELS, ...[...groups.keys()].filter(label => ![...DEFAULT_MUSIC_LABELS, '未分类'].includes(label)).sort((a,b)=>a.localeCompare(b,'zh-CN')), '未分类']
  return order.filter(label => groups.has(label)).map(label => ({ label, items: groups.get(label) }))
}
export const isAudioFile = path => /\.(mp3|wav|aac|m4a|flac)$/i.test(path)
