<script setup>
import { computed } from 'vue'
import { narrationStyles, narrationStyle } from '../narration-styles.js'

const props = defineProps({
  modelValue: { type: String, default: 'natural' },
  applied: Boolean,
  automatic: Boolean,
  enabled: Boolean,
  busy: Boolean,
})
const emit = defineEmits(['update:modelValue', 'update:applied'])
const description = computed(() => props.modelValue === 'auto'
  ? '为各条组合自动分配风格，可在右侧查看。'
  : narrationStyle(props.modelValue).description)
</script>

<template>
  <div class="narration-style-picker">
    <label class="style-toggle">
      <input
        type="checkbox"
        role="switch"
        :checked="applied"
        :disabled="busy"
        @change="emit('update:applied', $event.target.checked)"
      />
      <span>应用旁白风格</span>
    </label>
    <template v-if="applied">
      <label class="style-choice">
        <span>旁白风格</span>
        <select
          class="field"
          :value="modelValue"
          :disabled="busy"
          @change="emit('update:modelValue', $event.target.value)"
        >
          <option v-for="style in narrationStyles" :key="style.id" :value="style.id">
            {{ style.name }}{{ style.id === 'natural' ? ' · 默认' : '' }}
          </option>
          <option v-if="automatic" value="auto">自动</option>
        </select>
      </label>
      <p>{{ description }}</p>
      <small v-if="!enabled">开启大模型润色后应用；关闭时按原文生成。</small>
    </template>
    <p v-else>沿用当前提示词，不附加风格要求。</p>
  </div>
</template>

<style scoped>
.narration-style-picker {
  padding: 14px 16px;
  background: var(--tree-leaf);
  border: 1px solid var(--border);
  border-radius: 10px;
  margin: 14px 0 18px;
}
label { display: flex; align-items: center; gap: 18px; font-size: 13px; }
label > span { white-space: nowrap; }
.style-toggle { gap: 8px; color: var(--text); }
.style-choice { margin-top: 12px; }
select { flex: 1; min-width: 0; }
p { font-size: 12px; line-height: 1.6; margin: 9px 0 0; color: var(--text-soft); }
small { display: block; font-size: 12px; color: var(--text-muted); margin-top: 5px; }
</style>
