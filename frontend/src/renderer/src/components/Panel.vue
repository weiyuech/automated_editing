<template>
  <div class="panel">
    <div class="panel-title">{{ title }}</div>
    <slot />
  </div>
</template>

<script setup>
defineProps({ title: { type: String, required: true } })
</script>
