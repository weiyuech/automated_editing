<script setup>
import { computed, ref, watch } from 'vue'
import CompositionGroups from './CompositionGroups.vue'
import NarrationStylePicker from './NarrationStylePicker.vue'
import { narrationStyle } from '../narration-styles.js'
import { compositionLabel, durationLabel } from '../composition-groups.js'
import { narrationMediaName } from '../narration-context.js'
import { useGroupedNarration } from '../use-grouped-narration.js'

const props = defineProps({
  api: Function,
  mediaUrl: Function,
  active: { type: Boolean, default: true },
  initialCompositionIds: { type: Array, default: () => [] },
})
const emit = defineEmits(['changed', 'preview-source', 'busy'])
const {
  items, chosen, inspect, compositions, selected, selectedComposition, pending, source, instructions,
  llm, style, applyStyle, custom, system, showPrompt, prompt, promptError, promptLoading, rate, autoTempo,
  skip, editing, rewriting, loading, locked, running, error, batch, voices, missingNotes, inspectedDraft,
  canGenerate, requestFor, currentDraft, narration, batchState, enableCustom, restorePrompt, chooseVersion,
  editDraft, generate, boundVoice, inspectedNarration, scriptChanged, canAdjust, adjust,
} = useGroupedNarration(props, emit)
const audition = ref(null), panel = ref(null)
watch(() => props.active, active => {
  if (!active) panel.value?.querySelectorAll('audio').forEach(audio => audio.pause())
})
function exclusivePlayback(event) {
  panel.value?.querySelectorAll('audio').forEach(audio => { if (audio !== event.target) audio.pause() })
}
const completeCount = computed(() => (batch.value?.items || []).filter(item => ['ready', 'skipped'].includes(item.status)).length)
function seconds(value) { return Number.isFinite(Number(value)) ? `${Number(value).toFixed(1)} 秒` : '—' }
function styleLabel(item) {
  if (skip.value && item.metadata.bound_voice_id) {
    const existing = boundVoice(item)?.metadata
    return existing?.narration_style ? `${narrationStyle(existing.narration_style).name} · 已有` : '已有旁白'
  }
  if (!llm.value || currentDraft(item)?.version === 'source') return '原文直读'
  const assigned = requestFor(item).narration_style
  return assigned ? narrationStyle(assigned).name : '沿用提示词'
}
function stateLabel(item) {
  const state = batchState(item)
  if (narration(item)?.status === 'running') return '正在生成或调整'
  if (narration(item)?.status === 'ready' && state?.status !== 'queued') return ''
  if (state && ['queued', 'running'].includes(state.status)) return state.status === 'queued' ? '等待生成' : '正在生成'
  if (state?.status === 'needs_revision') return '语音过长，请精简文案'
  if (state?.error) return state.error
  const draft = currentDraft(item)
  return draft?.error || (draft?.loading ? '正在润色' : llm.value && draft && !draft.version ? '待审阅改写稿' : '')
}
</script>

<template>
  <section ref="panel" class="panel wide grouped-narration" @play.capture="exclusivePlayback">
    <div class="panel-title">旁白制作</div>
    <div class="narration-layout">
      <div class="narration-main">
        <div class="selection-heading"><h3>已保存的组合</h3><small>已选 {{ selected.length }} 条</small></div>
        <div class="group-selection">
          <CompositionGroups v-model="chosen" :items="compositions" :originals="items" selectable :disabled="locked"
            @preview="emit('preview-source', $event)" />
        </div>
        <p class="form-hint">每次只为同一次录制下的组合生成旁白。</p>
        <p v-if="loading" class="form-hint" role="status">正在读取组合与旁白…</p>
        <p v-if="selected.length > 200" class="danger">一次最多生成 200 条，请减少选择。</p>
        <h3>文案</h3>
        <label class="script">
          <span>{{ llm ? '原始文案' : '完整旁白文案' }}</span>
          <textarea v-model="source" class="field text" maxlength="8000" :disabled="locked"
            @input="editing = true" @blur="editing = false" @change="editing = false" />
        </label>
        <div class="rewrite-row">
          <label class="check-row"><input v-model="llm" type="checkbox" role="switch" :disabled="locked" />大模型润色</label>
          <button :aria-expanded="showPrompt" @click="showPrompt = !showPrompt">{{ showPrompt ? '收起提示词' : '展开提示词' }}</button>
        </div>
        <NarrationStylePicker v-model="style" v-model:applied="applyStyle" automatic :enabled="llm" :busy="locked" />
        <template v-if="llm">
          <label class="script">
            <span>本次补充要求 <small :class="{ required: missingNotes }">{{ missingNotes ? '必填' : '可选' }}</small></span>
            <textarea v-model="instructions" class="field supplement" maxlength="2000" :required="missingNotes"
              :disabled="locked" placeholder="例如：产品用途为重点，点位间移动时少讲。"
              @input="editing = true" @blur="editing = false" @change="editing = false" />
          </label>
          <p v-if="missingNotes && !instructions.trim()" class="form-hint">所选组合没有拍摄备注，请说明画面重点或旁白安排。</p>
          <div class="draft-review">
            <label class="script"><span>审阅改写稿（可直接修改）</span>
              <select v-model="inspect" class="field" aria-label="审阅组合">
                <option value="">第一个选中组合</option>
                <option v-for="item in selected" :key="item.id" :value="item.id">{{ compositionLabel(item) }}</option>
              </select>
            </label>
            <p v-if="rewriting" role="status" class="form-hint">正在逐条润色，每条组合使用自己的时长与画面安排。</p>
            <p v-if="inspectedDraft?.error" class="danger" role="alert">{{ inspectedDraft.error }} 可修改输入后重新润色，或关闭润色使用原文。</p>
            <textarea :value="inspectedDraft?.text || ''" class="field text" aria-label="审阅改写稿" maxlength="4000"
              :disabled="locked || !inspectedDraft || Boolean(inspectedDraft.error)" placeholder="改写稿会自动显示在这里。"
              @input="editDraft($event.target.value)" />
            <details v-if="inspectedDraft?.general_notes?.length"><summary>文案取舍说明</summary><p v-for="note in inspectedDraft.general_notes" :key="note" class="form-hint">{{ note }}</p></details>
            <div class="version-actions">
              <button :disabled="locked || !inspectedDraft || Boolean(inspectedDraft.error)" :class="{ active: inspectedDraft?.version === 'source' }" @click="chooseVersion('source')">保留原文</button>
              <button :disabled="locked || !inspectedDraft?.text?.trim()" :class="{ active: inspectedDraft?.version === 'draft' }" @click="chooseVersion('draft')">采用改写稿</button>
              <small>{{ inspectedDraft?.version === 'draft' ? '将使用当前改写稿' : inspectedDraft?.version === 'source' ? '将使用原文' : '请选择要合成的版本' }}</small>
            </div>
          </div>
        </template>
        <details class="playback-options">
          <summary>播放设置 · {{ rate.toFixed(1) }} 倍{{ autoTempo ? ' · 自动微调' : '' }}</summary>
          <label class="script"><span>基础播放速度</span><select v-model.number="rate" class="field" :disabled="locked">
            <option :value="0.9">0.9 倍 · 稍慢</option><option :value="1">1.0 倍 · 原速</option><option :value="1.1">1.1 倍 · 稍快</option>
          </select></label>
          <label class="check-row"><input v-model="autoTempo" type="checkbox" :disabled="locked" />允许在 0.9～1.1 倍内微调，以适应画面</label>
        </details>
        <div class="generation-footer">
          <label class="check-row"><input v-model="skip" type="checkbox" :disabled="locked" />跳过已有旁白</label>
          <button class="primary" :disabled="!canGenerate" @click="generate">{{ running ? '正在生成…' : `批量生成旁白${pending.length ? '（' + pending.length + ' 条）' : ''}` }}</button>
        </div>
        <p v-if="batch" class="inline-status" role="status">{{ running ? '生成中' : batch.status === 'complete' ? '生成完成' : '部分组合需要处理' }} · 已完成 {{ completeCount }} / {{ batch.items.length }} 条</p>
        <p v-if="batch && ['needs_attention', 'interrupted'].includes(batch.status)" class="form-hint">处理失败项后可再次生成，已绑定旁白的组合会按上方设置跳过。</p>
        <p v-if="error" class="danger" role="alert">{{ error }}</p>
      </div>
      <aside>
        <h3>本次生成</h3>
        <div class="batch-summary">
          <span>选中组合 <strong>{{ selected.length }} 条</strong></span>
          <span>待生成旁白 <strong>{{ pending.length }} 条</strong></span>
          <small>生成后，一条组合对应一条旁白。</small>
        </div>
        <div v-if="selected.length" class="time-table">
          <div class="time-row time-head"><span>组合 / 风格</span><span>画面</span><span>调整后语音</span></div>
          <div v-for="item in selected" :key="item.id" class="time-row">
            <span><button class="inspect-row" @click="inspect = item.id">{{ compositionLabel(item) }}</button>
              <small class="assigned-style">{{ styleLabel(item) }}</small>
              <small v-if="stateLabel(item)" :class="batchState(item)?.error || currentDraft(item)?.error ? 'danger' : 'assigned-style'">{{ stateLabel(item) }}</small>
            </span>
            <span>{{ durationLabel(item) }}</span>
            <span>{{ boundVoice(item)?.metadata?.duration_ms != null ? seconds(boundVoice(item).metadata.duration_ms / 1000) : narration(item)?.status === 'ready' ? seconds(narration(item).actual_seconds) : '—' }}
              <small v-if="narration(item)?.source_seconds" class="assigned-style">原始 {{ seconds(narration(item).source_seconds) }}</small>
            </span>
          </div>
        </div>
        <p v-else class="form-hint">从左侧选择组合。</p>
        <section v-if="inspectedNarration" class="audio-result">
          <strong>{{ compositionLabel(selectedComposition) }}</strong>
          <p v-if="inspectedNarration.status === 'running'" role="status">正在生成或调整…</p>
          <p v-else-if="inspectedNarration.status === 'needs_revision'" class="danger">语音过长，请精简文案或调整播放速度。</p>
          <p v-if="inspectedNarration.error" class="danger">{{ inspectedNarration.error }}</p>
          <audio v-if="inspectedNarration.audio_path" :key="inspectedNarration.audio_path" :src="mediaUrl(inspectedNarration.audio_path)" controls preload="none" />
          <p v-if="scriptChanged" class="form-hint">文案已修改，重新生成后更新绑定。</p>
          <button v-if="canAdjust" @click="adjust">{{ inspectedNarration.status === 'pending_review' ? '应用已有旁白' : '应用速度调整' }}</button>
          <small v-if="canAdjust" class="assigned-style">复用已有语音，不重新合成。</small>
          <details v-if="inspectedNarration.warnings?.length"><summary>时长与对齐说明</summary><p v-for="warning in inspectedNarration.warnings" :key="warning" class="form-hint">{{ warning }}</p></details>
        </section>
        <section v-if="showPrompt" class="prompt-inspector">
          <div class="selection-heading"><h3>完整提示词</h3><button @click="showPrompt = false">收起</button></div>
          <label class="script"><span>查看组合对应的输入</span><select v-model="inspect" class="field">
            <option value="">第一个选中组合</option>
            <option v-for="item in selected" :key="item.id" :value="item.id">{{ compositionLabel(item) }}</option>
          </select></label>
          <div class="rewrite-row">
            <label class="check-row"><input type="checkbox" :checked="custom" :disabled="locked || (!custom && !prompt)" @change="enableCustom($event.target.checked)" />自定义系统提示词</label>
            <button :disabled="locked || !custom" @click="restorePrompt">恢复默认</button>
          </div>
          <label class="script"><span>{{ prompt?.writing_system ? '点位事实对应 · 系统提示词' : '系统提示词' }}</span>
            <textarea v-if="custom" v-model="system" class="field text" maxlength="12000" :disabled="locked"
              @input="editing = true" @blur="editing = false" @change="editing = false" />
            <pre v-else-if="prompt">{{ prompt.system }}</pre>
          </label>
          <details v-if="prompt"><summary>本次输入 · 文案、时间与拍摄备注</summary><pre>{{ prompt.user }}</pre></details>
          <details v-if="prompt?.writing_system"><summary>整篇润色 · 系统提示词</summary><pre>{{ prompt.writing_system }}</pre><pre>{{ JSON.stringify(prompt.writing_options, null, 2) }}</pre><p class="form-hint">{{ prompt.writing_note }}</p></details>
          <details v-if="inspectedDraft?.prompt_trace?.length"><summary>本稿实际请求</summary><div v-for="(entry, index) in inspectedDraft.prompt_trace" :key="index"><small>{{ index + 1 }}{{ entry.cached ? ' · 复用相同输入' : '' }}</small><pre>{{ entry.system }}</pre><pre>{{ entry.user }}</pre></div></details>
          <p v-if="promptLoading" class="form-hint" role="status">正在更新提示词…</p>
          <p v-if="promptError" class="danger" role="alert">{{ promptError }}</p>
          <p v-if="!selectedComposition" class="form-hint">先选择一个组合。</p>
        </section>
        <details class="existing-voices">
          <summary>已有旁白 · {{ voices.length }}</summary>
          <div class="voice-scroll">
            <button v-for="voice in voices" :key="voice.id" class="voice-row" @click="audition = voice">
              {{ narrationMediaName(voice) }}<small>{{ voice.metadata.bound_source_id ? '已绑定组合' : '普通旁白 / 历史版本' }}</small>
            </button>
          </div>
          <audio v-if="audition" :key="audition.id" :src="mediaUrl(audition.path)" controls preload="none" />
        </details>
      </aside>
    </div>
  </section>
</template>

<style scoped>
.check-row { color: var(--text); font-size: 13px; }
.assigned-style { display: block; margin-top: 5px; font-size: 12px; color: var(--text-muted); }
.narration-layout { display: grid; grid-template-columns: 1.15fr 1fr; gap: 28px; margin-top: 18px; align-items: start; }
.narration-layout > * { min-width: 0; }
h3 { font-size: 15px; font-weight: 600; margin: 18px 0 12px; }
.selection-heading { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.selection-heading h3, aside > h3 { margin-top: 0; }
.selection-heading small, .script small { font-size: 12px; color: var(--text-muted); }
.group-selection { max-height: 330px; overflow: auto; padding-right: 4px; }
.script { display: block; margin: 16px 0; font-size: 13px; }
.script > span { display: block; margin-bottom: 8px; }
.script .required { color: #805cb0; }
textarea.text { min-height: 150px; }
.supplement { min-height: 80px; }
.rewrite-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin: 16px 0; }
.rewrite-row button { font-size: 12px; padding: 5px 9px; }
.batch-summary { display: grid; gap: 16px; padding: 18px; background: var(--tree-branch, #eee4f8); border: 1px solid var(--border); border-radius: 10px; font-size: 13px; }
.batch-summary span { display: flex; justify-content: space-between; }
.batch-summary small { font-size: 12px; color: var(--text-muted); }
.time-table { margin-top: 18px; max-height: 320px; overflow: auto; background: var(--tree-leaf); border: 1px solid var(--border); border-radius: 10px; }
.time-row { display: grid; grid-template-columns: 1.4fr .7fr 1fr; gap: 8px; padding: 10px 12px; border-bottom: 1px solid var(--border); font-size: 12px; }
.time-row > * { min-width: 0; overflow-wrap: anywhere; }
.time-head { background: var(--tree-branch); position: sticky; top: 0; }
.inspect-row { border: 0; background: none; padding: 0; text-align: left; font-size: 12px; }
.playback-options { margin: 18px 0; padding: 12px 0; border-top: 1px solid var(--border); border-bottom: 1px solid var(--border); font-size: 12px; }
.generation-footer { display: flex; justify-content: space-between; gap: 12px; align-items: center; }
.prompt-inspector { margin: 20px 0; padding: 15px; background: var(--tree-leaf); border: 1px solid var(--border); border-radius: 10px; }
.prompt-inspector pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 260px; overflow: auto; font-size: 12px; background: var(--tree-branch); padding: 12px; border-radius: 8px; }
.prompt-inspector details { font-size: 12px; }
.existing-voices { margin-top: 20px; font-size: 12px; }
.existing-voices summary { padding: 12px 0; }
.voice-scroll { max-height: 240px; overflow: auto; }
.voice-row { display: block; width: 100%; text-align: left; padding: 10px; border: 0; border-top: 1px solid var(--border); border-radius: 0; font-size: 12px; }
.voice-row small { display: block; color: var(--text-muted); margin-top: 4px; }
audio { width: 100%; margin-top: 10px; }
.audio-result { margin-top: 18px; padding: 14px; border: 1px solid var(--border); background: var(--tree-leaf); border-radius: 10px; font-size: 12px; }
.version-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 12px; font-size: 12px; }
.version-actions button { font-size: 12px; }
.danger { color: #b64453; font-size: 12px; }
@media (max-width: 900px) { .narration-layout { grid-template-columns: 1fr; } }
</style>
