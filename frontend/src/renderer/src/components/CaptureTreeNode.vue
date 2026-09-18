<script setup>
import { computed, ref } from 'vue'
import { captureNodeState, formatCaptureTime } from '../capture-group-policy.js'
const props = defineProps({ node: Object, group: Object, selection: Object, disabled: Boolean })
const emit = defineEmits(['change', 'preview'])
const open = ref(true)
const state = computed(() => captureNodeState(props.group, props.selection, props.node))
</script>
<template>
  <div class="tree-node">
    <div class="node-row">
      <button v-if="node.children?.length" class="disclosure" :aria-expanded="open" @click="open = !open">{{ open ? '▾' : '▸' }}</button>
      <label><input type="checkbox" :checked="state.checked" :indeterminate="state.partial" :disabled="disabled || (!group.master_available && !node.available)" @change="emit('change', { id: node.id, checked: $event.target.checked })" />
        <span><strong>{{ node.label }}</strong><small>{{ formatCaptureTime(node.end - node.start) }} · {{ formatCaptureTime(node.start) }}–{{ formatCaptureTime(node.end) }}<em v-if="node.complete === false"> · 未完成</em></small></span>
      </label>
      <button :disabled="!group.master_available && !node.available" @click="emit('preview', node)">预览</button>
    </div>
    <div v-if="open && node.children?.length" class="children">
      <CaptureTreeNode v-for="child in node.children" :key="child.id" :node="child" :group="group" :selection="selection" :disabled="disabled" @change="emit('change', $event)" @preview="emit('preview', $event)" />
    </div>
  </div>
</template>
<style scoped>
.node-row { display:flex; gap:8px; align-items:center; padding:10px; border-top:1px solid var(--border,#ded9ec) }
.node-row label { display:flex; gap:10px; flex:1; align-items:center; min-width:0 }
strong { font-size:13px; font-weight:500 } small { display:block; opacity:.65; font-variant-numeric:tabular-nums; margin-top:3px } em { color:#b34c4c }
.children { margin-left:24px; border-left:1px solid var(--border,#ded9ec) }
.disclosure { padding:2px; border:0; background:transparent }
</style>
