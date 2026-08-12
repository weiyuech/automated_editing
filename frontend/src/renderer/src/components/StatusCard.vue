<template>
  <div class="status-card" :class="accent">
    <div class="status-title">{{ title }}</div>
    <div class="status-value">{{ value }}</div>
  </div>
</template>

<script setup>
defineProps({
  title: { type: String, required: true },
  value: { type: String, required: true },
  accent: { type: String, default: 'muted' }
})
</script>
