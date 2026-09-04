export function hasBurnedSubtitleSource(clips, audioBed, mediaItems) {
  if ((clips || []).some((clip) => Boolean(clip?.has_burned_subtitles))) return true
  const bedPath = audioBed?.source_path
  if (!bedPath) return false
  return (mediaItems || []).some((item) => (
    item?.path === bedPath && Boolean(item?.metadata?.has_burned_subtitles)
  ))
}

function finiteDuration(clip) {
  const value = Number(clip?.duration)
  return Number.isFinite(value) && value > 0 ? value : 0
}

/** Keep the retained soundtrack attached to a stable point on the edited picture track. */
export function alignAudioBedToTimeline(clips, audioBed, fallbackTime) {
  if (!audioBed) return null
  const timeline = (clips || []).filter((clip) => finiteDuration(clip) > 0)
  if (!timeline.length) return { ...audioBed, timeline_start: 0 }

  const requestedFallback = Number(fallbackTime)
  const previousStart = Number(audioBed.timeline_start)
  const fallback = Number.isFinite(requestedFallback)
    ? requestedFallback
    : (Number.isFinite(previousStart) ? previousStart : 0)
  const anchorUid = audioBed._anchorUid
  let cursor = 0
  for (const clip of timeline) {
    const duration = finiteDuration(clip)
    if (anchorUid != null && clip.uid === anchorUid) {
      const requestedOffset = Number(audioBed._anchorOffset)
      const offset = Number.isFinite(requestedOffset)
        ? Math.max(0, Math.min(duration, requestedOffset))
        : 0
      return { ...audioBed, timeline_start: cursor + offset, _anchorOffset: offset }
    }
    cursor += duration
  }

  const total = cursor
  const target = Math.max(0, Math.min(total, fallback))
  cursor = 0
  for (let index = 0; index < timeline.length; index += 1) {
    const clip = timeline[index]
    const duration = finiteDuration(clip)
    const end = cursor + duration
    if (target < end || index === timeline.length - 1) {
      // If the old anchor was deleted at the very end, attach to the start of the last
      // surviving picture instead of leaving the soundtrack at EOF (which would be silence).
      const offset = target >= total ? 0 : Math.max(0, target - cursor)
      return {
        ...audioBed,
        timeline_start: cursor + offset,
        _anchorUid: clip.uid,
        _anchorOffset: offset
      }
    }
    cursor = end
  }
  return { ...audioBed, timeline_start: 0 }
}

/** Remove editor-only anchor fields before sending the public timeline contract. */
export function renderableAudioBed(audioBed) {
  if (!audioBed) return null
  return {
    source_path: audioBed.source_path,
    source_start: audioBed.source_start,
    timeline_start: audioBed.timeline_start,
    has_voiceover: Boolean(audioBed.has_voiceover)
  }
}
