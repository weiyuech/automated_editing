// The backend can spend two 5-second windows on service cleanup, then one 18-second window
// waiting for the capture owner, another confirming the final camera Stop, and 3 seconds on
// Uvicorn's graceful drain. Keep a clear margin before escalating the private stdin request to
// SIGKILL.
export const BACKEND_FORCE_KILL_DELAY_MS = 60_000
export const BACKEND_SHUTDOWN_COMMAND = 'shutdown\n'

export function writeBackendShutdownCommand(child, onError = () => {}) {
  const input = child?.stdin
  if (!input || typeof input.write !== 'function' || typeof input.end !== 'function') {
    onError(new Error('Backend stdin is unavailable'))
    return false
  }

  // EPIPE is normally emitted asynchronously by the stream. Consume it so a backend that died
  // between the quit event and this write cannot crash Electron's main process.
  input.on?.('error', onError)
  try {
    input.write(BACKEND_SHUTDOWN_COMMAND)
    input.end()
    return true
  } catch (error) {
    onError(error)
    return false
  }
}

export function beginBackendShutdown(
  child,
  pending,
  {
    schedule = setTimeout,
    delayMs = BACKEND_FORCE_KILL_DELAY_MS,
    onForceComplete = () => {},
    onStdinError = () => {}
  } = {}
) {
  if (!child || pending?.child === child) return pending

  const gracefulRequested = writeBackendShutdownCommand(child, onStdinError)

  const forceTimer = schedule(() => {
    try {
      child.kill('SIGKILL')
    } catch {
      // The graceful shutdown already completed.
    } finally {
      onForceComplete(child)
    }
  }, delayMs)
  return { child, forceTimer, gracefulRequested }
}

export function finishBackendShutdown(pending, child, cancel = clearTimeout) {
  if (!pending || pending.child !== child) return pending
  cancel(pending.forceTimer)
  return null
}

export function settleBackendChild(child, activeChild, pending, cancel = clearTimeout) {
  const owned = activeChild === child || pending?.child === child
  return {
    activeChild: activeChild === child ? null : activeChild,
    pending: finishBackendShutdown(pending, child, cancel),
    owned
  }
}

export function runBestEffort(action) {
  try {
    action()
    return true
  } catch {
    return false
  }
}

export function createAppQuitCoordinator({ hasBackend, stopBackend, quit }) {
  let quitRequested = false
  let shutdownStarted = false
  let quitReleased = false

  return {
    beforeQuit(event) {
      if (quitReleased || !hasBackend()) return false
      event.preventDefault()
      quitRequested = true
      if (!shutdownStarted) {
        shutdownStarted = true
        stopBackend()
      }
      return true
    },

    requestQuit() {
      if (quitRequested || quitReleased) return false
      quitRequested = true
      quit()
      return true
    },

    backendFinished() {
      if (!quitRequested || quitReleased) return false
      quitReleased = true
      quit()
      return true
    },

    state() {
      return { quitRequested, shutdownStarted, quitReleased }
    }
  }
}
