<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import CaptureGroupPicker from './CaptureGroupPicker.vue'
import CompositionTree from './CompositionTree.vue'
import {
  captureGroup,
  fullCaptureSelection,
  formatCaptureTime,
} from '../capture-group-policy.js'
const props = defineProps({ api: Function, mediaUrl: Function })
const emit = defineEmits(['saved', 'preview-source'])
const items = ref([]),
  records = ref([]),
  record = ref(null),
  chosen = ref([]),
  choices = ref({})
const title = ref('我的组合'),
  search = ref(''),
  busy = ref(false),
  error = ref(''),
  message = ref('')
const reviewed = ref(false),
  video = ref(null),
  stopAt = ref(null),
  builtChoice = ref('')
let timer,
  disposed = false
const sources = computed(() =>
  items.value
    .filter(
      (i) =>
        i.kind === 'video' &&
        i.metadata?.role === 'raw_video' &&
        !i.metadata?.capture_input,
    )
    .sort(
      (a, b) =>
        Number(a.metadata.composition_order) -
          Number(b.metadata.composition_order) || a.path.localeCompare(b.path),
    ),
)
const visible = computed(() =>
  sources.value.filter((i) =>
    `${i.path} ${captureGroup(i)?.title || ''}`
      .toLowerCase()
      .includes(search.value.toLowerCase()),
  ),
)
const ordered = computed(() =>
  sources.value.filter((i) => chosen.value.includes(i.id)),
)
const request = computed(() => ({
  purpose: 'library',
  title: title.value,
  media_ids: ordered.value.map((i) => i.id),
  capture_selections: ordered.value
    .map((i) => choices.value[i.id])
    .filter(Boolean),
}))
const fingerprint = computed(() => JSON.stringify(request.value))
const clean = computed(
  () =>
    record.value?.status === 'ready' && builtChoice.value === fingerprint.value,
)
function label(i) {
  return captureGroup(i)?.title || i.path.split(/[\\/]/).pop()
}
async function safe(fn) {
  busy.value = true
  error.value = ''
  try {
    await fn()
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}
async function refresh() {
  const [m, r] = await Promise.all([
    props.api('/media'),
    props.api('/compositions'),
  ])
  items.value = m
  records.value = r.filter((c) => c.request.purpose === 'library')
}
function choose(item, value) {
  reviewed.value = false
  choices.value[item.id] = value
  chosen.value = chosen.value.filter((id) => id !== item.id)
  if (value) chosen.value.push(item.id)
}
function toggle(item, checked) {
  choose(
    item,
    checked
      ? captureGroup(item)
        ? fullCaptureSelection(captureGroup(item))
        : true
      : null,
  )
  if (!captureGroup(item)) delete choices.value[item.id]
}
function load(r) {
  record.value = r || null
  reviewed.value = false
  message.value = ''
  error.value = ''
  chosen.value = []
  choices.value = {}
  title.value = r?.title || '我的组合'
  if (r)
    for (const oldId of r.request.media_ids) {
      const item = items.value.find(
        (i) =>
          i.id === oldId ||
          i.path === r.source_paths?.[r.request.media_ids.indexOf(oldId)],
      )
      if (!item) continue
      chosen.value.push(item.id)
      const group = captureGroup(item)
      if (group)
        choices.value[item.id] =
          r.request.capture_selections.find((c) => c.capture_id === group.id) ||
          fullCaptureSelection(group)
    }
  builtChoice.value = fingerprint.value
}
async function build() {
  await safe(async () => {
    record.value = await props.api('/compositions', {
      method: 'POST',
      body: JSON.stringify(request.value),
    })
    builtChoice.value = fingerprint.value
    reviewed.value = false
    message.value = ''
    await refresh()
  })
}
async function save() {
  await safe(async () => {
    const item = await props.api(`/compositions/${record.value.id}/save`, {
      method: 'POST',
      body: JSON.stringify({ signature: record.value.signature }),
    })
    record.value = await props.api(`/compositions/${record.value.id}`)
    message.value = `已保存「${label(item)}」，在媒体库顶层可见。还可以继续选择并保存其他组合。`
    await refresh()
    emit('saved')
  })
}
async function addToPool() {
  await safe(async () => {
    const item = items.value.find(
      (i) =>
        i.metadata?.role === 'raw_video' &&
        i.metadata?.composition_id === record.value.id,
    )
    if (!item) throw new Error('组合文件暂不可用，请刷新媒体库。')
    const pool = await props.api('/media/pool')
    await props.api('/media/pool', {
      method: 'PUT',
      body: JSON.stringify({
        ...pool,
        source_media_ids: [...new Set([...pool.source_media_ids, item.id])],
      }),
    })
    message.value = '组合已加入源视频素材池；已绑定的旁白会一起加入。'
    emit('saved')
  })
}
async function discard() {
  await safe(async () => {
    await props.api(`/compositions/${record.value.id}`, { method: 'DELETE' })
    load(null)
    await refresh()
  })
}
function previewNode(node) {
  if (video.value) {
    stopAt.value = node.end
    video.value.currentTime = node.start
    video.value.play().catch((e) => {
      error.value = e.message
    })
  }
}
function timeUpdate() {
  if (stopAt.value !== null && video.value.currentTime >= stopAt.value) {
    video.value.pause()
    stopAt.value = null
  }
}
async function poll() {
  if (disposed) return
  try {
    if (['queued', 'building'].includes(record.value?.status))
      record.value = await props.api(`/compositions/${record.value.id}`)
  } catch (e) {
    error.value = e.message
  }
  if (!disposed) timer = setTimeout(poll, 1500)
}
onMounted(() => {
  safe(refresh)
  poll()
})
onUnmounted(() => {
  disposed = true
  clearTimeout(timer)
})
</script>
<template>
  <div class="controlled-composer">
    <header>
      <div>
        <strong>组合与预览</strong>
        <p>
          在录制树中选择画面，按原有顺序拼接。点位、镜头和点位之间的移动都可以选。
        </p>
      </div>
      <button :disabled="busy" @click="safe(refresh)">刷新</button>
    </header>
    <label class="history"
      >组合记录<select
        :value="record?.id || ''"
        :disabled="busy"
        @change="load(records.find((r) => r.id === $event.target.value))"
      >
        <option value="">新组合</option>
        <option v-for="r in records" :key="r.id" :value="r.id">
          {{ r.title }} · {{ formatCaptureTime(r.duration)
          }}{{ r.material_path ? ' · 已保存' : '' }}
        </option></select
      ><button
        v-if="record && !record.material_path"
        :disabled="busy || ['queued', 'building'].includes(record.status)"
        @click="discard"
      >
        移除预览
      </button></label
    >
    <div class="composer-grid">
      <section class="source-pane">
        <h3>选择画面</h3>
        <input v-model="search" class="field" placeholder="搜索录制或视频" />
        <div class="source-scroll">
          <template v-for="item in visible" :key="item.id">
            <CaptureGroupPicker
              v-if="captureGroup(item)"
              :group="captureGroup(item)"
              :model-value="choices[item.id] || null"
              :disabled="busy"
              @update:model-value="choose(item, $event)"
              @preview="emit('preview-source', $event)"
              @retry="
                safe(async () => {
                  await api(`/media/captures/${$event.id}/regenerate`, {
                    method: 'POST',
                    body: '{}',
                  })
                  await refresh()
                })
              "
            />
            <label v-else class="plain-source"
              ><input
                type="checkbox"
                :checked="chosen.includes(item.id)"
                :disabled="busy"
                @change="toggle(item, $event.target.checked)"
              /><span>{{ label(item) }}</span
              ><button @click.prevent="emit('preview-source', item)">
                预览
              </button></label
            >
          </template>
          <p v-if="!visible.length">暂无视频，请先导入或完成拍摄。</p>
        </div>
        <p>
          完整录制优先，重叠的父子画面只使用一次。跨录制按录制时间排列，录制内按树的先后顺序排列。
        </p>
      </section>
      <section class="review-pane">
        <label
          >组合名称<input
            v-model="title"
            class="field"
            maxlength="200"
            :disabled="busy"
        /></label>
        <button
          class="primary"
          :disabled="
            busy ||
            !chosen.length ||
            ['queued', 'building'].includes(record?.status)
          "
          @click="build"
        >
          {{
            record?.material_path ? '用当前选择生成另一个组合' : '生成组合预览'
          }}
        </button>
        <div v-if="record" class="review">
          <p>
            {{ record.message }}
            <strong v-if="record.duration"
              >· {{ formatCaptureTime(record.duration) }}</strong
            >
          </p>
          <progress
            v-if="['queued', 'building'].includes(record.status)"
            :value="record.progress"
            max="1"
          />
          <p v-if="record.error" class="error">{{ record.error }}</p>
          <template v-if="record.status === 'ready'"
            ><video
              ref="video"
              :src="
                mediaUrl(
                  items.find(
                    (i) =>
                      i.metadata?.role === 'raw_video' &&
                      i.metadata?.composition_id === record.id,
                  )?.path || record.preview_path,
                )
              "
              controls
              preload="metadata"
              @timeupdate="timeUpdate"
            />
            <details>
              <summary>查看各层画面与时长</summary>
              <CompositionTree
                :nodes="record.tree"
                :editable="false"
                @preview="previewNode"
              />
            </details>
            <p v-if="!clean" class="notice">
              选择或名称已改变，请生成新的组合预览后再保存。
            </p>
            <template v-if="!record.material_path"
              ><label class="confirm"
                ><input
                  v-model="reviewed"
                  type="checkbox"
                  :disabled="!clean || busy"
                />这就是本次要保存的组合</label
              ><button
                class="primary"
                :disabled="!clean || !reviewed || busy"
                @click="save"
              >
                确认，保存到媒体库
              </button></template
            >
            <template v-else
              ><p>此组合已独立保存。原录制树保留，可以继续保存不同组合。</p>
              <button :disabled="busy" @click="addToPool">
                加入媒体池
              </button></template
            >
          </template>
        </div>
      </section>
    </div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="message" class="notice">{{ message }}</p>
  </div>
</template>
<style scoped>
.controlled-composer {
  padding: 20px;
  border: 1px solid var(--border);
  border-radius: 14px;
  background: var(--surface);
}
header {
  display: flex;
  justify-content: space-between;
  gap: 16px;
}
header strong {
  font-size: 20px;
}
p {
  font-size: 12px;
  line-height: 1.7;
  color: var(--text-muted);
}
.history {
  display: flex;
  gap: 12px;
  align-items: center;
  margin: 16px 0;
  font-size: 13px;
}
.history select {
  flex: 1;
  max-width: 480px;
}
.composer-grid {
  display: grid;
  grid-template-columns: minmax(260px, 1fr) minmax(320px, 1.15fr);
  gap: 24px;
}
.source-pane {
  border-right: 1px solid var(--border);
  padding-right: 24px;
}
.source-scroll {
  max-height: 540px;
  overflow: auto;
  margin-top: 14px;
}
h3 {
  font-size: 14px;
  margin: 0 0 12px;
}
.plain-source {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px 0;
  border-bottom: 1px solid var(--border);
  font-size: 13px;
}
.plain-source span {
  flex: 1;
  overflow-wrap: anywhere;
}
.review-pane > label {
  display: block;
  font-size: 13px;
}
.review-pane > .primary {
  margin-top: 14px;
}
.review {
  margin-top: 16px;
}
video {
  width: 100%;
  max-height: 400px;
  background: #191721;
  border-radius: 9px;
}
summary {
  cursor: pointer;
  font-size: 13px;
  padding: 14px 0;
}
.confirm {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 18px 0;
  font-size: 13px;
}
progress {
  width: 100%;
}
select {
  padding: 8px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--surface);
  color: inherit;
}
.error {
  color: #b64453;
}
.notice {
  color: #766087;
}
@media (max-width: 1050px) {
  .composer-grid {
    grid-template-columns: 1fr;
  }
  .source-pane {
    border: 0;
    padding: 0;
  }
  .source-scroll {
    max-height: 320px;
  }
}
</style>
