import { narrationStyle } from './narration-styles.js'
import { recordingIds } from './composition-groups.js'

export function needsNarrationInstructions(item) {
  if (recordingIds(item).length !== 1) return false
  const hasNotes = nodes => nodes.some(node => {
    const notes = typeof node.notes === 'string' ? [node.notes] : node.notes || []
    return notes.some(note => typeof note === 'string' && note.trim()) || hasNotes(node.children || [])
  })
  return !hasNotes(item.metadata?.composition_tree || [])
}

/** Resolve automatic styles in displayed order, before filtering already bound voices. */
export function narrationRequest(settings, index) {
  return {
    text: settings.source,
    instructions: settings.instructions,
    system_prompt: settings.custom ? settings.system : null,
    ...(settings.llm && settings.applyStyle
      ? { narration_style: narrationStyle(settings.style, index).id } : {}),
  }
}

export function draftIdentity(item, request) {
  return JSON.stringify([item.metadata.composition_id, item.metadata.visual_signature, request])
}

/** A reviewed draft belongs to one exact source, style, prompt and video snapshot. */
export function batchNarrationItem(item, draft, request, options) {
  let text = options.source.trim()
  const adopted = options.llm && draft?.version === 'draft'
  if (options.llm) {
    if (!draft || draft.key !== draftIdentity(item, request) || !draft.version || draft.error) {
      throw new Error('请审阅每条组合对应的改写稿，或选择保留原文。')
    }
    if (adopted) text = draft.text.trim()
  }
  if (!text || text.length > 4000) throw new Error('每条旁白需要 1～4000 个字符。')
  if (!item.metadata.visual_signature) throw new Error('组合画面信息不完整，请刷新媒体库。')
  return {
    composition_id: item.metadata.composition_id,
    visual_signature: item.metadata.visual_signature,
    narration: {
      text,
      // Edited drafts keep their original allocation as a reference; the backend checks
      // changed text against real speech timing instead of trusting these old sections.
      sections: adopted ? draft.sections || [] : [],
      note_assignments: adopted ? draft.note_assignments || [] : [],
      general_notes: adopted ? draft.general_notes || [] : [],
      playback_rate: options.rate,
      auto_tempo: options.autoTempo,
      direct_narration: !adopted,
      ...(adopted && draft.style ? { narration_style: draft.style } : {}),
    },
  }
}
