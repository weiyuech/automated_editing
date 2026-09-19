export function isPreviewPending(record) {
  return ['queued', 'building'].includes(record?.status)
}

export function previewStageLabel(record) {
  if (record?.status === 'cancelled') return '已取消'
  if (record?.status === 'failed') return '生成失败'
  if (record?.status === 'ready') return '预览已就绪'
  return ({ preparing: '准备素材', waiting: '等待处理', encoding: '生成预览', verifying: '校验预览' })[record?.stage]
    || (record?.status === 'queued' ? '等待处理' : '准备素材')
}

export function previewProgress(record) {
  const value = record?.progress
  return typeof value === 'number' && Number.isFinite(value) ? Math.min(1, Math.max(0, value)) : undefined
}

export function previewTime(value) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return '—'
  const seconds = Math.floor(value)
  return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
}
