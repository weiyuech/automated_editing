<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import ManualComposer from './ControlledComposer.vue'
import CompositionGroups from './CompositionGroups.vue'
import CompositionTree from './CompositionTree.vue'
import PreviewProgress from './PreviewProgress.vue'
import { compositionLabel, savedCompositions } from '../composition-groups.js'
const props = defineProps({ api: Function, mediaUrl: Function })
const emit = defineEmits(['saved', 'preview-source', 'narration'])
const mode = ref('auto'), items = ref([]), records = ref([]), selected = ref([])
const count = ref(20), transit = ref(false), plan = ref(null), busy = ref(false), planning = ref(false), error = ref('')
const previewId = ref(''), video = ref(null), stopAt = ref(null), reviewed = ref(false)
const activeBatch = ref('')
let disposed = false, timer, planTimer, planRevision = 0, recordsRevision = 0
const sources = computed(() => items.value.filter(item => item.kind === 'video' && item.metadata?.capture_group && !item.metadata?.capture_input && !item.metadata?.composition_id)
  .sort((a, b) => Number(a.metadata.composition_order) - Number(b.metadata.composition_order) || a.path.localeCompare(b.path)))
const input = computed(() => ({ media_ids: sources.value.filter(item => selected.value.includes(item.id)).map(item => item.id), count: count.value, include_transit: transit.value }))
const inputKey = computed(() => JSON.stringify(input.value))
const localError = computed(() => !selected.value.length ? '请选择录制。' : !Number.isInteger(count.value) || count.value <= selected.value.length ? '组合数量需大于所选录制数量。' : count.value > 200 ? '每次最多生成 200 条组合。' : '')
const savedById = computed(() => new Map(savedCompositions(items.value).map(item => [item.metadata.composition_id, item])))
const batchIds = computed(() => [...new Set(records.value.filter(record => record.automatic).map(record => record.automatic.batch_id))])
const automatic = computed(() => records.value.filter(record => record.automatic
  && (!activeBatch.value || record.automatic.batch_id === activeBatch.value)
  && (!record.material_path || savedById.value.has(record.id))))
const resultItems = computed(() => automatic.value.map(record => ({
  id: record.id, kind: 'video', name: savedById.value.has(record.id) ? compositionLabel(savedById.value.get(record.id)) : record.title,
  path: savedById.value.get(record.id)?.path || record.preview_path,
  created_at: String(record.started_at),
  metadata: { composition_id: record.id, title: record.title, composition_order: record.started_at,
    duration_seconds: record.duration, source_recording_ids: record.source_recording_ids || record.tree?.flatMap(node => node.source_recording_ids || []),
    composition_tree: record.tree || [] },
})))
const preview = computed(() => {
  const record = automatic.value.find(record => record.id === previewId.value)
  const saved = record && savedById.value.get(record.id)
  return saved ? { ...record, title: compositionLabel(saved), material_path: saved.path } : record
})
const unsaved = computed(() => automatic.value.filter(record => record.status === 'ready' && !record.material_path))
const pending = computed(() => records.value.some(record => record.automatic && ['queued', 'building'].includes(record.status)))
watch(inputKey, () => { reviewed.value = false; schedulePlan() })
watch(previewId, () => { stopAt.value = null })
watch(activeBatch, () => { reviewed.value = false; previewId.value = automatic.value[0]?.id || '' })
function schedulePlan() {
  clearTimeout(planTimer)
  const revision = ++planRevision
  plan.value = null
  if (localError.value) { planning.value = false; return }
  planning.value = true
  const body = JSON.stringify(input.value)
  planTimer = setTimeout(async () => {
    try {
      const data = await props.api('/compositions/automatic/plan', { method: 'POST', body })
      if (!disposed && revision === planRevision) { plan.value = data; error.value = '' }
    } catch (e) { if (!disposed && revision === planRevision) error.value = e.message }
    finally { if (revision === planRevision) planning.value = false }
  }, 300)
}
async function refresh() {
  const revision = ++recordsRevision
  const [media, combinations] = await Promise.all([props.api('/media'), props.api('/compositions')])
  if (!disposed && revision === recordsRevision) {
    items.value = media; records.value = combinations
    if (!activeBatch.value) activeBatch.value = batchIds.value[0] || ''
  }
}
async function action(fn) {
  recordsRevision++
  busy.value = true; error.value = ''
  try { await fn() } catch (e) { error.value = e.message } finally { busy.value = false }
}
async function generate() {
  if (localError.value || !plan.value?.feasible || planning.value || busy.value) return
  await action(async () => {
    const result = await props.api('/compositions/automatic', { method: 'POST', body: JSON.stringify({ ...input.value, plan_fingerprint: plan.value.fingerprint }) })
    records.value = [...result.records, ...records.value]
    activeBatch.value = result.batch_id
    previewId.value = result.records[0]?.id || ''
    reviewed.value = false
  })
}
async function saveReady() {
  if (!reviewed.value || busy.value) return
  const failures = []
  await action(async () => {
    for (const record of [...unsaved.value]) {
      try { await props.api(`/compositions/${record.id}/save`, { method: 'POST', body: JSON.stringify({ signature: record.signature }) }) }
      catch (e) { failures.push(`${record.title}：${e.message}`) }
    }
    await refresh(); emit('saved'); reviewed.value = false
    if (failures.length) throw new Error(failures.join('\n'))
  })
}
async function cancel(record) {
  await action(async () => { await props.api(`/compositions/${record.id}/cancel`, { method: 'POST' }); await refresh() })
}
function previewNode(node) {
  if (!video.value) return
  stopAt.value = node.end; video.value.currentTime = node.start
  video.value.play().catch(e => { error.value = e.message })
}
async function poll() {
  if (disposed) return
  if (pending.value && !busy.value) {
    const revision = recordsRevision
    try { const value = await props.api('/compositions'); if (!disposed && revision === recordsRevision) records.value = value }
    catch (e) { if (!disposed) error.value = e.message }
  }
  if (!disposed) timer = setTimeout(poll, 1500)
}
onMounted(async () => { await action(refresh); if (!disposed) poll() })
onUnmounted(() => { disposed = true; planRevision++; clearTimeout(timer); clearTimeout(planTimer) })
</script>
<template>
  <div class="grouped-composer">
    <div class="segmented mode-choice"><button :class="{active: mode === 'auto'}" :disabled="busy" @click="mode = 'auto'">自动组合</button><button :class="{active: mode === 'manual'}" :disabled="busy" @click="mode = 'manual'">手动组合</button></div>
    <ManualComposer v-if="mode === 'manual'" :api="api" :media-url="mediaUrl" @saved="refresh(); emit('saved')" @preview-source="emit('preview-source', $event)" />
    <template v-else>
      <div class="auto-layout">
        <section class="auto-sources"><h3>选择录制</h3><div class="source-scroll">
          <label v-for="item in sources" :key="item.id" class="recording-choice"><input v-model="selected" type="checkbox" :value="item.id" :disabled="busy" /><span>{{ item.metadata.capture_group.title }}<small>{{ item.metadata.capture_group.segments?.filter(node => node.kind === 'dwell').length || 0 }} 个点位</small></span></label>
          <p v-if="!sources.length" class="form-hint">完成机器人拍摄后，可在这里选择录制。</p>
        </div></section>
        <section class="auto-options"><h3>生成组合</h3>
          <label class="count-field">组合数量<input v-model.number="count" type="number" class="field" :min="selected.length + 1" :max="200" :disabled="busy" aria-label="组合数量" /></label>
          <label class="check-row"><input v-model="transit" type="checkbox" :disabled="busy" />包含点位间移动</label>
          <p class="form-hint">每个点位随机选一个片段，按拍摄顺序组合。</p>
          <p class="form-hint">按所选录制均匀分配，每条组合独立生成。</p>
          <div class="capacity-line">已选 {{ selected.length }} 次录制<span v-if="planning"> · 正在读取可用镜头…</span><span v-else-if="plan"> · 最多 {{ plan.available_count_exact }} 条不重复组合</span></div>
          <p v-if="localError" class="form-hint">{{ localError }}</p>
          <div v-if="plan && !plan.feasible" class="capacity-errors" role="alert"><p v-for="issue in plan.issues" :key="issue">{{ issue }}</p><p v-for="row in plan.recordings.filter(row => row.shortage || row.issues.length)" :key="row.media_id">{{ row.title }}：最多 {{ row.capacity_exact }} 条。{{ row.issues.join('；') }}</p><button v-if="plan.maximum_balanced_count >= plan.minimum_count" :disabled="busy" @click="count = plan.maximum_balanced_count">改为 {{ plan.maximum_balanced_count }} 条</button><p v-else>当前录制无法满足数量要求，请调整录制选择。</p></div>
          <button class="primary" :disabled="busy || pending || planning || !!localError || !plan?.feasible" @click="generate">{{ busy ? '正在提交…' : pending ? '正在生成组合…' : '生成组合' }}</button>
          <p class="form-hint">每条组合仅使用同一次录制的画面。</p>
        </section>
      </div>
      <div class="results-heading"><h3>组合结果 <small>{{ automatic.length }} 条</small></h3><button @click="emit('narration')">前往资产制作对应旁白</button></div>
      <label v-if="batchIds.length > 1" class="batch-history">组合记录 <select v-model="activeBatch" class="field" :disabled="busy"><option v-for="(id, index) in batchIds" :key="id" :value="id">{{ index === 0 ? '最近一次' : '较早的第 ' + index + ' 次' }} · {{ records.filter(record => record.automatic?.batch_id === id).length }} 条</option></select></label>
      <CompositionGroups :items="resultItems" :originals="items" @preview="previewId = $event.id">
        <template #actions="{item}"><button @click="previewId = item.id">{{ records.find(record => record.id === item.id)?.material_path ? '已保存 · 预览' : records.find(record => record.id === item.id)?.status === 'ready' ? '待保存 · 预览' : '查看进度' }}</button></template>
      </CompositionGroups>
      <section v-if="preview" class="preview-pane">
        <strong>{{ preview.title }}</strong><PreviewProgress :record="preview" :cancelling="busy" @cancel="cancel(preview)" />
        <p v-if="preview.error" class="danger" role="alert">{{ preview.error }}</p>
        <template v-if="preview.status === 'ready'"><video ref="video" :key="preview.id" :src="mediaUrl(preview.material_path || preview.preview_path)" controls preload="metadata" @timeupdate="if (stopAt !== null && video.currentTime >= stopAt) { video.pause(); stopAt = null }" /><details><summary>查看各层画面与时长</summary><CompositionTree :nodes="preview.tree" :editable="false" @preview="previewNode" /></details></template>
      </section>
      <div v-if="unsaved.length" class="save-row"><label class="check-row"><input v-model="reviewed" type="checkbox" :disabled="busy || pending" />这些就是本次要保存的组合</label><button class="primary" :disabled="!reviewed || busy || pending" @click="saveReady">确认，保存 {{ unsaved.length }} 条到媒体库</button></div>
      <p v-if="error" class="danger" role="alert">{{ error }}</p>
    </template>
  </div>
</template>
<style scoped>
.auto-sources > h3,
.auto-options > h3 {
  background: linear-gradient(110deg, #e3cefa 0%, #ebdcfc 52%, #f4eaff 100%);
  color: var(--text);
  border-bottom: 1px solid #dfccf3;
  box-shadow: inset 0 1px 0 #ffffff80;
}
.batch-history { display:flex; align-items:center; gap:12px; margin:12px 0; font-size:12px; }
.batch-history select { width:auto; max-width:320px; }
.check-row{color:var(--text);font-size:13px}.mode-choice{margin:16px 0}.auto-layout{display:grid;grid-template-columns:1fr 1fr;gap:22px;align-items:start}.auto-sources,.auto-options{border:1px solid var(--border);border-radius:12px;overflow:hidden;background:var(--tree-leaf,#f6f1fc)}h3{font-size:15px;margin:0;padding:13px 16px;background:var(--tree-root,#dfd0f3)}.source-scroll{max-height:400px;overflow:auto}.recording-choice{display:flex;align-items:center;gap:10px;padding:13px 16px;border-top:1px solid var(--border);background:var(--tree-branch,#eee4f8);font-size:15px}.recording-choice span{flex:1}.recording-choice small{float:right;font-size:12px;color:var(--text-muted)}.auto-options{padding-bottom:16px}.auto-options>label,.auto-options>p,.auto-options>.capacity-line,.auto-options>button,.capacity-errors{margin:16px}.count-field{display:grid;grid-template-columns:1fr 130px;align-items:center;font-size:13px}.capacity-line,.capacity-errors{font-size:12px;color:var(--text-soft)}.results-heading{display:flex;align-items:center;justify-content:space-between;margin:22px 0 10px}.results-heading h3{background:transparent;padding:0}.results-heading small{font-size:12px;font-weight:400;color:var(--text-muted);margin-left:8px}.preview-pane{margin:18px 0;padding:16px;background:var(--tree-leaf);border:1px solid var(--border);border-radius:10px;font-size:13px}.preview-pane video{display:block;width:100%;max-height:360px;margin:12px 0;background:#191721;border-radius:8px}.save-row{display:flex;justify-content:space-between;align-items:center;gap:12px;margin:20px 0}.danger{color:#b64453;font-size:12px;white-space:pre-wrap}@media(max-width:850px){.auto-layout{grid-template-columns:1fr}}
</style>
