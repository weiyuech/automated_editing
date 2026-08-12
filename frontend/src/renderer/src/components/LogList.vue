<template>
  <div class="log-list">
    <div v-if="logs.length === 0" class="empty">暂无事件。</div>
    <div v-for="(entry, index) in logs" :key="index" class="log-entry">
      <span>{{ entry.time }}</span>
      <strong>{{ entry.message }}</strong>
    </div>
  </div>
</template>

<script setup>
defineProps({ logs: { type: Array, default: () => [] } })
</script>
