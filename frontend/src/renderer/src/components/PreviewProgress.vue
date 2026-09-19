<script setup>
import { isPreviewPending, previewProgress, previewStageLabel, previewTime } from '../preview-progress.js'
defineProps({ record: { type: Object, required: true }, cancelling: Boolean, showTitle: Boolean })
defineEmits(['cancel'])
</script>
<template>
  <section class="preview-progress" :aria-label="`${record.title || '组合'}生成进度`">
    <div class="progress-heading">
      <span><strong v-if="showTitle">{{ record.title || '组合预览' }} · </strong>{{ previewStageLabel(record) }}<span v-if="isPreviewPending(record) && previewProgress(record) !== undefined"> · {{ Math.round(previewProgress(record) * 100) }}%</span></span>
      <button v-if="isPreviewPending(record)" :disabled="cancelling" @click="$emit('cancel')">{{ cancelling ? '正在取消…' : '取消生成' }}</button>
    </div>
    <progress v-if="isPreviewPending(record)" :value="previewProgress(record)" max="1" :aria-label="`${record.title || '组合'}生成进度`" />
    <div class="progress-detail">
      <span v-if="record.elapsed_seconds != null">已用 {{ previewTime(record.elapsed_seconds) }}</span>
      <span v-if="record.processed_seconds != null">已处理画面 {{ previewTime(record.processed_seconds) }}</span>
      <span v-if="record.encoder">{{ record.encoder === 'h264_nvenc' ? 'GPU 加速' : 'CPU 编码' }}</span>
    </div>
    <p v-if="record.message">{{ record.message }}</p>
    <details v-if="record.fallback_reason"><summary>编码说明</summary><p>{{ record.fallback_reason }}</p></details>
  </section>
</template>
<style scoped>
.preview-progress { padding:10px 12px; margin:10px 0; border:1px solid var(--border); border-radius:8px; background:#f1eafa; font-size:12px; }
.progress-heading { display:flex; justify-content:space-between; align-items:center; gap:10px; color:var(--text); }
.progress-heading span { min-width:0; overflow-wrap:anywhere; }
.progress-heading strong { font-weight:500; }
.progress-heading button { flex-shrink:0; padding:4px 8px; font-size:12px; }
progress { display:block; width:100%; height:6px; margin:9px 0; accent-color:#8964c5; }
.progress-detail { display:flex; flex-wrap:wrap; gap:4px 12px; color:var(--text-muted); margin-top:5px; }
p { margin:5px 0 0; color:var(--text-muted); font-size:12px; line-height:1.5; }
details { margin-top:5px; color:var(--text-muted); }
</style>
