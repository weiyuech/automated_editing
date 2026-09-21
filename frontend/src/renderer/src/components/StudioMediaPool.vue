<script setup>
import { computed, ref } from 'vue'
import MusicGroups from './MusicGroups.vue'
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
  'labels',
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
          <header class="iridescent-header">
            <div class="pool-heading">
              <strong>{{ group.title }}</strong
              ><small
                >已入池 {{ group.items.length }} · 已选用
                {{ group.selected.length }}</small
              >
            </div>
            <div class="actions">
              <button class="pool-add" :disabled="busy" @click="emit('add', group.key)">
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
                class="pool-clear"
                :disabled="busy || !group.items.length"
                @click="emit('clear', group.key)"
              >
                清空媒体池
              </button>
            </div>
          </header>
          <div class="pool-tools">
          <p v-if="group.key === 'source'" class="hint">
            组合视频作为一个完整输入。录制树的选择与确认在媒体库完成。
          </p>
          <p v-if="group.key === 'music'" class="hint">
            本次选用一首，自动为您选取最合适的音乐片段。
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
          </div>
          <div class="pool-rows">
            <p v-if="!group.items.length" class="empty">
              媒体池为空，请从媒体库添加。
            </p>
            <MusicGroups :items="visibleItems(group)" :grouped="group.key === 'music'" v-slot="{ item }">
            <div class="pool-row" :class="{ selected: group.selected.includes(item.id) }">
              <label v-if="group.key !== 'effect'"
                ><input
                  type="checkbox"
                  :checked="group.selected.includes(item.id)"
                  :disabled="busy"
                  @change="pick(group.key, item, $event.target.checked)"
                /><span :title="name(item)"
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
                <button v-if="group.key === 'music'" :disabled="busy" @click="emit('labels', item)">标签</button>
                <button @click="emit('preview', item)">
                  {{ item.kind === 'audio' ? '试听' : '预览' }}</button
                ><button
                  class="row-remove"
                  :disabled="busy"
                  @click="emit('remove', group.key, item.id)"
                >
                  移出
                </button>
              </div>
            </div>
            </MusicGroups>
          </div>
        </section>
      </div>
    </div>
  </section>
</template>
<style scoped>
.pool-section {
  border: 1px solid #ded5ef;
  border-radius: 10px;
  overflow: hidden;
  background: #faf7ff;
}
header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 12px 14px;
  background: var(--iridescent-header-background);
  border-bottom: 1px solid #ddd1ee;
}
.pool-heading { flex-shrink: 0; }
header strong {
  font-size: 15px;
  font-weight: 600;
  color: #483769;
}
header small {
  display: block;
  font-size: 12px;
  color: #79698f;
  margin-top: 3px;
  font-variant-numeric: tabular-nums;
}
.actions {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-wrap: wrap;
  justify-content: flex-end;
}
.actions button {
  font-size: 12px;
  padding: 6px 8px;
  min-height: 30px;
  border: 1px solid transparent;
  background: transparent;
  color: #66547f;
  white-space: nowrap;
  border-radius: 6px;
}
.actions button:hover:not(:disabled) { background: #e0d3f0; }
.actions .pool-add {
  background: #f8f3ff;
  border-color: #cabbdf;
  color: #59417e;
  font-weight: 500;
  padding-inline: 10px;
  margin-right: 3px;
}
.actions .pool-clear:hover:not(:disabled),
.row-actions .row-remove:hover:not(:disabled) { color: #9d3d63; background: #f4e6ef; }
.pool-tools { padding: 10px 14px 12px; display: grid; gap: 9px; }
.hint { font-size: 12px; line-height: 1.6; color: #817397; margin: 0; }
.pool-search {
  width: 100%;
  margin: 0;
  padding: 7px 10px;
  font-size: 12px;
  line-height: 18px;
  border-color: #e2d9ef;
  background: #f5f0fb;
  border-radius: 6px;
}
.pool-search::placeholder { color: #9385a8; }
.pool-search:focus { outline: 2px solid #b5a0d9; outline-offset: 1px; }
.pool-rows { max-height: 245px; overflow: auto; }
.source-video-pool .pool-rows { max-height: 310px; }
.pool-row {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 46px;
  padding: 9px 14px;
  border-top: 1px solid #e8e0f2;
  transition: background 120ms ease;
}
.pool-row:hover { background: #f2ebfb; }
.pool-row.selected { background: #eee5f9; box-shadow: inset 2px 0 #9e83c9; }
.pool-row > label {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: 1;
  min-width: 0;
  font-size: 13px;
  color: #504164;
  cursor: pointer;
}
.pool-row input[type="checkbox"] { width: 15px; height: 15px; margin: 0; accent-color: #8a6cbc; flex-shrink: 0; }
.pool-row > label > span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pool-row small { display: block; color: var(--text-muted); font-size: 11px; margin-top: 3px; }
.row-actions { display: flex; gap: 4px; margin-left: auto; flex-shrink: 0; }
.row-actions button {
  font-size: 12px;
  padding: 4px 8px;
  min-height: 28px;
  border-color: #e0d5ef;
  background: #f7f2fd;
  color: #6b5688;
  border-radius: 6px;
}
.row-actions button:hover:not(:disabled) { background: #eae0f5; border-color: #cabbdf; }
.row-actions .row-remove { border-color: transparent; background: transparent; color: #8a7a9c; }
.effect-name { flex: 1; min-width: 0; font-size: 13px; overflow-wrap: anywhere; }
.effect-checks { display: flex; gap: 10px; font-size: 12px; flex-shrink: 0; }
.effect-checks label { display: flex; align-items: center; gap: 4px; }
.empty { font-size: 12px; color: #9182a5; margin: 0; padding: 10px 14px 16px; }
.media-pool-stack { display: flex; flex-direction: column; gap: 14px; }
@media (prefers-reduced-motion: reduce) { .pool-row { transition: none; } }
</style>
