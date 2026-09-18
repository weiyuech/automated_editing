/** Derive linked narration from selected videos; keep at most one ordinary voice. */
export function linkedVoiceSelection(
  sourceIds,
  sources,
  voices,
  currentVoiceIds,
) {
  const selected = new Set(sourceIds)
  const ordinary = voices
    .filter((v) => !v.metadata?.binding_id && currentVoiceIds.includes(v.id))
    .slice(0, 1)
    .map((v) => v.id)
  const available = new Set(voices.map((v) => v.id))
  const linked = sources
    .filter((s) => selected.has(s.id))
    .map((s) => s.metadata?.bound_voice_id)
    .filter((id) => id && available.has(id))
  return [...new Set([...ordinary, ...linked])]
}

export function poolWithoutVoice(pool, voiceIds, voices) {
  const removed = new Set(voiceIds)
  const sourceIds = new Set(
    voices
      .filter((v) => removed.has(v.id))
      .map((v) => v.metadata?.bound_source_id)
      .filter(Boolean),
  )
  return {
    ...pool,
    source_media_ids: pool.source_media_ids.filter((id) => !sourceIds.has(id)),
    voiceover_media_ids: pool.voiceover_media_ids.filter(
      (id) => !removed.has(id),
    ),
  }
}
