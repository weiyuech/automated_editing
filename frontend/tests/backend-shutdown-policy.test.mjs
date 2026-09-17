import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

import {
  BACKEND_FORCE_KILL_DELAY_MS,
  BACKEND_SHUTDOWN_COMMAND,
  beginBackendShutdown,
  createAppQuitCoordinator,
  finishBackendShutdown,
  runBestEffort,
  settleBackendChild,
  writeBackendShutdownCommand
} from '../src/main/backend-shutdown-policy.js'

const backendRobotPath = fileURLToPath(
  new URL('../../backend/src/automated_video_editing_backend/services/robot.py', import.meta.url)
)
const backendMainPath = fileURLToPath(
  new URL('../../backend/src/automated_video_editing_backend/main.py', import.meta.url)
)
const frontendMainPath = fileURLToPath(
  new URL('../src/main/index.js', import.meta.url)
)

test('Electron force-kill delay covers every backend shutdown window with margin', () => {
  const robotSource = readFileSync(backendRobotPath, 'utf8')
  const mainSource = readFileSync(backendMainPath, 'utf8')
  const recordingMatch = robotSource.match(
    /_RECORDING_SHUTDOWN_TIMEOUT_SECONDS\s*=\s*([0-9.]+)/
  )
  const cleanupMatch = mainSource.match(
    /_APP_SHUTDOWN_CLEANUP_TIMEOUT_SECONDS\s*=\s*([0-9.]+)/
  )
  const uvicornMatch = mainSource.match(
    /_UVICORN_GRACEFUL_REQUEST_TIMEOUT_SECONDS\s*=\s*([0-9.]+)/
  )
  assert.ok(recordingMatch, 'recording shutdown budget must remain discoverable')
  assert.ok(cleanupMatch, 'application cleanup budget must remain discoverable')
  assert.ok(uvicornMatch, 'Uvicorn graceful shutdown budget must remain discoverable')
  const recordingPhaseMs = Number(recordingMatch[1]) * 1000
  const cleanupPhaseMs = Number(cleanupMatch[1]) * 1000
  const uvicornPhaseMs = Number(uvicornMatch[1]) * 1000
  const completeBackendBudgetMs = (
    recordingPhaseMs * 2 + cleanupPhaseMs * 2 + uvicornPhaseMs
  )

  assert.ok(
    BACKEND_FORCE_KILL_DELAY_MS >= completeBackendBudgetMs + 5_000,
    'SIGKILL must leave at least five seconds beyond all backend shutdown phases'
  )
})

test('repeated shutdown hooks write one private command and create one force timer', () => {
  const signals = []
  const scheduled = []
  const timer = {}
  const writes = []
  let ends = 0
  const child = {
    stdin: {
      on() {},
      write: (value) => writes.push(value),
      end: () => { ends += 1 }
    },
    kill: (signal) => signals.push(signal)
  }
  const schedule = (callback, delay) => {
    scheduled.push({ callback, delay })
    return timer
  }

  const forced = []
  const options = { schedule, onForceComplete: (value) => forced.push(value) }
  const first = beginBackendShutdown(child, null, options)
  const repeated = beginBackendShutdown(child, first, options)

  assert.equal(repeated, first)
  assert.deepEqual(writes, [BACKEND_SHUTDOWN_COMMAND])
  assert.equal(ends, 1)
  assert.deepEqual(signals, [])
  assert.equal(scheduled.length, 1)
  assert.equal(scheduled[0].delay, BACKEND_FORCE_KILL_DELAY_MS)

  scheduled[0].callback()
  assert.deepEqual(signals, ['SIGKILL'])
  assert.deepEqual(forced, [child])
})

test('missing stdin still arms the force deadline without sending a graceful kill', () => {
  const signals = []
  const errors = []
  let forceCallback = null
  const child = { kill: (signal) => signals.push(signal) }
  const pending = beginBackendShutdown(child, null, {
    schedule: (callback) => {
      forceCallback = callback
      return {}
    },
    onStdinError: (error) => errors.push(error.message)
  })

  assert.equal(pending.gracefulRequested, false)
  assert.deepEqual(errors, ['Backend stdin is unavailable'])
  assert.deepEqual(signals, [])
  forceCallback()
  assert.deepEqual(signals, ['SIGKILL'])
})

test('stdin EPIPE is consumed and the force fallback remains armed', () => {
  let errorListener = null
  const errors = []
  const input = {
    on(event, listener) {
      if (event === 'error') errorListener = listener
    },
    write(value) {
      assert.equal(value, BACKEND_SHUTDOWN_COMMAND)
    },
    end() {}
  }

  assert.equal(
    writeBackendShutdownCommand(
      { stdin: input },
      (error) => errors.push(error.code || error.message)
    ),
    true
  )
  const brokenPipe = new Error('broken pipe')
  brokenPipe.code = 'EPIPE'
  errorListener(brokenPipe)
  assert.deepEqual(errors, ['EPIPE'])
})

test('backend exit cancels only its own force timer', () => {
  const child = { kill() {} }
  const timer = { unref() {} }
  const pending = { child, forceTimer: timer }
  const canceled = []

  assert.equal(finishBackendShutdown(pending, {}, (value) => canceled.push(value)), pending)
  assert.deepEqual(canceled, [])
  assert.equal(finishBackendShutdown(pending, child, (value) => canceled.push(value)), null)
  assert.deepEqual(canceled, [timer])
})

test('child exit or spawn error releases owned state before best-effort logging', () => {
  const child = {}
  const otherChild = {}
  const timer = {}
  const canceled = []
  const settled = settleBackendChild(
    child,
    child,
    { child, forceTimer: timer },
    (value) => canceled.push(value)
  )

  assert.equal(settled.activeChild, null)
  assert.equal(settled.pending, null)
  assert.equal(settled.owned, true)
  assert.deepEqual(canceled, [timer])
  assert.equal(runBestEffort(() => { throw new Error('disk full') }), false)

  const unrelated = settleBackendChild(child, otherChild, null)
  assert.equal(unrelated.activeChild, otherChild)
  assert.equal(unrelated.owned, false)
})

test('main process consumes spawn errors and rethrows them through startup readiness', () => {
  const source = readFileSync(frontendMainPath, 'utf8')
  assert.match(source, /if \(backendStartupError\) throw backendStartupError/)

  const errorStart = source.indexOf("child.on('error'")
  const errorEnd = source.indexOf("child.on('exit'", errorStart)
  assert.ok(errorStart >= 0 && errorEnd > errorStart, 'spawn error handler must be registered')
  const errorHandler = source.slice(errorStart, errorEnd)
  assert.ok(
    errorHandler.indexOf('settleChild()') < errorHandler.indexOf('runBestEffort('),
    'spawn error ownership must be released before best-effort logging'
  )

  const exitStart = errorEnd
  const exitEnd = source.indexOf('await waitForBackend()', exitStart)
  const exitHandler = source.slice(exitStart, exitEnd)
  assert.ok(
    exitHandler.indexOf('settleChild()') < exitHandler.indexOf('runBestEffort('),
    'exit ownership must be released before best-effort logging'
  )
})

test('Electron launches its managed backend with a private stdin pipe', () => {
  const source = readFileSync(frontendMainPath, 'utf8')

  assert.match(
    source,
    /APP_MANAGED_BY_ELECTRON:\s*['"]1['"]/,
    'the backend must be able to distinguish Electron pipe EOF from standalone stdin EOF'
  )
  assert.match(
    source,
    /stdio:\s*\[\s*['"]pipe['"]\s*,\s*['"]pipe['"]\s*,\s*['"]pipe['"]\s*\]/,
    'the graceful shutdown command requires a piped child stdin'
  )
})

test('normal quit waits for backend completion and releases app.quit exactly once', () => {
  let backendPresent = true
  let prevented = 0
  let stopCalls = 0
  let quitCalls = 0
  const coordinator = createAppQuitCoordinator({
    hasBackend: () => backendPresent,
    stopBackend: () => { stopCalls += 1 },
    quit: () => { quitCalls += 1 }
  })
  const event = { preventDefault: () => { prevented += 1 } }

  assert.equal(coordinator.beforeQuit(event), true)
  assert.equal(coordinator.beforeQuit(event), true)
  assert.equal(prevented, 2)
  assert.equal(stopCalls, 1)
  assert.equal(quitCalls, 0)

  backendPresent = false
  assert.equal(coordinator.backendFinished(), true)
  assert.equal(coordinator.backendFinished(), false)
  assert.equal(quitCalls, 1)
  assert.equal(coordinator.beforeQuit(event), false)
})

test('signal quit enters the same wait path before backend shutdown', () => {
  let stopCalls = 0
  let quitCalls = 0
  const coordinator = createAppQuitCoordinator({
    hasBackend: () => true,
    stopBackend: () => { stopCalls += 1 },
    quit: () => { quitCalls += 1 }
  })
  const event = { prevented: false, preventDefault() { this.prevented = true } }

  assert.equal(coordinator.requestQuit(), true)
  assert.equal(coordinator.requestQuit(), false)
  assert.equal(quitCalls, 1)
  assert.equal(coordinator.beforeQuit(event), true)
  assert.equal(event.prevented, true)
  assert.equal(stopCalls, 1)
  assert.deepEqual(coordinator.state(), {
    quitRequested: true,
    shutdownStarted: true,
    quitReleased: false
  })

  assert.equal(coordinator.backendFinished(), true)
  assert.equal(quitCalls, 2)
})
