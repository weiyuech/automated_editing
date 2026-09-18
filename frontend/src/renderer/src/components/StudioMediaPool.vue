<script setup>
import { computed, ref } from 'vue'
const props = defineProps({
  sources: Array,
  music: Array,
  voices: Array,
  effects: Array,
  selectedSources: Array,
  selectedMusic: Array,
  selectedVoices: Array,
  selectedIntro: Array,
  selectedOutro: Array,
  busy: Boolean,
})
const emit = defineEmits([
  'add',
  'remove',
  'clear',
  'source',
  'music',
  'voice',
  'intro',
  'outro',
  'preview',
  'select-all',
  'deselect-all',
])
const search = ref('')
function visibleItems(group) {
  return group.key === 'source'
    ? group.items.filter((i) =>
        name(i).toLowerCase().includes(search.value.toLowerCase()),
      )
    : group.items
}
const groups = computed(() => [
  {
    key: 'source',
    title: '源视频素材池',
    items: props.sources,
    selected: props.selectedSources,
  },
  {
    key: 'music',
    title: '音乐池',
    items: props.music,
    selected: props.selectedMusic,
  },
  {
    key: 'voiceover',
    title: '旁白池',
    items: props.voices,
    selected: props.selectedVoices,
  },
  {
    key: 'effect',
    title: '特效池',
    items: props.effects,
    selected: [...new Set([...props.selectedIntro, ...props.selectedOutro])],
  },
])
function name(i) {
  return i.path.split(/[\\/]/).pop()
}
function pick(key, item, checked) {
  emit(key === 'voiceover' ? 'voice' : key, item.id, checked)
}
</script>
<template>
  <section class="studio-pane media-pool-pane">
    <div class="panel-title studio-pane-title">媒体池</div>
    <div class="media-pool-scroll">
      <div class="media-pool-stack">
        <section
          v-for="group in groups"
          :key="group.key"
          class="pool-section"
          :class="{ 'source-video-pool': group.key === 'source' }"
        >
          <header>
            <div>
              <strong>{{ group.title }}</strong
              ><small
                >已入池 {{ group.items.length }} · 已选用
                {{ group.selected.length }}</small
              >
            </div>
            <div class="actions">
              <button :disabled="busy" @click="emit('add', group.key)">
                从媒体库添加</button
              ><button
                v-if="group.key === 'source'"
                :disabled="busy"
                @click="emit('select-all')"
              >
                全选</button
              ><button
                v-if="group.key === 'source'"
                :disabled="busy || !selectedSources.length"
                @click="emit('deselect-all')"
              >
                取消全选</button
              ><button
                :disabled="busy || !group.items.length"
                @click="emit('clear', group.key)"
              >
                清空媒体池
              </button>
            </div>
          </header>
          <p v-if="group.key === 'source'" class="hint">
            组合视频作为一个完整输入。录制树的选择与确认在媒体库完成。
          </p>
          <p v-if="group.key === 'music'" class="hint">
            本次选用一首，从头播放，按成片时长截取。
          </p>
          <p v-if="group.key === 'voiceover'" class="hint">
            绑定旁白跟随组合同步选用；普通视频可另选一条普通旁白。
          </p>
          <p v-if="group.key === 'effect'" class="hint">
            本次分别选用一个片头和一个片尾，可留空。
          </p>
          <input
            v-if="group.key === 'source'"
            v-model="search"
            class="field pool-search"
            placeholder="筛选文件名"
            aria-label="筛选源视频文件名"
          />
          <div class="pool-rows">
            <p v-if="!group.items.length" class="empty">
              媒体池为空，请从媒体库添加。
            </p>
            <div
              v-for="item in visibleItems(group)"
              :key="item.id"
              class="pool-row"
            >
              <label v-if="group.key !== 'effect'"
                ><input
                  type="checkbox"
                  :checked="group.selected.includes(item.id)"
                  :disabled="busy"
                  @change="pick(group.key, item, $event.target.checked)"
                /><span
                  >{{ name(item)
                  }}<small v-if="item.metadata?.bound_voice_id"
                    >与旁白联动</small
                  ><small v-if="item.metadata?.bound_source_id"
                    >与组合联动</small
                  ></span
                ></label
              ><span v-else class="effect-name">{{ name(item) }}</span>
              <div v-if="group.key === 'effect'" class="effect-checks">
                <label
                  ><input
                    type="checkbox"
                    :checked="selectedIntro.includes(item.id)"
                    :disabled="busy"
                    @change="emit('intro', item.id, $event.target.checked)"
                  />片头</label
                ><label
                  ><input
                    type="checkbox"
                    :checked="selectedOutro.includes(item.id)"
                    :disabled="busy"
                    @change="emit('outro', item.id, $event.target.checked)"
                  />片尾</label
                >
              </div>
              <div class="row-actions">
                <button @click="emit('preview', item)">
                  {{ item.kind === 'audio' ? '试听' : '预览' }}</button
                ><button
                  :disabled="busy"
                  @click="emit('remove', group.key, item.id)"
                >
                  移出
                </button>
              </div>
            </div>
          </div>
        </section>
      </div>
    </div>
  </section>
</template>
<style scoped>
.pool-search {
  width: calc(100% - 28px);
  margin: 0 14px 12px;
}
.pool-section {
  border: 1px solid var(--border);
  border-radius: 12px;
  overflow: hidden;
  background: var(--surface);
}
header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 16px;
  background: linear-gradient(
    110deg,
    color-mix(in srgb, var(--purple) 22%, var(--lavender)),
    color-mix(in srgb, var(--lilac) 26%, var(--lavender))
  );
  border-bottom: 1px solid var(--border-strong);
}
header strong {
  font-size: 15px;
  color: var(--purple-strong);
}
header small {
  display: block;
  font-size: 11px;
  color: var(--text-soft);
  margin-top: 4px;
}
.actions {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  justify-content: flex-end;
}
.actions button {
  font-size: 12px;
  padding: 7px 10px;
}
.hint {
  font-size: 11px;
  line-height: 1.6;
  color: var(--text-muted);
  margin: 0;
  padding: 0 16px 10px;
}
.pool-rows {
  max-height: 245px;
  overflow: auto;
}
.source-video-pool .pool-rows {
  max-height: 310px;
}
.pool-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 15px;
  border-top: 1px solid var(--border);
}
.pool-row > label {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: 1;
  min-width: 0;
  font-size: 13px;
}
.pool-row span {
  overflow-wrap: anywhere;
}
.pool-row small {
  display: block;
  color: var(--text-muted);
  font-size: 11px;
  margin-top: 4px;
}
.row-actions {
  display: flex;
  gap: 6px;
  margin-left: auto;
  flex-shrink: 0;
}
.row-actions button {
  font-size: 12px;
  padding: 5px 9px;
}
.effect-name {
  flex: 1;
  font-size: 13px;
}
.effect-checks {
  display: flex;
  gap: 10px;
  font-size: 12px;
  flex-shrink: 0;
}
.effect-checks label {
  display: flex;
  align-items: center;
  gap: 4px;
}
.empty {
  font-size: 12px;
  padding: 15px;
}
.media-pool-stack {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
</style>
