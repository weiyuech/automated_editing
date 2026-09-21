<script setup>
import { onUnmounted, ref, nextTick } from 'vue'
const props = defineProps({ api: {type:Function, required:true} })
const dialog = ref(null), labels = ref([]), selected = ref([]), names = ref([])
const adding = ref(false), draft = ref(''), error = ref(''), busy = ref(false), editing = ref(false)
let resolveChoice = null
async function choose(fileNames, initial = [], edit = false) {
  if (resolveChoice) finish(null)
  labels.value = await props.api('/music/labels')
  names.value = fileNames; selected.value = [...initial]; editing.value = edit
  adding.value = false; draft.value = ''; error.value = ''
  await nextTick(); dialog.value.showModal()
  return new Promise(resolve => { resolveChoice = resolve })
}
function finish(value) {
  dialog.value?.close()
  resolveChoice?.(value === null ? null : [...value]); resolveChoice = null
}
function toggle(label) { selected.value = selected.value.includes(label) ? selected.value.filter(t => t !== label) : [...selected.value, label] }
async function createLabel() {
  if (!draft.value.trim() || busy.value) return
  busy.value = true; error.value = ''
  try {
    const name = draft.value.trim()
    labels.value = await props.api('/music/labels', {method:'POST', body:JSON.stringify({name})})
    if (!selected.value.includes(name)) selected.value.push(name)
    adding.value = false; draft.value = ''
  } catch (err) { error.value = err.message } finally { busy.value = false }
}
onUnmounted(() => finish(null))
defineExpose({choose})
</script>
<template>
  <dialog ref="dialog" class="music-label-dialog" aria-labelledby="music-label-title" @cancel.prevent="!busy && finish(null)">
    <header class="iridescent-header"><strong id="music-label-title">{{ editing ? '编辑音乐标签' : '为音乐选择标签' }}</strong><button :disabled="busy" aria-label="关闭" @click="finish(null)">×</button></header>
    <div class="music-label-body">
      <p class="music-file-names">{{ names.join('、') }}</p>
      <p class="label-title">音乐标签 <small>可选，可多选</small></p>
      <div class="label-choices"><button v-for="label in labels" :key="label" :disabled="busy" :class="{selected:selected.includes(label)}" :aria-pressed="selected.includes(label)" @click="toggle(label)">{{ label }}</button><button :disabled="busy" class="new-label" @click="adding = !adding">＋ 新建标签</button></div>
      <form v-if="adding" class="label-form" @submit.prevent="createLabel"><input v-model="draft" :disabled="busy" class="field" placeholder="输入标签名称" aria-label="新标签名称" maxlength="20"><button type="submit" :disabled="busy || !draft.trim()">{{ busy ? '保存中…' : '添加' }}</button></form>
      <p v-if="error" class="label-error" role="alert">{{ error }}</p>
      <p class="label-hint">{{ selected.length ? '已选择：' + selected.join('、') : '不选标签，将归入「未分类」。' }}</p>
      <footer><button :disabled="busy" @click="finish(null)">取消</button><button v-if="!editing" :disabled="busy" @click="finish([])">跳过标签</button><button class="primary" :disabled="busy || selected.length > 20" @click="finish(selected)">{{ editing ? '保存' : '继续导入' }}</button></footer>
    </div>
  </dialog>
</template>
<style scoped>
.music-label-dialog{padding:0;width:min(540px,calc(100vw - 48px));max-height:80vh;overflow:auto;border:1px solid var(--border-strong);border-radius:12px;background:var(--tree-leaf,#f4effc);color:var(--text);box-shadow:0 20px 60px #35245035}
.music-label-dialog::backdrop{background:#281c4a55}
header{display:flex;align-items:center;justify-content:space-between;padding:12px 18px;background:var(--iridescent-header-background);font-size:15px}header button{padding:2px 9px;border:0;background:transparent;font-size:20px}.music-label-body{padding:18px}.music-file-names{font-size:13px;color:var(--text-muted);overflow-wrap:anywhere;max-height:90px;overflow:auto;margin:0 0 18px}.label-title{font-size:13px}.label-title small{color:var(--text-muted)}.label-choices{display:flex;gap:8px;flex-wrap:wrap;max-height:220px;overflow:auto}.label-choices button{font-size:13px;padding:7px 13px;background:var(--tree-leaf)}.label-choices .selected{background:#ddcef3;border-color:#9d80cb;color:#493174}.new-label{border-style:dashed}.label-form{display:flex;gap:8px;margin-top:12px}.label-form input{flex:1;min-width:0}.label-hint,.label-error{font-size:12px;line-height:1.6;color:var(--text-muted)}.label-error{color:#aa3158}footer{display:flex;gap:8px;justify-content:flex-end;margin-top:22px}footer button{font-size:13px;padding:8px 13px}
</style>
