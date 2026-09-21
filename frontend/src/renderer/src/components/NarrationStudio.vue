<script setup>
import { ref, watch } from 'vue'
import GroupedNarrationPanel from './GroupedNarrationPanel.vue'
import NarrationPanel from './NarrationPanel.vue'

const props = defineProps({
  api: Function,
  mediaUrl: Function,
  active: { type: Boolean, default: true },
  initialCompositionIds: { type: Array, default: () => [] },
})
const emit = defineEmits(['changed', 'preview-source'])
const mode = ref('group'), busy = ref(false), ordinaryOpened = ref(false)
watch(mode, value => { if (value === 'ordinary') ordinaryOpened.value = true })
watch(() => props.initialCompositionIds, values => { if (values?.length) mode.value = 'group' })
</script>

<template>
  <div class="wide narration-studio">
    <div class="segmented narration-mode">
      <button :class="{ active: mode === 'group' }" :disabled="busy" @click="mode = 'group'">按组合画面对齐</button>
      <button :class="{ active: mode === 'ordinary' }" :disabled="busy" @click="mode = 'ordinary'">普通旁白</button>
    </div>
    <GroupedNarrationPanel v-show="mode === 'group'" :api="api" :media-url="mediaUrl"
      :active="active && mode === 'group'" :initial-composition-ids="initialCompositionIds"
      @busy="busy = $event" @changed="emit('changed')" @preview-source="emit('preview-source', $event)" />
    <NarrationPanel v-if="ordinaryOpened" v-show="mode === 'ordinary'" :api="api" :media-url="mediaUrl"
      :active="active && mode === 'ordinary'" :initial-aligned="false" hide-mode @changed="emit('changed')" />
  </div>
</template>

<style scoped>
.narration-studio { min-width: 0; }
.narration-mode { margin-bottom: 16px; }
</style>
