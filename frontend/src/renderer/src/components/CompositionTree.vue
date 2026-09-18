<script setup>
import { ref } from 'vue'
import { formatCaptureTime } from '../capture-group-policy.js'
defineProps({ nodes: Array, selected: Array, texts: Object, editable: Boolean, disabled: Boolean })
const emit = defineEmits(['pick', 'text', 'preview'])
const collapsed = ref({})
</script>
<template>
  <div class="composition-tree">
    <div v-for="node in nodes" :key="node.id" class="composition-node">
      <div class="row">
        <button v-if="node.children?.length" class="caret" @click="collapsed[node.id] = !collapsed[node.id]">{{ collapsed[node.id] ? '▸' : '▾' }}</button>
        <input v-if="editable && node.kind !== 'preparation'" type="checkbox" :aria-label="`为 ${node.label} 配旁白`" :checked="selected.includes(node.id)" :disabled="disabled" @change="emit('pick', { node, checked: $event.target.checked })" />
        <strong>{{ node.label }}</strong><span>{{ formatCaptureTime(node.duration) }}</span>
        <button @click="emit('preview', node)">预览此层</button>
      </div>
      <small>成片 {{ formatCaptureTime(node.start) }}–{{ formatCaptureTime(node.end) }}</small>
      <p v-if="node.notes?.length" class="notes">拍摄备注：{{ node.notes.join('；') }}</p>
      <textarea v-if="editable && selected.includes(node.id)" :value="texts[node.id] || ''" :disabled="disabled" @input="emit('text', { id: node.id, text: $event.target.value })" placeholder="填写这段画面要讲的内容；留空则不合成" />
      <CompositionTree v-if="!collapsed[node.id] && node.children?.length" :nodes="node.children" :selected="selected" :texts="texts" :editable="editable" :disabled="disabled" @pick="emit('pick', $event)" @text="emit('text', $event)" @preview="emit('preview', $event)" />
    </div>
  </div>
</template>
<style scoped>
.composition-tree { border-left:1px solid #d7d0e4; padding-left:14px; margin-top:8px }
.composition-node { padding:8px 0 }.row { display:flex; align-items:center; gap:10px }.row strong { flex:1; font-size:13px; font-weight:550 }.row span, small { font-size:11px; opacity:.65; font-variant-numeric:tabular-nums }.caret { padding:2px; background:none; border:none } small { display:block; margin:4px 0 }.notes { font-size:12px; color:#777; margin:6px 0 } textarea { width:100%; min-height:64px; padding:8px; border:1px solid #d7d0e4; border-radius:6px; box-sizing:border-box; background:var(--panel,#fff); color:inherit }
</style>
