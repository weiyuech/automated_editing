<script setup>
import { computed, onUnmounted, ref, watch } from 'vue'
const props = defineProps({ modelValue: String, fonts: Array, disabled: Boolean })
const emit = defineEmits(['update:modelValue'])
const menu = ref(null)
const loaded = ref(new Set())
const faces = new Map()
let disposed = false
const selected = computed(() =>
  props.fonts.find((font) => font.key === props.modelValue),
)
const family = (key) => `ave-preview-${key.replace(/[^a-z0-9_]/gi, '')}`
const appearance = (key) =>
  loaded.value.has(key) ? { fontFamily: `"${family(key)}", sans-serif` } : {}
watch(
  () => props.fonts,
  async (fonts) => {
    for (const font of fonts) {
      if (
        !font.preview?.startsWith('data:font/woff2;base64,') ||
        faces.has(font.key)
      )
        continue
      const face = new FontFace(family(font.key), `url(${font.preview})`)
      faces.set(font.key, face)
      try {
        await face.load()
        if (disposed) return
        document.fonts.add(face)
        loaded.value = new Set([...loaded.value, font.key])
      } catch {
        /* The name remains usable even if its preview resource is missing. */
      }
    }
  },
  { immediate: true },
)
watch(
  () => props.disabled,
  (value) => {
    if (value && menu.value) menu.value.open = false
  },
)
function close() {
  menu.value.open = false
  menu.value.querySelector('summary')?.focus()
}
function pick(font) {
  emit('update:modelValue', font.key)
  close()
}
function navigate(event) {
  if (event.key === 'Escape') {
    event.preventDefault()
    close()
    return
  }
  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
  event.preventDefault()
  const options = [...menu.value.querySelectorAll('button:not(:disabled)')]
  const index = options.indexOf(document.activeElement)
  const next =
    event.key === 'Home'
      ? 0
      : event.key === 'End'
        ? options.length - 1
        : (index + (event.key === 'ArrowDown' ? 1 : -1) + options.length) %
          options.length
  options[next]?.focus()
}
onUnmounted(() => {
  disposed = true
  for (const face of faces.values()) document.fonts.delete(face)
})
</script>
<template>
  <div class="font-picker">
    <span>字体</span>
    <details ref="menu" @keydown="navigate">
      <summary
        class="field"
        :aria-disabled="disabled"
        @click="disabled && $event.preventDefault()"
      >
        <span :style="appearance(modelValue)">{{
          selected?.label || '选择字体'
        }}</span>
        <span aria-hidden="true">⌄</span>
      </summary>
      <div class="choices" aria-label="字幕字体">
        <button
          v-for="font in fonts"
          :key="font.key"
          type="button"
          :disabled="disabled || !font.installed"
          :aria-label="font.label"
          :aria-pressed="modelValue === font.key"
          @click="pick(font)"
        >
          <span :style="appearance(font.key)">{{ font.label }}</span>
          <span v-if="modelValue === font.key" class="selected" aria-label="已选"
            >✓</span
          >
          <small :style="appearance(font.key)">{{
            loaded.has(font.key)
              ? '机器人从大厅出发，缓缓驶过展区。'
              : '字体预览暂不可用'
          }}</small>
          <small v-if="!font.installed">未安装</small>
        </button>
      </div>
    </details>
    <p
      v-if="selected && loaded.has(modelValue)"
      class="sample"
      :style="appearance(modelValue)"
    >
      机器人从大厅出发，缓缓驶过展区。
    </p>
  </div>
</template>
<style scoped>
.font-picker {
  min-width: 0;
}
details {
  position: relative;
  margin-top: 6px;
}
summary {
  list-style: none;
  display: flex;
  justify-content: space-between;
  align-items: center;
  cursor: pointer;
}
summary::-webkit-details-marker {
  display: none;
}
summary[aria-disabled='true'] {
  opacity: 0.5;
  cursor: default;
}
.choices {
  position: absolute;
  z-index: 5;
  width: 100%;
  min-width: 220px;
  padding: 6px;
  border: 1px solid var(--border-strong);
  border-radius: 10px;
  background: var(--lavender);
  box-shadow: 0 8px 24px #211a4320;
}
.choices button {
  width: 100%;
  text-align: left;
  margin: 2px 0;
  font-size: 17px;
}
.choices button[aria-pressed='true'] {
  border-color: var(--purple);
  background: #785cff12;
}
.choices small {
  display: block;
  font-size: 13px;
  margin-top: 6px;
  line-height: 1.6;
}
.selected {
  float: right;
  color: var(--purple-strong);
}
.sample {
  font-size: 17px;
  line-height: 1.8;
  color: var(--text-soft);
  margin: 8px 0 0;
}
</style>
