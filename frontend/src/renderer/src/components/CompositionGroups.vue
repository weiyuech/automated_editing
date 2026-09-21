<script setup>
import { computed, ref, watch } from 'vue'
import { groupCompositions, selectCompositionGroup, selectCompositionChild, compositionLabel, durationLabel, compositionPieces } from '../composition-groups.js'
const props = defineProps({ items: { type: Array, default: () => [] }, originals: { type: Array, default: () => [] }, modelValue: { type: Array, default: () => [] }, selectable: Boolean, disabled: Boolean })
const emit = defineEmits(['update:modelValue', 'preview'])
const groups = computed(() => groupCompositions(props.items, props.originals))
const expanded = ref([]), detail = ref('')
watch(groups, value => { if (!expanded.value.length && value.length) expanded.value = [value[0].id] }, { immediate: true })
function toggle(id) { expanded.value = expanded.value.includes(id) ? expanded.value.filter(value => value !== id) : [...expanded.value, id] }
function checked(group) { return group.children.every(child => props.modelValue.includes(child.id)) }
function partial(group) { return !checked(group) && group.children.some(child => props.modelValue.includes(child.id)) }
function chooseGroup(group, value) {
  emit('update:modelValue', selectCompositionGroup(groups.value, props.modelValue, group.id, value))
  if (value && !expanded.value.includes(group.id)) expanded.value.push(group.id)
}
</script>
<template>
  <div class="grouped-results">
    <section v-for="group in groups" :key="group.id" class="result-group">
      <div class="result-parent">
        <input v-if="selectable" type="checkbox" :aria-label="'全选 ' + group.name" :checked="checked(group)" :indeterminate="partial(group)" :disabled="disabled" @change="chooseGroup(group, $event.target.checked)" />
        <button class="result-disclosure" :aria-expanded="expanded.includes(group.id)" @click="toggle(group.id)">{{ expanded.includes(group.id) ? '▾' : '▸' }} {{ group.name }}</button>
        <small>{{ group.children.length }} 条组合</small>
      </div>
      <div v-if="expanded.includes(group.id)" class="result-children">
        <template v-for="child in group.children" :key="child.id">
          <div class="result-child">
            <input v-if="selectable" type="checkbox" :checked="modelValue.includes(child.id)" :disabled="disabled" :aria-label="compositionLabel(child)" @change="emit('update:modelValue', selectCompositionChild(groups, modelValue, child.id, $event.target.checked))" />
            <span class="result-title">{{ compositionLabel(child) }}<small>{{ compositionPieces(child).length }} 段 · {{ durationLabel(child) }}</small></span>
            <small v-if="child.metadata?.bound_voice_id" class="bound-label">已绑定旁白</small>
            <button :aria-expanded="detail === child.id" @click="detail = detail === child.id ? '' : child.id">{{ detail === child.id ? '收起' : '查看' }}</button>
            <slot name="actions" :item="child" />
          </div>
          <div v-if="detail === child.id" class="result-detail">
            <div v-for="piece in compositionPieces(child)" :key="piece.id"><span>{{ piece.label }}</span><small>{{ Number(piece.duration || piece.end - piece.start).toFixed(1) }} 秒</small></div>
            <button @click="emit('preview', child)">预览组合</button>
          </div>
        </template>
      </div>
    </section>
    <p v-if="!groups.length" class="form-hint">先在「选择与组合预览」保存组合。</p>
  </div>
</template>
<style scoped>
.grouped-results{display:grid;gap:8px}.result-group{border:1px solid var(--border);border-radius:10px;overflow:hidden;background:var(--tree-leaf,#f6f1fc)}.result-parent{display:flex;align-items:center;gap:10px;padding:9px 12px;background:var(--tree-root,#dfd0f3)}.result-disclosure{flex:1;min-width:0;text-align:left;border:0;background:transparent;box-shadow:none;padding:3px 0;font-size:15px;font-weight:600}.result-parent small{font-size:12px;white-space:nowrap;color:var(--text-soft)}.result-children{padding-left:22px;background:var(--tree-branch,#ede3f8)}.result-child{display:flex;align-items:center;gap:10px;padding:9px 12px;border-top:1px solid var(--border);font-size:12px;background:var(--tree-leaf,#f6f1fc)}.result-title{flex:1;min-width:0;overflow-wrap:anywhere}.result-title small{margin-left:12px;color:var(--text-muted)}.result-child button,.result-detail button{font-size:12px;padding:5px 10px}.bound-label{color:#7052a0}.result-detail{padding:6px 14px 6px 24px;font-size:12px;background:#faf6ff}.result-detail>div{display:flex;justify-content:space-between;gap:12px;padding:6px 0}
</style>
