<script setup>
import { ref } from 'vue'
import { formatCaptureTime } from '../capture-group-policy.js'
defineProps({ nodes: Array, selected: Array, texts: Object, editable: Boolean, disabled: Boolean, depth: { type: Number, default: 0 } })
const emit = defineEmits(['pick', 'text', 'preview'])
const collapsed = ref({})
</script>
<template>
  <div class="composition-tree">
    <div v-for="node in nodes" :key="node.id" class="composition-node">
      <div class="row" :class="{ 'row-root': depth === 0, 'row-branch': depth === 1 }">
        <button v-if="node.children?.length" class="caret" @click="collapsed[node.id] = !collapsed[node.id]">{{ collapsed[node.id] ? '▸' : '▾' }}</button>
        <input v-if="editable && node.kind !== 'preparation'" type="checkbox" :aria-label="`为 ${node.label} 配旁白`" :checked="selected.includes(node.id)" :disabled="disabled" @change="emit('pick', { node, checked: $event.target.checked })" />
        <div class="node-copy">
          <strong>{{ node.label }}</strong>
          <small>成片 {{ formatCaptureTime(node.start) }}–{{ formatCaptureTime(node.end) }}</small>
        </div>
        <span class="duration">{{ formatCaptureTime(node.duration) }}</span>
        <button class="preview" @click="emit('preview', node)">预览此层</button>
      </div>
      <p v-if="node.notes?.length" class="notes">拍摄备注：{{ node.notes.join('；') }}</p>
      <textarea v-if="editable && selected.includes(node.id)" :value="texts[node.id] || ''" :disabled="disabled" @input="emit('text', { id: node.id, text: $event.target.value })" placeholder="填写这段画面要讲的内容；留空则不合成" />
      <CompositionTree v-if="!collapsed[node.id] && node.children?.length" :nodes="node.children" :selected="selected" :texts="texts" :editable="editable" :disabled="disabled" :depth="depth + 1" @pick="emit('pick', $event)" @text="emit('text', $event)" @preview="emit('preview', $event)" />
    </div>
  </div>
</template>
<style scoped>
.composition-tree { border-left: 1px solid var(--border); padding-left: 14px; margin-top: 4px; background: var(--tree-leaf); }
.composition-node { padding: 2px 0; }
.row { display: flex; align-items: center; gap: 8px; padding: 6px 8px; border-radius: 6px; background: var(--tree-leaf); }
.row-root { background: var(--tree-root); }
.row-branch { background: var(--tree-branch); }
.node-copy { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.node-copy strong { font-size: 12px; font-weight: 500; line-height: 1.4; overflow-wrap: anywhere; }
.row-root .node-copy strong { font-size: 15px; }
.duration, .node-copy small { font-size: 12px; color: var(--text-muted); font-variant-numeric: tabular-nums; line-height: 1.4; }
.duration { flex: none; white-space: nowrap; }
.row button, .row input { flex: none; }
.preview { padding: 5px 8px; font-size: 12px; line-height: 1.4; white-space: nowrap; }
.caret { width: 24px; min-height: 24px; padding: 2px; font-size: 12px; line-height: 1; background: none; border: none; }
.notes { font-size: 12px; color: var(--text-muted); margin: 4px 8px; }
textarea { width: 100%; min-height: 64px; margin-top: 4px; padding: 8px; border: 1px solid var(--border); border-radius: 6px; box-sizing: border-box; background: var(--panel, #fff); color: inherit; }
</style>
