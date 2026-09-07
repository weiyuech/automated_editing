const CAPTURE_LIFECYCLE_EVENTS = new Set(['CAPTURE_STARTED', 'CAPTURE_STOPPED'])

function liveNonRecordingCruise(cruiseRun) {
  return cruiseRun?.status === 'running' && cruiseRun?.recording === false
}

/**
 * A no-record cruise still opens a CaptureSession for point metadata. Those internal lifecycle
 * events must not masquerade as a manual recording in the renderer. The tracked session id makes
 * the stop decision stable even if the terminal cruise event and UI request resolve close together.
 */
export function captureLifecycleDecision({
  type,
  data = {},
  cruiseRun = null,
  nonRecordingCruisePending = false,
  nonRecordingCaptureSessionId = ''
}) {
  const trackedId = String(nonRecordingCaptureSessionId || '')
  if (!CAPTURE_LIFECYCLE_EVENTS.has(type)) {
    return { ignore: false, nonRecordingCaptureSessionId: trackedId }
  }

  const sessionId = String(data?.id || '')
  const trackedMatch = Boolean(trackedId && sessionId && trackedId === sessionId)
  const noRecordRun = liveNonRecordingCruise(cruiseRun)
  const ignore = trackedMatch || noRecordRun || Boolean(nonRecordingCruisePending)
  let nextTrackedId = trackedId

  if (type === 'CAPTURE_STARTED' && ignore && sessionId) nextTrackedId = sessionId
  if (
    type === 'CAPTURE_STOPPED'
    && ignore
    && (trackedMatch || (!trackedId && (noRecordRun || nonRecordingCruisePending)))
  ) nextTrackedId = ''

  return { ignore, nonRecordingCaptureSessionId: nextTrackedId }
}

/** Explicit null is authoritative; only a field omitted by the service event may use robot state. */
export function resolveCaptureStoppedMedia(data = {}, robotState = {}) {
  const hasLocalPath = Object.prototype.hasOwnProperty.call(data || {}, 'media_local_path')
  const hasSyncError = Object.prototype.hasOwnProperty.call(data || {}, 'media_sync_error')
  const localPath = hasLocalPath ? data.media_local_path : robotState?.media_local_path
  const syncError = hasSyncError ? data.media_sync_error : robotState?.media_sync_error
  return {
    localPath,
    syncError,
    savePending: Boolean(data?.active) && !localPath
  }
}
