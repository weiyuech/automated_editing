import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { groupCompositions, savedCompositions } from './composition-groups.js'
import { narrationRequest, draftIdentity, batchNarrationItem, needsNarrationInstructions } from './grouped-narration.js'

export function useGroupedNarration(props, emit) {
  const items = ref([]), records = ref([]), chosen = ref([]), inspect = ref('')
  const source = ref(''), instructions = ref(''), llm = ref(false)
  const style = ref('natural'), applyStyle = ref(false), custom = ref(false), system = ref('')
  const showPrompt = ref(false), prompt = ref(null), promptError = ref(''), promptLoading = ref(false)
  const rate = ref(1), autoTempo = ref(true), skip = ref(true)
  const editing = ref(false), rewriting = ref(false), submitting = ref(false), loading = ref(false)
  const error = ref(''), batch = ref(null), drafts = reactive({}), adjusting = ref('')
  let disposed = false, timer, promptTimer, promptRevision = 0, refreshing = null

  const compositions = computed(() => savedCompositions(items.value))
  const ordered = computed(() => groupCompositions(compositions.value, items.value).flatMap(group => group.children))
  const selected = computed(() => ordered.value.filter(item => chosen.value.includes(item.id)))
  const selectedComposition = computed(() => selected.value.find(item => item.id === inspect.value) || selected.value[0])
  const pending = computed(() => selected.value.filter(item => !skip.value || !item.metadata.bound_voice_id))
  const running = computed(() => submitting.value || Boolean(adjusting.value) || batch.value?.status === 'running')
  const locked = computed(() => running.value || rewriting.value)
  const missingNotes = computed(() => pending.value.some(needsNarrationInstructions))
  const settings = computed(() => ({ source: source.value, instructions: instructions.value,
    llm: llm.value, style: style.value, applyStyle: applyStyle.value, custom: custom.value, system: system.value }))
  function requestFor(item) { return narrationRequest(settings.value, selected.value.findIndex(value => value.id === item.id)) }
  function identity(item) { return draftIdentity(item, requestFor(item)) }
  function currentDraft(item) {
    if (!item) return null
    const draft = item && drafts[item.id]
    return draft?.key === identity(item) ? draft : null
  }
  const inspectedDraft = computed(() => currentDraft(selectedComposition.value))
  const recordById = computed(() => new Map(records.value.map(record => [record.id, record])))
  function narration(item) { return recordById.value.get(item.metadata.composition_id)?.narration }
  function boundVoice(item) {
    return items.value.find(voice => voice.id === item.metadata.bound_voice_id)
  }
  const inspectedNarration = computed(() => selectedComposition.value && narration(selectedComposition.value))
  const scriptChanged = computed(() => {
    const desired = llm.value && inspectedDraft.value?.version === 'draft' ? inspectedDraft.value.text : source.value
    return Boolean(desired.trim() && inspectedNarration.value?.text && desired !== inspectedNarration.value.text)
  })
  const canAdjust = computed(() => !locked.value && !scriptChanged.value && inspectedNarration.value?.source_seconds
    && (rate.value !== inspectedNarration.value.playback_rate || autoTempo.value !== inspectedNarration.value.auto_tempo
      || ['needs_revision', 'pending_review'].includes(inspectedNarration.value.status)))
  const batchItems = computed(() => new Map((batch.value?.items || []).map(item => [item.composition_id, item])))
  function batchState(item) { return batchItems.value.get(item.metadata.composition_id) }
  const voices = computed(() => items.value.filter(item => item.metadata?.role === 'tts_voice'))

  async function refresh({ resume = false } = {}) {
    if (refreshing) return refreshing
    refreshing = (async () => {
      const [media, saved, batches] = await Promise.all([
        props.api('/media'), props.api('/compositions'),
        resume ? props.api('/narration/batches') : Promise.resolve(null),
      ])
      if (disposed) return
      records.value = saved
      if (resume && !adjusting.value) {
        const runningRecord = saved.find(record => record.narration?.status === 'running')
        if (runningRecord && !batches?.some(value => value.status === 'running')) adjusting.value = runningRecord.id
      }
      const signatures = new Map(saved.map(record => [record.id, record.visual_signature]))
      items.value = media.map(item => item.metadata?.composition_id ? {
        ...item, metadata: { ...item.metadata, visual_signature: item.metadata.visual_signature || signatures.get(item.metadata.composition_id) },
      } : item)
      chosen.value = chosen.value.filter(id => items.value.some(item => item.id === id))
      if (batches?.length && !batch.value) {
        batch.value = batches.find(value => value.status === 'running') || batches[0]
        if (batch.value.status === 'running' || (!chosen.value.length && !props.initialCompositionIds?.length)) {
          const keys = new Set(batch.value.items.map(item => item.composition_id))
          chosen.value = items.value.filter(item => keys.has(item.metadata?.composition_id)).map(item => item.id)
        }
      }
    })()
    try { await refreshing } finally { refreshing = null }
  }
  function applyInitialSelection() {
    if (running.value || !props.initialCompositionIds?.length) return
    const keys = new Set(props.initialCompositionIds)
    const group = groupCompositions(compositions.value, items.value)
      .find(value => value.children.some(item => keys.has(item.metadata.composition_id)))
    if (group) chosen.value = group.children.filter(item => keys.has(item.metadata.composition_id)).map(item => item.id)
  }
  watch(() => props.initialCompositionIds, applyInitialSelection)
  watch(running, value => emit('busy', value), { immediate: true })
  watch(llm, value => {
    if (value) {
      // The switch is an explicit retry after a failure; routine refreshes never retry.
      for (const item of pending.value) if (drafts[item.id]?.error) delete drafts[item.id]
    }
  })
  const rewriteKey = computed(() => JSON.stringify(pending.value.map(item => identity(item))))
  const rewriteReady = computed(() => props.active && llm.value && !locked.value && !loading.value && !editing.value
    && pending.value.length > 0 && pending.value.length <= 200 && source.value.trim()
    && (!missingNotes.value || instructions.value.trim()) && (!custom.value || system.value.trim()))

  // One call per distinct input snapshot. Inspection and polling never arm another request.
  watch([rewriteKey, rewriteReady], async () => {
    if (!rewriteReady.value) return
    const item = pending.value.find(value => !currentDraft(value))
    if (!item) return
    const request = requestFor(item), key = identity(item)
    rewriting.value = true
    drafts[item.id] = { key, text: '', version: '', loading: true, error: '' }
    try {
      const data = await props.api(`/compositions/${item.metadata.composition_id}/narration/draft`, {
        method: 'POST', body: JSON.stringify(request),
      })
      if (!disposed && llm.value && key === identity(item)) {
        drafts[item.id] = { key, text: data.text, sections: data.sections || [],
          note_assignments: data.note_assignments || [], general_notes: data.general_notes || [],
          prompt_trace: data.prompt_trace || [],
          style: request.narration_style || null, version: '', loading: false, error: '' }
      } else if (drafts[item.id]?.key === key) delete drafts[item.id]
    } catch (reason) {
      if (!disposed && drafts[item.id]?.key === key) {
        drafts[item.id] = { key, text: '', version: '', loading: false, error: reason.message }
      }
    } finally { rewriting.value = false }
  })

  const promptKey = computed(() => selectedComposition.value ? identity(selectedComposition.value) : '')
  watch([promptKey, showPrompt, () => props.active], () => {
    clearTimeout(promptTimer)
    const revision = ++promptRevision
    prompt.value = null
    promptError.value = ''
    promptLoading.value = false
    if (!showPrompt.value || !props.active || !selectedComposition.value) return
    const item = selectedComposition.value, request = requestFor(item)
    promptLoading.value = true
    promptTimer = setTimeout(async () => {
      try {
        const result = await props.api(`/compositions/${item.metadata.composition_id}/narration/prompt`, {
          method: 'POST', body: JSON.stringify(request),
        })
        if (!disposed && revision === promptRevision) prompt.value = result
      } catch (reason) {
        if (!disposed && revision === promptRevision) promptError.value = reason.message
      } finally { if (revision === promptRevision) promptLoading.value = false }
    }, 200)
  })
  function enableCustom(value) {
    if (value && !system.value) system.value = prompt.value?.baseline_system || ''
    custom.value = value
  }
  function restorePrompt() { custom.value = false; system.value = '' }
  function chooseVersion(value) { if (inspectedDraft.value) inspectedDraft.value.version = value }
  function editDraft(value) {
    if (inspectedDraft.value) {
      inspectedDraft.value.text = value
      inspectedDraft.value.version = ''
    }
  }
  const generationItems = computed(() => {
    try {
      return pending.value.map(item => batchNarrationItem(item, currentDraft(item), requestFor(item), {
        source: source.value, llm: llm.value, rate: rate.value, autoTempo: autoTempo.value,
      }))
    } catch { return null }
  })
  const canGenerate = computed(() => !locked.value && !loading.value && pending.value.length > 0
    && pending.value.length <= 200 && generationItems.value !== null)
  async function generate() {
    if (!canGenerate.value) return
    const snapshot = generationItems.value
    submitting.value = true
    error.value = ''
    try {
      batch.value = await props.api('/narration/batches', {
        method: 'POST', body: JSON.stringify({ items: snapshot, skip_existing: skip.value }),
      })
    } catch (reason) { error.value = reason.message }
    finally { submitting.value = false }
  }
  async function adjust() {
    if (!canAdjust.value) return
    const key = selectedComposition.value.metadata.composition_id
    const request = { attempt_id: inspectedNarration.value.attempt_id, playback_rate: rate.value, auto_tempo: autoTempo.value }
    adjusting.value = key
    error.value = ''
    try {
      const result = await props.api(`/compositions/${key}/narration/adjust`, { method: 'POST', body: JSON.stringify(request) })
      const record = records.value.find(value => value.id === key)
      if (record) record.narration = result
      if (result.status !== 'running') { adjusting.value = ''; await refresh(); emit('changed') }
    } catch (reason) { error.value = reason.message; adjusting.value = '' }
  }
  async function poll() {
    if (disposed) return
    try {
      if (adjusting.value) {
        const key = adjusting.value
        const next = await props.api(`/compositions/${key}`)
        if (!disposed && adjusting.value === key) {
          const index = records.value.findIndex(record => record.id === key)
          if (index >= 0) records.value[index] = next
          if (next.narration?.status !== 'running') {
            adjusting.value = ''
            await refresh()
            emit('changed')
          }
        }
      }
      if (batch.value?.status === 'running') {
        const id = batch.value.id
        const next = await props.api(`/narration/batches/${id}`)
        if (!disposed && batch.value?.id === id) {
          batch.value = next
          await refresh()
          if (next.status !== 'running') emit('changed')
        }
      }
    } catch (reason) { if (!disposed) error.value = `更新生成进度失败：${reason.message}` }
    if (!disposed) timer = setTimeout(poll, 1500)
  }
  async function activate() {
    loading.value = true
    try { await refresh({ resume: true }); applyInitialSelection() }
    catch (reason) { if (!disposed) error.value = reason.message }
    finally { loading.value = false }
  }
  watch(() => props.active, value => { if (value) activate() }, { flush: 'sync' })
  onMounted(() => { activate(); poll() })
  onUnmounted(() => { disposed = true; clearTimeout(timer); clearTimeout(promptTimer); promptRevision++ })

  return { items, chosen, inspect, compositions, selected, selectedComposition, pending, source, instructions,
    llm, style, applyStyle, custom, system, showPrompt, prompt, promptError, promptLoading, rate, autoTempo,
    skip, editing, rewriting, loading, locked, running, error, batch, voices, missingNotes, inspectedDraft,
    canGenerate, requestFor, currentDraft, narration, batchState, enableCustom, restorePrompt, chooseVersion,
    editDraft, generate, boundVoice, inspectedNarration, scriptChanged, canAdjust, adjust }
}
