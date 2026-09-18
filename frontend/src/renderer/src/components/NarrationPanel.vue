<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
const props = defineProps({
  api: Function,
  mediaUrl: Function,
  active: { type: Boolean, default: true },
})
const panel = ref(null)
watch(
  () => props.active,
  (active) => {
    if (!active)
      panel.value?.querySelectorAll('video, audio').forEach((media) => media.pause())
  },
)
const emit = defineEmits(['changed'])
const aligned = ref(true),
  useLlm = ref(true),
  items = ref([]),
  composition = ref(''),
  title = ref(''),
  source = ref('')
const draft = ref(''),
  draftSections = ref([]),
  version = ref(''),
  result = ref(null),
  busy = ref(false),
  error = ref(''),
  info = ref(''),
  quota = ref(null)
let timer,
  disposed = false,
  revision = 0
const combinations = computed(() =>
  items.value.filter(
    (i) =>
      i.kind === 'video' &&
      i.metadata?.role === 'raw_video' &&
      i.metadata?.composition_id,
  ),
)
const selected = computed(() =>
  combinations.value.find((i) => i.metadata.composition_id === composition.value),
)
const voices = computed(() =>
  items.value
    .filter((i) => i.metadata?.role === 'tts_voice')
    .sort((a, b) =>
      String(b.metadata?.created_at || b.created_at || '').localeCompare(
        String(a.metadata?.created_at || a.created_at || ''),
      ),
    ),
)
const text = computed(() =>
  !useLlm.value || version.value === 'source'
    ? source.value
    : version.value === 'draft'
      ? draft.value
      : '',
)
const running = computed(() => result.value?.status === 'running')
const canGenerate = computed(
  () =>
    !busy.value &&
    !running.value &&
    text.value.trim() &&
    text.value.length <= 4000 &&
    (!aligned.value || selected.value),
)
const playbackRate = ref(1),
  autoTempo = ref(true),
  accepted = ref(false)
const voiceSearch = ref(''),
  voiceFilter = ref('all'),
  auditionId = ref('')
const visibleVoices = computed(() =>
  voices.value.filter(
    (voice) =>
      name(voice).toLowerCase().includes(voiceSearch.value.toLowerCase()) &&
      (voiceFilter.value !== 'current' ||
        voice.metadata?.composition_id === composition.value),
  ),
)
const audition = computed(() =>
  voices.value.find((voice) => voice.id === auditionId.value),
)
const scriptChanged = computed(() => {
  const current =
    !useLlm.value || version.value === 'source'
      ? source.value
      : draft.value || source.value
  return Boolean(
    result.value?.text && current.trim() && current !== result.value.text,
  )
})
const settingsChanged = computed(
  () =>
    result.value &&
    (playbackRate.value !== result.value.playback_rate ||
      autoTempo.value !== result.value.auto_tempo),
)
watch([() => result.value?.review_id, text, playbackRate, autoTempo], () => {
  accepted.value = false
})
watch([aligned, composition], () => {
  accepted.value = false
  auditionId.value = ''
})
function restoreSettings() {
  playbackRate.value = result.value?.playback_rate ?? 1
  autoTempo.value = result.value?.auto_tempo ?? true
}
async function adjust() {
  await safe(async () => {
    result.value = await props.api(
      `/compositions/${composition.value}/narration/adjust`,
      {
        method: 'POST',
        body: JSON.stringify({
          attempt_id: result.value.attempt_id,
          playback_rate: playbackRate.value,
          auto_tempo: autoTempo.value,
        }),
      },
    )
    accepted.value = false
  })
}
async function editNarration() {
  const previous = result.value
  source.value = previous.text
  await nextTick()
  if (useLlm.value) {
    draft.value = previous.text
    draftSections.value = previous.sections || []
    version.value = 'draft'
  }
  info.value = '可以直接修改本次文案，也可以继续润色；修改后需重新合成。'
}
async function confirmNarration() {
  await safe(async () => {
    result.value = await props.api(
      `/compositions/${composition.value}/narration/confirm`,
      {
        method: 'POST',
        body: JSON.stringify({
          attempt_id: result.value.attempt_id,
          review_id: result.value.review_id,
        }),
      },
    )
    await refresh()
    emit('changed')
  })
}
function exclusivePlayback(event) {
  panel.value?.querySelectorAll('audio, video').forEach((media) => {
    if (media !== event.target) media.pause()
  })
}

function name(i) {
  return i.path.split(/[\\/]/).pop()
}
function seconds(n) {
  return Number(n || 0).toFixed(2)
}
async function refresh() {
  items.value = await props.api('/media')
  try {
    quota.value = await props.api('/tts/quota')
  } catch {}
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
watch(
  () => props.active,
  (active) => {
    if (active && !busy.value) safe(refresh)
  },
)
watch([source, aligned, composition, useLlm], () => {
  revision++
  draft.value = ''
  draftSections.value = []
  version.value = ''
  info.value = ''
})
watch([aligned, composition], async () => {
  const key = composition.value
  result.value = null
  if (aligned.value && key)
    try {
      const record = await props.api(`/compositions/${key}`)
      if (composition.value === key && aligned.value) {
        result.value = record.narration || null
        restoreSettings()
      }
    } catch (e) {
      error.value = e.message
    }
})
async function polish(measured = false) {
  const requested = ++revision
  await safe(async () => {
    const inputText =
      draft.value || text.value || source.value || result.value?.text || ''
    const data = aligned.value
      ? await props.api(`/compositions/${composition.value}/narration/draft`, {
          method: 'POST',
          body: JSON.stringify({
            text: inputText,
            measured_feedback: measured,
          }),
        })
      : await props.api('/tts/draft', {
          method: 'POST',
          body: JSON.stringify({ text: inputText }),
        })
    if (requested !== revision) return
    draft.value = data.text || data.draft_text
    draftSections.value = data.sections || []
    version.value = ''
    info.value = '请审阅整篇文案，可以继续修改，再选择用于配音的版本。'
  })
}
async function generate() {
  await safe(async () => {
    if (aligned.value)
      result.value = await props.api(
        `/compositions/${composition.value}/narration`,
        {
          method: 'POST',
          body: JSON.stringify({
            text: text.value,
            sections: version.value === 'draft' ? draftSections.value : [],
            playback_rate: playbackRate.value,
            auto_tempo: autoTempo.value,
          }),
        },
      )
    else {
      const data = await props.api('/tts/generate', {
        method: 'POST',
        body: JSON.stringify({
          title: title.value || '旁白',
          text: text.value,
          use_llm: false,
        }),
      })
      result.value = {
        ...data.asset,
        status: 'ready',
        actual_seconds: data.asset.duration_ms / 1000,
        message: '完整旁白已生成，可加入普通视频的旁白池。',
      }
      await refresh()
      emit('changed')
    }
  })
}
async function poll() {
  if (disposed) return
  try {
    if (aligned.value && composition.value && running.value) {
      const key = composition.value
      const data = await props.api(`/compositions/${key}`)
      if (composition.value === key) {
        result.value = data.narration
        if (!running.value) {
          await refresh()
          emit('changed')
        }
      }
    }
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
  <section
    ref="panel"
    class="panel wide narration-panel"
    @play.capture="exclusivePlayback"
  >
    <div class="panel-title">旁白制作</div>
    <div class="mode-row">
      <label class="check-row"
        ><input
          v-model="aligned"
          type="checkbox"
          role="switch"
          :disabled="busy || running"
        />按组合画面对齐</label
      ><span>{{
        aligned ? '一份组合，绑定一条完整旁白' : '为普通导入视频制作旁白'
      }}</span>
    </div>
    <div class="narration-layout">
      <div>
        <label v-if="aligned" class="field-row"
          ><span>已保存的组合</span
          ><select v-model="composition" class="field" :disabled="busy || running">
            <option value="">请选择媒体库中的组合</option>
            <option
              v-for="item in combinations"
              :key="item.id"
              :value="item.metadata.composition_id"
            >
              {{ name(item) }} · {{ seconds(item.metadata.duration_seconds) }} 秒
            </option>
          </select></label
        >
        <input
          v-else
          v-model="title"
          class="field"
          placeholder="旁白名称"
          :disabled="busy"
        />
        <p v-if="aligned && !combinations.length" class="form-hint">
          先在媒体库预览并保存组合，再制作与它对应的旁白。
        </p>
        <p v-if="selected && aligned" class="form-hint">
          画面
          {{ seconds(selected.metadata.duration_seconds) }}
          秒。拍摄备注和点位顺序会作为润色参考；移动画面可以串场，也可以留白。
        </p>
        <label class="script"
          ><span>原始文案 / 改写要求</span
          ><textarea
            v-model="source"
            class="field text"
            maxlength="8000"
            :disabled="busy || running"
            placeholder="输入准确的介绍内容，也可以说明哪些画面要讲、哪些留白。"
          />
        </label>
        <label class="check-row"
          ><input
            v-model="useLlm"
            type="checkbox"
            :disabled="busy || running"
          />大模型润色（先审阅）</label
        >
        <div class="actions">
          <button
            v-if="useLlm"
            :disabled="
              busy ||
              running ||
              !(draft.trim() || source.trim()) ||
              (aligned && !selected)
            "
            @click="polish()"
          >
            {{
              busy ? '处理中…' : draft ? '基于当前改写稿再润色' : '生成整篇改写稿'
            }}</button
          ><small v-if="quota?.remaining != null"
            >今日剩余 {{ quota.remaining }} / {{ quota.limit }} 次</small
          >
        </div>
        <section v-if="useLlm && draft" class="draft">
          <strong>审阅改写稿（可直接修改）</strong
          ><textarea
            v-model="draft"
            aria-label="审阅改写稿"
            class="field text"
            maxlength="4000"
            :disabled="busy || running"
          />
          <div class="actions">
            <button
              :class="{ active: version === 'source' }"
              :disabled="busy || running"
              @click="version = 'source'"
            >
              保留原文</button
            ><button
              :class="{ active: version === 'draft' }"
              :disabled="busy || running"
              @click="version = 'draft'"
            >
              采用改写稿
            </button>
          </div>
          <p>
            {{
              version === 'source'
                ? '将使用原文'
                : version === 'draft'
                  ? '将使用当前改写稿'
                  : '请选择要合成的版本'
            }}
          </p>
        </section>
        <div v-if="aligned" class="tempo-settings">
          <label
            >基础播放速度
            <select
              v-model.number="playbackRate"
              class="field"
              :disabled="busy || running"
            >
              <option :value="0.9">0.9 倍 · 稍慢</option>
              <option :value="1">1.0 倍 · 原速</option>
              <option :value="1.1">1.1 倍 · 稍快</option>
            </select>
          </label>
          <label class="check-row"
            ><input
              v-model="autoTempo"
              type="checkbox"
              :disabled="busy || running"
            />允许在 0.9～1.1 倍内微调，以适应画面</label
          >
        </div>
        <button class="primary" :disabled="!canGenerate" @click="generate">
          {{ running ? '正在合成并测量…' : '合成一条完整旁白' }}
        </button>
        <p class="form-hint">
          整篇只合成一次。修改文案后需重新合成；调整播放速度可复用已有音频。超过微调范围时会提示精简文案。
        </p>
        <p v-if="info" class="inline-status">{{ info }}</p>
        <p v-if="error" class="inline-status danger" role="alert">
          {{ error }}
        </p>
      </div>
      <aside>
        <video
          v-if="aligned && selected"
          :key="result?.preview_path || selected.path"
          :src="mediaUrl(result?.preview_path || selected.path)"
          controls
          preload="metadata"
        />
        <section v-if="result" class="result">
          <strong>{{ result.message }}</strong
          ><progress v-if="running" max="1" :value="result.progress" />
          <p v-if="result.actual_seconds != null">
            语音 {{ seconds(result.actual_seconds) }} 秒<span v-if="aligned">
              / 画面 {{ seconds(result.available_seconds) }} 秒</span
            >
          </p>
          <audio
            v-if="result.audio_path && !result.preview_path"
            :src="mediaUrl(result.audio_path)"
            controls
          />
          <details v-if="result.text">
            <summary>查看本次合成文案</summary>
            <p class="saved-script">{{ result.text }}</p>
            <button :disabled="busy || running" @click="editNarration">
              继续编辑这版文案
            </button>
          </details>
          <p v-if="result.error" class="danger">{{ result.error }}</p>
          <p v-for="warning in result.warnings || []" :key="warning" class="warn">
            {{ warning }}
          </p>
          <p v-if="aligned && result.preview_path">
            上方视频已包含这条旁白，可直接同步试听。
          </p>
          <p v-if="result.playback_blocks?.length" class="rate-info">
            本次播放速度：{{
              [
                ...new Set(
                  result.playback_blocks.map((b) => Number(b.rate).toFixed(2)),
                ),
              ].join(' / ')
            }}
            倍
          </p>
          <p v-if="aligned && scriptChanged" class="warn">
            当前文案已修改，请重新合成后试听。
          </p>
          <template v-if="aligned && result.attempt_id && result.status !== 'ready'">
            <button
              v-if="result.source_seconds"
              :disabled="busy || running || scriptChanged"
              @click="adjust"
            >
              按当前速度重新试听（不重新合成）
            </button>
            <p
              v-if="settingsChanged && result.status === 'pending_review'"
              class="warn"
            >
              速度设置已改变，请先重新生成试听。
            </p>
            <label
              v-if="result.status === 'pending_review'"
              class="check-row accept"
            >
              <input
                v-model="accepted"
                type="checkbox"
                :disabled="busy || settingsChanged || scriptChanged"
              />我已试听，确认使用这条旁白
            </label>
            <button
              v-if="result.status === 'pending_review'"
              class="primary"
              :disabled="busy || !accepted || settingsChanged || scriptChanged"
              @click="confirmNarration"
            >
              确认应用此旁白
            </button>
          </template>
          <p v-if="aligned && result.status === 'ready'">
            已绑定此组合。加入媒体池、选用和导出时会跟随画面。
          </p>
          <button
            v-if="aligned && useLlm && result.actual_seconds != null"
            :disabled="
              busy || running || !(draft.trim() || source.trim() || result.text)
            "
            @click="polish(true)"
          >
            参考实测结果重新润色
          </button>
          <details v-if="result.mappings?.length">
            <summary>
              内容匹配
              {{
                result.semantic_evidence === 'bge-small-zh-v1.5-int8'
                  ? '· BAAI 已检查'
                  : '· 段落参考'
              }}
            </summary>
            <p v-for="(mapping, index) in result.mappings" :key="index">
              {{ mapping.label }} · {{ mapping.text }}<br />{{
                mapping.warning ||
                (mapping.method === 'reference'
                  ? '保留段落绑定'
                  : mapping.method === 'semantic'
                    ? '语义匹配，建议试听确认'
                    : '未确定对应画面，请试听核对')
              }}
            </p>
          </details>
          <details v-if="result.checks?.length">
            <summary>查看画面与语音时间对照</summary>
            <div v-for="check in result.checks" :key="check.node_id" class="timing">
              <strong>{{ check.label }}</strong
              ><small
                >画面 {{ seconds(check.planned_start) }}–{{
                  seconds(check.planned_end)
                }}
                秒</small
              ><small v-if="check.actual_start != null"
                >语音 {{ seconds(check.actual_start) }}–{{
                  seconds(check.actual_end)
                }}
                秒 ·
                {{ check.status === 'aligned' ? '在范围内' : '建议试听调整' }}</small
              ><small v-else>暂无可靠时间对照，请试听</small>
              <p>{{ check.text }}</p>
            </div>
          </details>
        </section>
        <details class="voice-list">
          <summary>已有旁白 · {{ voices.length }}</summary>
          <div class="voice-tools">
            <input
              v-model="voiceSearch"
              class="field"
              placeholder="搜索旁白名称"
              aria-label="搜索已有旁白"
            />
            <select v-model="voiceFilter" class="field" aria-label="筛选旁白">
              <option value="all">全部旁白</option>
              <option v-if="composition" value="current">当前组合</option>
            </select>
          </div>
          <div class="voice-scroll" tabindex="0" aria-label="已有旁白列表">
            <button
              v-for="voice in visibleVoices"
              :key="voice.id"
              class="voice-row"
              :aria-pressed="auditionId === voice.id"
              @click="auditionId = voice.id"
            >
              <span>{{ name(voice) }}</span
              ><small>{{
                voice.metadata?.bound_source_id
                  ? '已绑定组合'
                  : voice.metadata?.narration_status === 'pending_review'
                    ? '候选 / 历史版本'
                    : voice.metadata?.binding_id
                      ? '历史版本'
                      : '普通旁白'
              }}</small>
            </button>
            <p v-if="!visibleVoices.length">没有符合条件的旁白。</p>
          </div>
          <div v-if="audition" class="voice-audition">
            <small>{{ name(audition) }}</small
            ><audio
              :key="audition.id"
              :src="mediaUrl(audition.path)"
              controls
              preload="none"
            />
          </div>
        </details>
      </aside>
    </div>
  </section>
</template>
<style scoped>
input[role='switch'] {
  appearance: none;
  flex-shrink: 0;
  width: 34px;
  height: 20px;
  border: 1px solid var(--border-strong);
  border-radius: 12px;
  background: #d9d5e6;
  position: relative;
  cursor: pointer;
  transition: background 0.15s;
}
input[role='switch']::after {
  content: '';
  position: absolute;
  top: 2px;
  left: 2px;
  width: 14px;
  height: 14px;
  background: white;
  border-radius: 50%;
  transition: transform 0.15s;
}
input[role='switch']:checked {
  background: var(--purple);
}
input[role='switch']:checked::after {
  transform: translateX(14px);
}
input[role='switch']:focus-visible {
  outline: 2px solid var(--purple);
  outline-offset: 3px;
}
input[role='switch']:disabled {
  opacity: 0.5;
}

.check-row {
  color: var(--text);
  font-size: 13px;
}
.mode-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 0;
  margin-bottom: 14px;
  border-bottom: 1px solid var(--border);
}
.mode-row span {
  font-size: 12px;
  color: var(--text-muted);
}
.narration-layout {
  display: grid;
  grid-template-columns: 1.15fr 1fr;
  gap: 28px;
  align-items: start;
}
.script {
  display: block;
  margin: 16px 0;
  font-size: 13px;
}
.script span {
  display: block;
  margin-bottom: 8px;
}
textarea {
  min-height: 150px;
  resize: vertical;
}
.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
  margin: 14px 0;
}
.actions small,
p,
small {
  font-size: 12px;
  line-height: 1.65;
  color: var(--text-muted);
}
.draft,
.result {
  padding: 16px;
  border: 1px solid var(--border);
  border-radius: 10px;
  margin: 15px 0;
}
.draft textarea {
  margin-top: 12px;
}
video {
  width: 100%;
  max-height: 300px;
  background: #181621;
  border-radius: 10px;
}
audio,
progress {
  width: 100%;
  margin: 10px 0;
}
.danger {
  color: #b64453;
}
.warn {
  color: #987332;
}
.timing {
  border-top: 1px solid var(--border);
  padding: 12px 0;
}
.timing small,
.voice-list small {
  display: block;
}
.voice-list {
  margin-top: 16px;
}
.voice-tools {
  display: flex;
  gap: 8px;
  margin: 8px 0;
}
.voice-tools input {
  min-width: 0;
  flex: 1;
}
.voice-tools select {
  width: auto;
}
.voice-scroll {
  max-height: 320px;
  overflow-y: auto;
  overscroll-behavior: contain;
  border: 1px solid var(--border);
  border-radius: 8px;
}
.voice-row {
  display: block;
  width: 100%;
  border: 0;
  border-bottom: 1px solid var(--border);
  border-radius: 0;
  text-align: left;
  padding: 12px;
  overflow-wrap: anywhere;
}
.voice-row[aria-pressed='true'] {
  background: #785cff15;
  color: var(--purple-strong);
}
.voice-audition {
  margin-top: 10px;
}
.saved-script {
  white-space: pre-wrap;
}
.tempo-settings {
  display: grid;
  gap: 12px;
  margin: 16px 0;
  font-size: 13px;
}
.tempo-settings select {
  margin-top: 6px;
}
.accept {
  display: flex;
  margin: 16px 0;
}
summary {
  cursor: pointer;
  padding: 10px 0;
  font-size: 13px;
}
@media (max-width: 1050px) {
  .narration-layout {
    grid-template-columns: 1fr;
  }
}
</style>
