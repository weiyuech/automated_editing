<script setup>
import { computed, ref } from 'vue'
import CaptureTreeNode from './CaptureTreeNode.vue'
import { captureSelectionState, changeCaptureChild, defaultCaptureSelection, formatCaptureTime, normalizeCaptureSelection } from '../capture-group-policy.js'

const props = defineProps({ group: { type: Object, required: true }, modelValue: { type: Object, default: null }, disabled: Boolean })
const emit = defineEmits(['update:modelValue', 'preview', 'retry'])
const expanded = ref(false)
const normalizedSelection = computed(() => normalizeCaptureSelection(props.group, props.modelValue))
const state = computed(() => captureSelectionState(props.group, normalizedSelection.value))
const summary = computed(() => {
  if (state.value.whole) return '完整录制 · 1 个剪辑输入'
  const unit = props.group.master_available ? '段' : '个可用片段'
  if (state.value.count) return `已选 ${state.value.count} / ${state.value.selectableCount} ${unit} · 合成 1 个剪辑输入`
  return `${state.value.selectableCount} ${unit} · 展开选择`
})
function child(id, checked) { emit('update:modelValue', changeCaptureChild(props.group, normalizedSelection.value, id, checked)) }
function preview(segment = null) {
  emit('preview', { path: segment?.available ? segment.path : props.group.master_path, kind: 'video',
    metadata: { role: 'raw_video' }, start_seconds: segment?.available ? 0 : (segment?.start || 0),
    end_seconds: segment ? (segment.available ? segment.end - segment.start : segment.end) : null })
}
</script>

<template>
  <div class="capture-picker" :class="{ 'capture-picker-selected': state.checked || state.partial }">
    <div class="capture-picker-root">
      <input type="checkbox" :aria-label="`选用 ${group.title} 的全部内容`" :checked="state.checked" :indeterminate="state.partial"
        :disabled="disabled || (!group.master_available && state.selectableCount === 0)"
        @change="emit('update:modelValue', $event.target.checked ? defaultCaptureSelection(group) : null)" />
      <button class="capture-picker-disclosure" :aria-expanded="expanded" @click="expanded = !expanded">
        <span class="capture-picker-caret">{{ expanded ? '▾' : '▸' }}</span>
        <span><strong>{{ group.title }}</strong><small>{{ summary }}</small></span>
      </button>
      <span class="capture-picker-duration">{{ formatCaptureTime(state.duration || group.duration) }}</span>
    </div>
    <div v-if="expanded" class="capture-picker-children">
      <div class="capture-picker-child">
        <label><input type="checkbox" :checked="Boolean(normalizedSelection?.include_full)" :disabled="disabled || !group.master_available" @change="child('full', $event.target.checked)" />
          <span><strong>完整录制</strong><small>{{ group.master_available ? '包含本次拍摄全部内容' : '原片已移走或删除' }}</small></span></label>
        <button :disabled="!group.master_available" @click="preview()">预览</button>
      </div>
      <CaptureTreeNode v-for="segment in group.segments" :key="segment.id" :node="segment" :group="group"
        :selection="normalizedSelection" :disabled="disabled" @change="child($event.id, $event.checked)" @preview="preview" />
      <p class="capture-picker-note">{{ group.error || group.timing_note }}<button v-if="group.status === 'failed'" @click="emit('retry', group)">重试生成</button></p>
    </div>
  </div>
</template>

<style scoped>
.capture-picker { min-width: 0; width: 100%; border: 1px solid var(--border, #ded9ec); border-radius: 10px; margin: 6px 0; overflow: hidden; background: var(--tree-recording); }
.capture-picker-selected { border-color: var(--purple); }
.capture-picker-root { display: flex; gap: 10px; align-items: center; padding: 10px 12px; background: var(--tree-root); }
.capture-picker input { flex: none; accent-color: #7961bf; }
.capture-picker-disclosure { display: flex; align-items: center; gap: 8px; flex: 1; min-width: 0; text-align: left; border: 0; background: transparent; padding: 0; color: inherit; }
.capture-picker-disclosure > span:last-child { min-width: 0; }
.capture-picker-disclosure strong, .capture-picker-child strong { display: block; font-size: 12px; font-weight: 500; overflow-wrap: break-word; }
.capture-picker-disclosure strong { font-size: 15px; }
.capture-picker small { display: block; font-size: 12px; font-weight: normal; opacity: .7; margin-top: 3px; }
.capture-picker-duration { flex: none; white-space: nowrap; font-size: 12px; font-variant-numeric: tabular-nums; opacity: .7; }
.capture-picker-children { margin-left: 25px; border-left: 1px solid var(--border, #ded9ec); background: var(--tree-recording); }
.capture-picker-child { display: flex; align-items: center; justify-content: space-between; gap: 8px; border-top: 1px solid var(--border, #ded9ec); padding: 9px 12px; background: var(--tree-recording); }
.capture-picker-child label { display: flex; align-items: center; gap: 10px; min-width: 0; cursor: pointer; }
.capture-picker-note { font-size: 11px; opacity: .75; margin: 10px 12px; }
.capture-picker-note button { margin-left: 8px; }
</style>
