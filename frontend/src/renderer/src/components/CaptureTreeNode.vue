<script setup>
import { computed, ref } from 'vue'
import { captureNodeState, formatCaptureTime } from '../capture-group-policy.js'
const props = defineProps({ node: Object, group: Object, selection: Object, disabled: Boolean, readOnly: Boolean, depth: { type: Number, default: 0 } })
const emit = defineEmits(['change', 'preview'])
const open = ref(!props.readOnly)
const state = computed(() => captureNodeState(props.group, props.selection, props.node))
function onToggle(event) {
  const checked = event.target.checked
  if (checked && props.node.kind === 'preparation') {
    const confirmed = window.confirm('该区间为“准备”镜头（云台就位或回原点），通常不应进入成片。仍要选入吗？')
    if (!confirmed) {
      event.target.checked = false
      return
    }
  }
  emit('change', { id: props.node.id, checked })
}
</script>
<template>
  <div class="tree-node" :class="{ 'tree-node-point': depth === 0 }">
    <div class="node-row" :class="{ 'node-row-point': depth === 0 }">
      <button v-if="node.children?.length" class="disclosure" :aria-label="`${open ? '收起' : '展开'}${node.label}`" :aria-expanded="open" @click="open = !open">{{ open ? '▾' : '▸' }}</button>
      <component :is="readOnly ? 'div' : 'label'" class="node-label"><input v-if="!readOnly" type="checkbox" :checked="state.checked" :indeterminate="state.partial" :disabled="disabled || (!group.master_available && !node.available)" @change="onToggle" />
        <span><strong>{{ node.label }}</strong><small>{{ formatCaptureTime(node.end - node.start) }} · {{ formatCaptureTime(node.start) }}–{{ formatCaptureTime(node.end) }}<em v-if="node.complete === false"> · 未完成</em></small></span>
      </component>
      <button :disabled="!group.master_available && !node.available" @click="emit('preview', node)">预览</button>
    </div>
    <div v-if="open && node.children?.length" class="children">
      <CaptureTreeNode v-for="child in node.children" :key="child.id" :node="child" :group="group" :selection="selection" :disabled="disabled" :read-only="readOnly" :depth="depth + 1" @change="emit('change', $event)" @preview="emit('preview', $event)" />
    </div>
  </div>
</template>
<style scoped>
.tree-node { background:var(--tree-leaf) }
.tree-node-point { background:var(--tree-branch) }
.node-row { display:flex; gap:8px; align-items:center; padding:10px; border-top:1px solid var(--border,#ded9ec); background:var(--tree-leaf) }
.node-row-point { background:var(--tree-branch) }
.node-label { display:flex; gap:10px; flex:1; align-items:center; min-width:0 }
.node-label > span { min-width:0 }
.node-row input, .node-row button { flex:none }
.node-row button { padding:6px 10px; font-size:12px; white-space:nowrap }
strong { display:block; font-size:12px; font-weight:500; overflow-wrap:break-word } small { display:block; color:var(--text-muted); font-size:12px; font-variant-numeric:tabular-nums; margin-top:3px } em { color:#b34c4c }
.children { margin-left:24px; border-left:1px solid var(--border,#ded9ec) }
.node-row .disclosure { width:18px; padding:2px; border:0; background:transparent }
</style>
