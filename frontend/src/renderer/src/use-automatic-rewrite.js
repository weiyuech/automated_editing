import { onScopeDispose, ref, watch } from 'vue'

// Enabling arms one request. Missing input may become ready later; unrelated
// context refreshes and the request's own busy state must never generate again.
export function useAutomaticRewrite({ enabled, ready, run }) {
  const pending = ref(false)
  const rewriting = ref(false)
  let active = true
  const stopEnabled = watch(enabled, value => { pending.value = value }, { flush: 'sync' })
  const stopReady = watch([pending, ready, rewriting], async ([armed, canRun, inFlight]) => {
    if (!active || !enabled.value || !armed || !canRun || inFlight) return
    pending.value = false
    rewriting.value = true
    try {
      await run()
    } finally {
      rewriting.value = false
    }
  })
  onScopeDispose(() => {
    active = false
    stopEnabled()
    stopReady()
  })
  return { pending, rewriting }
}
