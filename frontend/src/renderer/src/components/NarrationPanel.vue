<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { narrationMediaName, compositionDuration } from '../narration-context.js'
import { useAutomaticRewrite } from '../use-automatic-rewrite.js'
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
  useLlm = ref(false),
  items = ref([]),
  composition = ref(''),
  title = ref(''),
  source = ref('')
const draft = ref(''),
  draftSections = ref([]),
  noteAssignments = ref([]),
  generalNotes = ref([]),
  version = ref(''),
  result = ref(null),
  busy = ref(false),
  error = ref(''),
  info = ref('')
const sourceEditing = ref(false), instructionsEditing = ref(false), customEditing = ref(false)
const instructions = ref(''), contextData = ref(null), promptError = ref('')
const showPrompt = ref(false), customEnabled = ref(false), customPrompt = ref(''), promptPreview = ref(null), promptLoading = ref(false), measuredFeedback = ref(false)
function restorePrompt() { customEnabled.value = false; customPrompt.value = '' }
function enableCustom(checked) {
  if (checked && !customPrompt.value) customPrompt.value = promptPreview.value?.baseline_system || ''
  customEnabled.value = checked
}
let promptTimer, promptRevision = 0, narrationRevision = 0
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
const missingNotes = computed(() => aligned.value && contextData.value?.mode === 'single_recording'
  && !(contextData.value.capture_notes || []).some(note => note.trim()))
const needsInstructions = computed(() => useLlm.value && missingNotes.value)
const selectedDuration = computed(() => compositionDuration(selected.value))
const rewriteInput = computed(() => measuredFeedback.value
  ? draft.value || source.value || result.value?.text || ''
  : source.value)
const promptRequest = computed(() => ({text:rewriteInput.value, instructions:instructions.value,
  system_prompt:customEnabled.value ? customPrompt.value : null,
  ...(aligned.value ? {measured_feedback:measuredFeedback.value} : {}),
}))
const promptKey = computed(() => JSON.stringify([aligned.value,composition.value,promptRequest.value]))
watch([promptKey, showPrompt, () => props.active], (values, previous) => {
  if (values[0] === previous[0] && values[2] === previous[2] && promptPreview.value) return
  clearTimeout(promptTimer)
  const requested = ++promptRevision
  promptError.value = ''
  promptPreview.value = null
  if (!props.active || (aligned.value ? !composition.value : !showPrompt.value)) { promptLoading.value = false; return }
  const key = composition.value
  const endpoint = aligned.value ? `/compositions/${key}/narration/prompt` : '/tts/prompt'
  const body = JSON.stringify(promptRequest.value)
  promptLoading.value = true
  promptTimer = setTimeout(async () => {
    try {
      const data = await props.api(endpoint, {method:'POST',body})
      if (requested === promptRevision && !disposed) { contextData.value = data; promptPreview.value = data }
    } catch (e) {
      if (requested === promptRevision && !disposed) promptError.value = e.message
    } finally { if (requested === promptRevision && !disposed) promptLoading.value = false }
  }, 250)
})
watch([instructions, customEnabled, customPrompt, useLlm], () => { revision++ })
watch(useLlm, enabled => {
  if (!enabled) return
  measuredFeedback.value = false
  draft.value = ''
  draftSections.value = []
  noteAssignments.value = []
  generalNotes.value = []
  version.value = ''
})
const running = computed(() => result.value?.status === 'running')
const rewriteReady = computed(() => !sourceEditing.value && !instructionsEditing.value && !customEditing.value
  && !busy.value && !running.value && props.active
  && Boolean(rewriteInput.value.trim())
  && (!aligned.value || Boolean(selected.value && contextData.value))
  && (!needsInstructions.value || Boolean(instructions.value.trim()))
  && (!customEnabled.value || Boolean(customPrompt.value.trim())))
const { pending: autoRewritePending, rewriting } = useAutomaticRewrite({
  enabled: useLlm,
  ready: rewriteReady,
  run: polish,
})
const canGenerate = computed(
  () =>
    !busy.value &&
    !running.value &&
    text.value.trim() &&
    text.value.length <= 4000 &&
    (!aligned.value || selected.value),
)
const playbackRate = ref(1),
  autoTempo = ref(true)
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
    aligned.value && result.value &&
    (playbackRate.value !== result.value.playback_rate ||
      autoTempo.value !== result.value.auto_tempo),
)
watch([aligned, composition], () => { auditionId.value = '' })
function restoreSettings() {
  playbackRate.value = result.value?.playback_rate ?? 1
  autoTempo.value = result.value?.auto_tempo ?? true
}
function narrationSnapshot() {
  return { revision: narrationRevision, key: composition.value, aligned: aligned.value }
}
function isCurrentNarration(snapshot) {
  return !disposed && snapshot.revision === narrationRevision
    && snapshot.key === composition.value && snapshot.aligned === aligned.value
}
async function publishNarration(snapshot, data) {
  if (!isCurrentNarration(snapshot)) return
  result.value = data
  if (data.status === 'ready') {
    await refresh()
    if (isCurrentNarration(snapshot)) emit('changed')
  }
}
async function adjust() {
  narrationRevision++
  const snapshot = narrationSnapshot()
  const attempt = result.value.attempt_id
  await safe(async () => {
    const data = await props.api(
      `/compositions/${snapshot.key}/narration/adjust`,
      {
        method: 'POST',
        body: JSON.stringify({
          attempt_id: attempt,
          playback_rate: playbackRate.value,
          auto_tempo: autoTempo.value,
        }),
      },
    )
    await publishNarration(snapshot, data)
  }, () => isCurrentNarration(snapshot))
}
function exclusivePlayback(event) {
  panel.value?.querySelectorAll('audio, video').forEach((media) => {
    if (media !== event.target) media.pause()
  })
}

function name(i) {
  return narrationMediaName(i)
}
function durationLabel(item) {
  const value = compositionDuration(item)
  return value === null ? '时长待读取' : `${seconds(value)} 秒`
}
function seconds(n) {
  return Number(n || 0).toFixed(2)
}
async function refresh() {
  items.value = await props.api('/media')
}
async function safe(fn, isCurrent = () => !disposed) {
  busy.value = true
  error.value = ''
  try {
    await fn()
  } catch (e) {
    if (isCurrent()) error.value = e.message
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
watch([source, aligned, composition], () => {
  revision++
  draft.value = ''
  draftSections.value = []
  noteAssignments.value = []
  generalNotes.value = []
  version.value = ''
  info.value = ''
})
watch([aligned, composition], async () => {
  useLlm.value = false
  sourceEditing.value = instructionsEditing.value = customEditing.value = false
  const loadRevision = ++narrationRevision
  const key = composition.value
  result.value = null
  contextData.value = null
  restorePrompt()
  measuredFeedback.value = false
  showPrompt.value = false
  if (aligned.value && key)
    try {
      const record = await props.api(`/compositions/${key}`)
      if (!disposed && narrationRevision === loadRevision && composition.value === key && aligned.value) {
        result.value = record.narration || null
        if (!source.value.trim() && result.value?.text) source.value = result.value.text
        restoreSettings()
      }
    } catch (e) {
      if (!disposed && narrationRevision === loadRevision) error.value = e.message
    }
})
async function polish(measured = false) {
  if (needsInstructions.value && !instructions.value.trim()) {
    error.value = '请填写本次补充要求，说明画面重点或旁白安排。'
    return
  }
  if (customEnabled.value && !customPrompt.value.trim()) { error.value = '请填写自定义提示词，或恢复默认。'; return }
  measuredFeedback.value = measured
  // Finish reactive invalidation before capturing this request's revision.
  await nextTick()
  const requested = ++revision
  await safe(async () => {
    const endpoint = aligned.value ? `/compositions/${composition.value}/narration/draft` : '/tts/draft'
    const data = await props.api(endpoint, {method:'POST',body:JSON.stringify(promptRequest.value)})
    if (disposed || requested !== revision) return
    draft.value = data.text || data.draft_text
    draftSections.value = data.sections || []
    noteAssignments.value = data.note_assignments || []
    generalNotes.value = data.general_notes || []
    version.value = ''
    info.value = ''
  })
}
async function generate() {
  narrationRevision++
  const snapshot = narrationSnapshot()
  await safe(async () => {
    if (snapshot.aligned) {
      const data = await props.api(
        `/compositions/${snapshot.key}/narration`,
        {
          method: 'POST',
          body: JSON.stringify({
            text: text.value,
            sections: useLlm.value && version.value === 'draft' ? draftSections.value : [],
            note_assignments: useLlm.value ? noteAssignments.value : [],
            general_notes: useLlm.value ? generalNotes.value : [],
            playback_rate: playbackRate.value,
            auto_tempo: autoTempo.value,
            direct_narration: !useLlm.value,
          }),
        },
      )
      await publishNarration(snapshot, data)
    } else {
      const data = await props.api('/tts/generate', {
        method: 'POST',
        body: JSON.stringify({
          title: title.value || '旁白',
          text: text.value,
          use_llm: false,
        }),
      })
      await publishNarration(snapshot, {
        ...data.asset,
        status: 'ready',
        actual_seconds: data.asset.duration_ms / 1000,
        source_seconds: data.asset.duration_ms / 1000,
        audio_path: data.asset.audio_path || data.asset.path,
        message: '完整旁白已生成，可加入普通视频的旁白池。',
      })
    }
  }, () => isCurrentNarration(snapshot))
}
async function poll() {
  if (disposed) return
  const snapshot = narrationSnapshot()
  const attempt = result.value?.attempt_id
  const currentAttempt = () => isCurrentNarration(snapshot) && result.value?.attempt_id === attempt
  try {
    if (snapshot.aligned && snapshot.key && running.value) {
      const data = await props.api(`/compositions/${snapshot.key}`)
      // A slow response for an older synthesis must never replace a new attempt.
      if (currentAttempt() && data.narration?.attempt_id === attempt) {
        result.value = data.narration
        if (!running.value) {
          await refresh()
          if (currentAttempt()) emit('changed')
        }
      }
    }
  } catch (e) {
    if (currentAttempt()) error.value = e.message
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
  clearTimeout(promptTimer)
  promptRevision++
  revision++
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
              {{ name(item) }} · {{ durationLabel(item) }}
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
        <h3 class="work-heading">文案</h3>
        <label class="script"
          ><span>{{ useLlm ? '原始文案' : '完整旁白文案' }}</span
          ><textarea
            v-model="source" @input="sourceEditing = true" @change="sourceEditing = false" @blur="sourceEditing = false"
            class="field text"
            maxlength="8000"
            :disabled="busy || running"
            :placeholder="useLlm ? '填写希望介绍的内容。' : '填写要直接朗读的完整正文。'"
          />
        </label>
        <div class="rewrite-switch-row">
          <label class="check-row polish-toggle"><input v-model="useLlm" type="checkbox" role="switch" :disabled="busy || running" />大模型润色</label>
          <button class="prompt-reveal" :aria-expanded="showPrompt" @click="showPrompt = !showPrompt">{{ showPrompt ? '收起提示词' : '展开提示词' }}</button>
        </div>
        <label v-if="useLlm" class="script extra-instructions">
          <span>本次补充要求 <small :class="{required: needsInstructions}">{{ needsInstructions ? '必填' : '可选' }}</small></span>
          <textarea v-model="instructions" @input="instructionsEditing = true" @change="instructionsEditing = false" @blur="instructionsEditing = false" :required="needsInstructions" :aria-required="needsInstructions" class="field" maxlength="2000" :disabled="busy || running"
            placeholder="例如：A 点重点讲产品用途，移动时留白，B 点用一句话介绍成品。" />
          <small v-if="needsInstructions" class="requirement-hint">此组合没有拍摄备注，请说明画面重点或旁白安排。</small>
        </label>
        <section v-if="useLlm" class="draft">
          <p v-if="rewriting" class="form-hint" role="status">正在润色…</p>
          <p v-else-if="autoRewritePending && needsInstructions && !instructions.trim()" class="form-hint">填写补充要求后自动润色。</p>
          <strong>审阅改写稿（可直接修改）</strong
          ><textarea
            v-model="draft"
            aria-label="审阅改写稿"
            placeholder="改写稿将自动显示在这里，可直接修改。"
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
              :disabled="busy || running || !draft.trim()"
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
        <details v-if="aligned" class="playback-options"><summary>播放设置 · {{ playbackRate.toFixed(1) }} 倍{{ autoTempo ? ' · 自动微调' : '' }}</summary><div class="tempo-settings">
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
        </details>
        <button class="primary" :disabled="!canGenerate" @click="generate">
          {{ running ? '正在合成并测量…' : '生成旁白' }}
        </button>
        <p v-if="aligned && useLlm && !contextData && promptError" class="inline-status danger" role="alert">{{ promptError }}</p>
        <p v-if="info" class="inline-status">{{ info }}</p>
        <p v-if="error" class="inline-status danger" role="alert">
          {{ error }}
        </p>
      </div>
      <aside>
        <h3 class="work-heading">时长</h3>
        <dl class="duration-summary">
          <div v-if="aligned"><dt>组合画面</dt><dd>{{ selectedDuration ? seconds(selectedDuration) + ' 秒' : '—' }}</dd></div>
          <div><dt>原始语音</dt><dd>{{ result?.source_seconds != null ? seconds(result.source_seconds) + ' 秒' : '—' }}</dd></div>
          <div class="adjusted-duration"><dt>调整后语音</dt><dd>{{ result?.status === 'ready' ? seconds(result.actual_seconds) + ' 秒' : '—' }}</dd></div>
        </dl>
        <p v-if="showPrompt && promptError" class="danger" role="alert">{{ promptError }}</p>
        <section v-if="result" class="generation-result">
          <p class="generation-state" role="status">{{ running ? '正在生成并调整…' : result.status === 'ready' ? aligned ? '已生成 · 已绑定此组合' : '已生成' : result.status === 'needs_revision' ? '语音过长，请精简文案' : result.status === 'pending_review' ? '已有旁白尚未绑定，可直接应用' : '生成失败' }}</p>
          <progress v-if="running" max="1" :value="result.progress" />
          <audio v-if="result.audio_path" :key="result.audio_path" :src="mediaUrl(result.audio_path)" controls preload="metadata" aria-label="旁白试听" />
          <details v-if="result.warnings?.length" class="alignment-notes">
            <summary>时长与对齐说明</summary>
            <p v-for="(warning, index) in result.warnings" :key="index" class="form-hint">{{ warning }}</p>
          </details>
          <p v-if="result.error" class="danger">{{ result.error }}</p>
          <p v-if="aligned && scriptChanged" class="form-hint">文案已修改，重新生成后更新绑定。</p>
          <p v-if="settingsChanged && result.status === 'ready'" class="form-hint">速度设置已修改，应用后更新音频与绑定。</p>
          <button v-if="aligned && result.source_seconds && (settingsChanged || ['needs_revision', 'pending_review'].includes(result.status))" :disabled="busy || running || scriptChanged" @click="adjust">{{ result.status === 'pending_review' ? '应用已有旁白' : '应用速度调整' }}</button>
          <button v-if="aligned && useLlm && result.status === 'needs_revision'" :disabled="busy || running || (needsInstructions && !instructions.trim())" @click="polish(true)">按实测时长精简文案</button>
        </section>
        <section v-if="showPrompt" class="prompt-inspector">
          <div class="inspector-heading"><strong>完整提示词</strong><button @click="showPrompt = false" aria-label="收起提示词面板">收起</button></div>
          <p v-if="aligned && !selected" class="form-hint">先选择一个组合。</p>
          <template v-else>
            <div class="prompt-controls"><label class="check-row"><input type="checkbox" :checked="customEnabled" :disabled="busy || running || (!customEnabled && !promptPreview)" @change="enableCustom($event.target.checked)" />自定义系统提示词</label><button :disabled="busy || running || !customEnabled" @click="restorePrompt">恢复默认</button></div>
            <label class="prompt-label">系统提示词</label>
            <textarea v-if="customEnabled" v-model="customPrompt" @input="customEditing = true" @change="customEditing = false" @blur="customEditing = false" aria-label="自定义系统提示词" class="field prompt-editor" maxlength="12000" :disabled="busy || running" />
            <pre v-else-if="promptPreview" class="prompt-content">{{ promptPreview.system }}</pre>
            <label v-if="promptPreview" class="prompt-label">本次输入</label>
            <pre v-if="promptPreview" class="prompt-content">{{ promptPreview.user }}</pre>
            <p v-if="promptLoading" class="form-hint" role="status">正在更新提示词…</p>
          </template>
        </section>
        <details v-if="useLlm && (noteAssignments.length || generalNotes.length)" class="voice-list">
          <summary>查看备注对应</summary>
          <p v-for="(note, index) in noteAssignments" :key="index" class="form-hint">{{ contextData?.windows?.find(window => window.id === note.node_id)?.label || note.node_id }}：{{ note.text }}</p>
          <p v-for="(note, index) in generalNotes" :key="'general-' + index" class="form-hint">整体参考：{{ note }}</p>
        </details>
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
.rewrite-switch-row { display:flex; justify-content:space-between; align-items:center; margin:16px 0; gap:12px; }
.rewrite-switch-row .polish-toggle { margin:0; }
.prompt-reveal { font-size:12px; padding:5px 9px; color:#7553a4; }
.prompt-inspector { margin:20px 0; padding:15px; border:1px solid #ded1ef; background:#f4eefb; border-radius:10px; }
.inspector-heading { display:flex; align-items:center; justify-content:space-between; font-size:13px; }
.inspector-heading button { font-size:12px; padding:4px 8px; }
.prompt-controls { margin:12px 0; display:flex; align-items:center; justify-content:space-between; gap:8px; flex-wrap:wrap; font-size:12px; }
.prompt-controls button { font-size:12px; padding:5px 8px; }
.prompt-inspector .prompt-content { font-size:12px; }
.draft > .actions:first-child { margin:12px 0; }
.duration-summary { margin:0; padding:8px 18px; background:#f0e8fa; border:1px solid #e0d3ef; border-radius:10px; }
.duration-summary div { display:flex; justify-content:space-between; gap:14px; padding:15px 0; font-size:13px; }
.duration-summary div + div { border-top:1px solid #e0d3ef; }
.duration-summary dt { color:#79668d; }
.duration-summary dd { margin:0; font-variant-numeric:tabular-nums; font-weight:500; color:#463058; }
.duration-summary .adjusted-duration dd { color:#7650a8; }
.generation-result { margin:18px 0; }
.generation-state { color:#7650a8; font-size:13px; }
.generation-result button { margin:6px 8px 0 0; }
.work-heading { font-size:15px; font-weight:600; margin:20px 0 12px; color:#45345d; }
aside .work-heading { margin-top:0; }
.polish-toggle { justify-content:flex-start; margin:0 0 16px; font-size:13px; }
.polish-toggle small { color:#88779c; font-size:12px; margin-left:8px; }
.extra-instructions small { color:#8a7a9c; font-size:12px; font-weight:400; }
.extra-instructions .required { color:#805cb0; }
.requirement-hint { display:block; margin-top:6px; line-height:1.6; }
.playback-options { margin:18px 0; border-top:1px solid #e4d9f0; border-bottom:1px solid #e4d9f0; }
.playback-options summary { color:#77618e; }
.empty-preview { color:#8a7a9c; padding:70px 20px; text-align:center; background:#f1eafa; border-radius:10px; font-size:13px; }
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
.extra-instructions textarea { min-height: 80px; }
.context-toggles, .prompt-controls { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin: 12px 0; }
.context-toggles button, .prompt-controls button { font-size: 12px; padding: 6px 9px; }
.context-card { background: var(--tree-leaf); border: 1px solid var(--border); border-radius: 10px; padding: 12px; margin-bottom: 14px; }
.context-card > strong { font-size: 14px; font-weight: 500; }
.narration-mode { display: inline-block; background: var(--tree-branch); color: var(--text-soft); border-radius: 6px; padding: 5px 8px; margin: 4px 0; font-size: 12px; }
.prompt-label { display: block; font-size: 12px; color: var(--text-soft); margin: 10px 0 6px; }
.prompt-content { margin: 0; max-height: 260px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; font-size: 12px; line-height: 1.65; color: var(--text-soft); background: var(--tree-branch); border-radius: 6px; padding: 10px; }
.prompt-editor { min-height: 260px; font-size: 12px; line-height: 1.65; }
.context-scroll { max-height: 320px; overflow: auto; }
.context-window { border-top: 1px solid var(--border); padding: 8px 0; }
.context-window strong, .context-window small { display: block; font-size: 12px; }
.context-window p { margin: 4px 0; }
.narration-layout > * { min-width: 0; }
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
