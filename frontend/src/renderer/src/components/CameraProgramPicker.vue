<script setup>
import { computed } from 'vue'
const props = defineProps({ mode: { type: Number, default: 4 }, modelValue: { type: Array, default: null }, disabled: Boolean })
const emit = defineEmits(['update:modelValue'])
const pieces = computed(() => [
  ['zoom-outbound','基础倍率 → 缩放倍率'],['zoom-return','缩放倍率 → 基础倍率'],
  ['origin-left','原点 → 左'],['left-right','左 → 右'],['right-origin','右 → 原点'],
  ['origin-up','原点 → 上'],['up-down','上 → 下'],['down-origin','下 → 原点'],
  ...(props.mode === 8 ? [['upper-left','原点 → 左上 → 原点'],['upper-right','原点 → 右上 → 原点'],['lower-right','原点 → 右下 → 原点'],['lower-left','原点 → 左下 → 原点']] : [])
])
function change(id, checked) {
  const chosen = new Set(props.modelValue ?? pieces.value.map(p => p[0]))
  checked ? chosen.add(id) : chosen.delete(id)
  emit('update:modelValue', pieces.value.map(p => p[0]).filter(key => chosen.has(key)))
}
</script>
<template>
  <div class="program">
    <label v-for="(piece, index) in pieces" :key="piece[0]"><input type="checkbox" :disabled="disabled || index < 2" :checked="index < 2 || modelValue === null || modelValue.includes(piece[0])" @change="change(piece[0], $event.target.checked)" /><small>{{ index+1 }}</small>{{ piece[1] }}</label>
  </div>
</template>
<style scoped>
.program { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:7px; margin:10px 0 }
label { display:flex; align-items:center; gap:8px; padding:8px; border:1px solid var(--border,#ddd); border-radius:6px; font-size:12px } small { opacity:.5 }
</style>
