<script setup>
import { computed } from 'vue'
import { groupMusic } from '../music-labels'
const props = defineProps({ items: { type: Array, default: () => [] }, grouped: { type: Boolean, default: true } })
const groups = computed(() => groupMusic(props.items))
</script>
<template>
  <template v-if="grouped">
    <details v-for="group in groups" :key="group.label" class="music-label-group" open>
      <summary>{{ group.label }} <small>{{ group.items.length }} 首</small></summary>
      <slot v-for="item in group.items" :key="item.id" :item="item" />
    </details>
  </template>
  <template v-else><slot v-for="item in items" :key="item.id" :item="item" /></template>
</template>
<style scoped>
.music-label-group {
  border-bottom: 1px solid var(--border);
}
summary {
  cursor: pointer;
  padding: 9px 15px;
  background: var(--tree-branch, #ebe2f8);
  font-size: 13px;
  line-height: 1.5;
  color: #494252;
  font-weight: 500;
}
summary::marker {
  color: #8c819d;
  font-size: 10px;
}
summary small {
  margin-left: 8px;
  font-weight: 400;
  font-size: 12px;
  color: #7c7488;
  font-variant-numeric: tabular-nums;
}
summary:hover {
  background: #e8dff3;
}
summary:focus-visible {
  outline: 2px solid #b5a0d9;
  outline-offset: -2px;
}
</style>
