<script setup>
import PreviewProgress from './PreviewProgress.vue'
import { isPreviewPending } from '../preview-progress.js'
import SubtitleFontPicker from './SubtitleFontPicker.vue'
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
const props = defineProps({
  active: { type: Boolean, default: true },
  api: Function,
  mediaUrl: Function,
  sources: Array,
  voices: Array,
  music: Array,
  intro: Array,
  outro: Array,
})
const previewVideo = ref(null)
watch(
  () => props.active,
  (active) => {
    if (!active) previewVideo.value?.pause()
  },
)
const emit = defineEmits(['created'])
const title = ref(''),
  mute = ref(true),
  subtitles = ref(true),
  cover = ref(false),
  font = ref('noto_sans_sc'),
  size = ref('medium'),
  fonts = ref([])
const records = ref([]),
  current = ref(''),
  accepted = ref({}),
  busy = ref(false),
  error = ref(''),
  built = ref(''),
  cancelling = ref({})
let timer,
  disposed = false,
  generation = 0
const recordRevisions = new Map()
const request = computed(() => ({
  title: title.value,
  media_ids: props.sources.map((i) => i.id),
  voiceover_media_ids: props.voices.map((i) => i.id),
  music_media_id: props.music[0]?.id || null,
  intro_effect_media_id: props.intro[0]?.id || null,
  outro_effect_media_id: props.outro[0]?.id || null,
  mute_original_audio: mute.value,
  subtitles: subtitles.value,
  effect_cover_audio: cover.value,
  subtitle_font: font.value,
  subtitle_size: size.value,
}))
const clean = computed(() => built.value === JSON.stringify(request.value))
const active = computed(() => records.value.find((r) => r.id === current.value))
const rendering = computed(() =>
  records.value.some(isPreviewPending),
)
const exportable = computed(() =>
  records.value.filter(
    (r) => r.status === 'ready' && !r.job_id && accepted.value[r.id],
  ),
)
function name(i) {
  return i.path.split(/[\\/]/).pop()
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
async function build() {
  const epoch = ++generation
  await safe(async () => {
    const input = request.value
    const created = await props.api('/studio/previews', {
      method: 'POST',
      body: JSON.stringify(input),
    })
    if (disposed || generation !== epoch) return
    records.value = created
    cancelling.value = {}
    recordRevisions.clear()
    built.value = JSON.stringify(input)
    current.value = records.value[0]?.id || ''
    accepted.value = {}
  })
}
async function confirm() {
  await safe(async () => {
    for (const record of exportable.value) {
      const job = await props.api(`/compositions/${record.id}/confirm`, {
        method: 'POST',
        body: JSON.stringify({ signature: record.signature }),
      })
      record.job_id = job.id
      emit('created', job)
    }
  })
}
async function cancel(record) {
  if (!isPreviewPending(record) || cancelling.value[record.id]) return
  const epoch = generation
  const revision = (recordRevisions.get(record.id) || 0) + 1
  recordRevisions.set(record.id, revision)
  cancelling.value[record.id] = true
  error.value = ''
  try {
    const updated = await props.api(`/compositions/${record.id}/cancel`, { method: 'POST' })
    if (!disposed && generation === epoch && records.value.includes(record)) {
      Object.assign(record, updated)
      delete accepted.value[record.id]
    }
  } catch (e) {
    if (!disposed && generation === epoch) error.value = e.message
  } finally {
    if (!disposed && generation === epoch) delete cancelling.value[record.id]
  }
}
async function poll() {
  if (disposed) return
  const epoch = generation
  await Promise.all(records.value.filter(record => isPreviewPending(record) && !cancelling.value[record.id]).map(async record => {
    const revision = recordRevisions.get(record.id) || 0
    const current = () => !disposed && generation === epoch && records.value.includes(record)
      && (recordRevisions.get(record.id) || 0) === revision
    try {
      const updated = await props.api(`/compositions/${record.id}`)
      if (current()) Object.assign(record, updated)
    } catch (e) {
      if (current()) error.value = e.message
    }
  }))
  if (!disposed) timer = setTimeout(poll, 1500)
}
onMounted(() => {
  safe(async () => {
    fonts.value = (await props.api('/subtitles/fonts')).fonts || []
  })
  poll()
})
onUnmounted(() => {
  disposed = true
  clearTimeout(timer)
})
</script>
<template>
  <section class="studio-pane workbench">
    <div class="panel-title studio-pane-title">自动剪辑工作台</div>
    <div class="automation-summary">
      <div>
        <small>源视频素材池</small><strong>{{ sources.length }} / 20</strong>
      </div>
      <div>
        <small>音乐池</small><strong>{{ music.length }} / 100</strong>
      </div>
      <div>
        <small>旁白池</small><strong>{{ voices.length }} / 100</strong>
      </div>
      <div>
        <small>特效池</small
        ><strong>片头 {{ intro.length }} · 片尾 {{ outro.length }}</strong>
      </div>
    </div>
    <div class="workbench-scroll">
      <div class="intro">
        <strong>以您确认的画面为准</strong>
        <p>
          请从左侧选用视频，每条独立成片，保留完整画面与顺序。机器人录制或跨点位拼接，请先在媒体库确认并保存组合。
        </p>
      </div>
      <div v-for="item in sources" :key="item.id" class="input-row">
        <span>{{ name(item) }}</span
        ><small
          >{{
            item.metadata?.duration_seconds
              ? `${Number(item.metadata.duration_seconds).toFixed(2)} 秒 · `
              : ''
          }}{{
            item.metadata?.bound_voice_missing
              ? '绑定旁白文件缺失'
              : item.metadata?.bound_voice_id
                ? '已带入绑定旁白'
                : item.metadata?.composition_id
                  ? '尚未绑定旁白'
                  : '普通视频'
          }}</small
        >
      </div>
      <label class="setting"
        ><span>成片名称前缀（可选）</span
        ><input
          v-model="title"
          class="field"
          maxlength="150"
          placeholder="默认沿用视频名称"
          :disabled="busy || rendering"
      /></label>
      <div class="settings">
        <label class="check-row"
          ><input
            v-model="mute"
            type="checkbox"
            :disabled="busy || rendering"
          />静音原视频声音</label
        ><label class="check-row"
          ><input
            v-model="cover"
            type="checkbox"
            :disabled="busy || rendering"
          />音乐覆盖片头、片尾</label
        ><label class="check-row"
          ><input
            v-model="subtitles"
            type="checkbox"
            :disabled="busy || rendering"
          />根据旁白生成字幕</label
        >
      </div>
      <div v-if="subtitles" class="subtitle-settings">
        <SubtitleFontPicker
          v-model="font"
          :fonts="fonts"
          :disabled="busy || rendering"
        />
        <label
          >字号<select v-model="size" class="field" :disabled="busy || rendering">
            <option value="small">小</option>
            <option value="medium">中</option>
            <option value="large">大</option>
          </select></label
        >
      </div>
      <p class="form-hint">
        画面有多长，主体就有多长；片头、片尾另计。音乐从头播放，长于成片就截取，短于成片则后段留白。组合旁白从主体开始，字幕跟随旁白。
      </p>
      <button
        class="primary preview-action"
        :disabled="busy || rendering || !sources.length"
        @click="build"
      >
        {{ rendering ? '正在生成预览…' : '生成成片预览' }}
      </button>
      <section v-if="records.length" class="previews">
        <PreviewProgress v-for="record in records.filter(item => isPreviewPending(item) || item.status === 'cancelled')" :key="record.id" :record="record" :cancelling="Boolean(cancelling[record.id])" :show-title="records.length > 1" @cancel="cancel(record)" />
        <label
          >查看预览<select v-model="current" class="field">
            <option v-for="r in records" :key="r.id" :value="r.id">
              {{ r.title }} ·
              {{
                r.job_id
                  ? '已提交导出'
                  : r.status === 'ready'
                    ? '待确认'
                    : r.status === 'cancelled'
                      ? '已取消'
                    : r.status === 'failed'
                      ? '失败'
                      : '生成中'
              }}
            </option>
          </select></label
        >
        <template v-if="active"
          ><p v-if="!isPreviewPending(active) && active.status !== 'cancelled'">
            {{ active.message
            }}<span v-if="active.duration">
              · {{ Number(active.duration).toFixed(2) }} 秒</span
            >
          </p>
          <p v-if="active.error" class="danger">{{ active.error }}</p>
          <p v-for="warning in active.warnings" :key="warning" class="warn">
            {{ warning }}
          </p>
          <video
            ref="previewVideo"
            v-if="active.status === 'ready'"
            :src="mediaUrl(active.preview_path)"
            controls
            preload="metadata"
          /><label v-if="active.status === 'ready'" class="check-row confirm"
            ><input
              v-model="accepted[active.id]"
              type="checkbox"
              :disabled="!clean || busy || Boolean(active.job_id)"
            />{{
              active.job_id
                ? '已提交导出，可在任务队列查看'
                : '已试听并检查，这是本次要导出的成片'
            }}</label
          ></template
        >
        <p v-if="!clean" class="warn">选用素材或设置已改变，请重新生成预览。</p>
        <button
          class="primary"
          :disabled="busy || !clean || !exportable.length"
          @click="confirm"
        >
          确认导出{{ exportable.length ? `（${exportable.length} 条）` : '' }}
        </button>
      </section>
      <p v-if="error" class="inline-status danger" role="alert">{{ error }}</p>
    </div>
  </section>
</template>
<style scoped>
.check-row {
  color: var(--text);
  font-size: 13px;
}
.automation-summary {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  border: 1px solid var(--border);
  border-radius: 10px;
  overflow: hidden;
  background: linear-gradient(120deg, #eee8ff77, #e8ebff66);
  margin-bottom: 18px;
}
.automation-summary > div {
  padding: 10px 14px;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.automation-summary > div:nth-child(odd) {
  border-right: 1px solid var(--border);
}
.automation-summary > div:nth-child(-n + 2) {
  border-bottom: 1px solid var(--border);
}
.automation-summary small {
  font-size: 12px;
  color: var(--text-muted);
}
.automation-summary strong {
  font-size: 14px;
}
.intro {
  padding: 16px;
  border: 1px solid var(--border);
  border-radius: 10px;
  margin-bottom: 14px;
}
.intro strong {
  font-size: 15px;
}
p {
  font-size: 12px;
  line-height: 1.7;
  color: var(--text-muted);
}
.input-row {
  padding: 10px 2px;
  border-bottom: 1px solid var(--border);
  font-size: 13px;
  overflow-wrap: anywhere;
}
.input-row small {
  display: block;
  color: var(--text-muted);
  margin-top: 5px;
}
.setting {
  display: block;
  margin: 18px 0;
  font-size: 13px;
}
.setting > span {
  display: block;
  margin-bottom: 8px;
}
.settings {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.subtitle-settings {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  margin: 16px 0;
  font-size: 13px;
}
.subtitle-settings select {
  margin-top: 6px;
}
.preview-action {
  width: 100%;
  margin: 10px 0;
}
.previews {
  border-top: 1px solid var(--border);
  margin-top: 15px;
  padding-top: 18px;
  font-size: 13px;
}
video {
  width: 100%;
  max-height: 320px;
  background: #191721;
  border-radius: 8px;
}
.confirm {
  margin: 16px 0;
}
progress {
  width: 100%;
}
.danger {
  color: #b64453;
}
.warn {
  color: #987332;
}
</style>
