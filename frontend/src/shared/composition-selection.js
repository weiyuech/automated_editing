/** A binding is active only when both assets point to each other. */
export function activeVoiceSource(voice, sources) {
  if (!voice?.metadata?.binding_id) return null
  return sources.find((source) => source.id === voice.metadata.bound_source_id
    && source.metadata?.bound_voice_id === voice.id) || null
}

/** Derive linked narration from selected videos; keep at most one ordinary voice. */
export function linkedVoiceSelection(sourceIds, sources, voices, currentVoiceIds) {
  const selected = new Set(sourceIds)
  const ordinary = voices
    .filter((v) => !v.metadata?.binding_id && currentVoiceIds.includes(v.id))
    .slice(0, 1)
    .map((v) => v.id)
  const linked = voices.filter((voice) => {
    const source = activeVoiceSource(voice, sources)
    return source && selected.has(source.id)
  }).map((voice) => voice.id)
  return [...new Set([...ordinary, ...linked])]
}

export function poolWithItems(pool, kind, ids, sources, voices) {
  const next = {
    ...pool,
    source_media_ids: [...pool.source_media_ids],
    voiceover_media_ids: [...pool.voiceover_media_ids],
  }
  const field = `${kind === 'source' ? 'source' : 'voiceover'}_media_ids`
  next[field] = [...new Set([...next[field], ...ids])]
  for (const voice of voices) {
    const source = activeVoiceSource(voice, sources)
    if (!source) continue
    if (next.source_media_ids.includes(source.id) || next.voiceover_media_ids.includes(voice.id)) {
      if (!next.source_media_ids.includes(source.id)) next.source_media_ids.push(source.id)
      if (!next.voiceover_media_ids.includes(voice.id)) next.voiceover_media_ids.push(voice.id)
    }
  }
  return next
}

export function poolWithoutVoice(pool, voiceIds, voices, sources) {
  const removed = new Set(voiceIds)
  const sourceIds = new Set(voices.filter((v) => removed.has(v.id))
    .map((v) => activeVoiceSource(v, sources)?.id).filter(Boolean))
  return {
    ...pool,
    source_media_ids: pool.source_media_ids.filter((id) => !sourceIds.has(id)),
    voiceover_media_ids: pool.voiceover_media_ids.filter((id) => !removed.has(id)),
  }
}

export function poolWithoutSources(pool, sourceIds, sources, voices) {
  const removed = new Set(sourceIds)
  const voiceIds = new Set(voices.filter((voice) => {
    const source = activeVoiceSource(voice, sources)
    return source && removed.has(source.id)
  }).map((voice) => voice.id))
  return {
    ...pool,
    source_media_ids: pool.source_media_ids.filter((id) => !removed.has(id)),
    voiceover_media_ids: pool.voiceover_media_ids.filter((id) => !voiceIds.has(id)),
  }
}
