<script setup>
import CaptureTreeNode from './CaptureTreeNode.vue'
import { formatCaptureTime } from '../capture-group-policy.js'

const props = defineProps({ group: { type: Object, required: true }, offset: [Number, String] })
const emit = defineEmits(['preview', 'retry', 'update:offset'])

function preview(segment = null) {
  emit('preview', {
    path: segment?.available ? segment.path : props.group.master_path,
    kind: 'video',
    metadata: { role: 'raw_video' },
    start_seconds: segment?.available ? 0 : (segment?.start || 0),
    end_seconds: segment ? (segment.available ? segment.end - segment.start : segment.end) : null,
  })
}
</script>

<template>
  <div class="capture-details">
    <div class="capture-full-row">
      <div class="capture-full-name">
        <strong>完整录制</strong>
        <small>{{ group.master_available ? '本次拍摄的全部画面' : '原片已移走或删除' }}</small>
      </div>
      <span class="capture-full-duration">{{ formatCaptureTime(group.duration) }}</span>
      <button :disabled="!group.master_available" @click="preview()">预览</button>
    </div>
    <div v-if="group.segments?.length" class="capture-detail-tree">
      <CaptureTreeNode v-for="segment in group.segments" :key="segment.id"
        :node="segment" :group="group" :selection="null" read-only @preview="preview" />
    </div>
    <p v-else class="capture-empty">尚无分段，可预览完整录制。</p>
    <p v-if="group.error" class="capture-error" role="alert">{{ group.error }}</p>
    <details class="capture-calibration">
      <summary>片段时间校准</summary>
      <p>{{ group.timing_note || '片段边界依据机器人反馈记录，可在此调整时间偏移。' }}</p>
      <p>所有切点统一调整：正数后移，负数前移。完整录制和已保存的组合不会改变。</p>
      <div class="capture-calibration-actions">
        <label>边界偏移（秒）<input type="number" min="-120" max="120" step="0.1"
          :value="offset ?? group.offset_seconds ?? 0" @input="emit('update:offset', $event.target.value)" /></label>
        <button :disabled="group.status === 'generating'" @click="emit('retry', true)">应用校准</button>
        <button :disabled="group.status === 'generating'" @click="emit('retry', false)">重新生成缺失片段</button>
      </div>
    </details>
  </div>
</template>

<style scoped>
.capture-details { grid-column: 1 / -1; min-width: 0; width: 100%; background: var(--tree-recording); border-bottom: 1px solid var(--border); }
.capture-full-row { display: flex; gap: 14px; align-items: center; padding: 13px 16px 13px calc(var(--capture-indent, 24px) + 22px); background: var(--tree-recording); }
.capture-full-name { flex: 1; min-width: 0; }
.capture-full-name strong { display: block; font-size: 12px; font-weight: 500; }
.capture-full-name small { display: block; color: var(--text-muted); font-size: 12px; margin-top: 4px; }
.capture-full-duration { color: var(--text-muted); font-size: 12px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.capture-details button { flex: none; padding: 6px 10px; font-size: 12px; white-space: nowrap; }
.capture-detail-tree { margin-left: var(--capture-indent, 24px); border-left: 1px solid var(--border); }
.capture-empty, .capture-error { margin: 0; padding: 0 16px 12px calc(var(--capture-indent, 24px) + 22px); color: var(--text-muted); font-size: 12px; }
.capture-error { color: var(--rose); }
.capture-calibration { border-top: 1px solid var(--border); padding: 0 16px 0 calc(var(--capture-indent, 24px) + 22px); color: var(--text-muted); font-size: 12px; }
.capture-calibration summary { padding: 11px 0; cursor: pointer; }
.capture-calibration p { margin: 0 0 12px; line-height: 1.6; }
.capture-calibration-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; padding-bottom: 14px; }
.capture-calibration label { display: flex; align-items: center; gap: 8px; }
.capture-calibration input { width: 76px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface); color: var(--text); padding: 6px; }
</style>
