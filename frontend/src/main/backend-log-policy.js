import { appendFileSync, existsSync, renameSync, rmSync, statSync } from 'node:fs'

// Match the structured diagnostics log: each live log is limited to 5 MiB and three complete
// prior generations remain available for support collection.
export const BACKEND_LOG_MAX_BYTES = 5 * 1024 * 1024
export const BACKEND_LOG_BACKUP_COUNT = 3

function fileSize(path) {
  return existsSync(path) ? statSync(path).size : 0
}

export function rotateBackendLog(logPath, backupCount = BACKEND_LOG_BACKUP_COUNT) {
  if (!Number.isInteger(backupCount) || backupCount < 1) {
    throw new RangeError('backend log backup count must be a positive integer')
  }
  if (!existsSync(logPath)) return false

  for (let generation = backupCount; generation >= 1; generation -= 1) {
    const source = generation === 1 ? logPath : `${logPath}.${generation - 1}`
    if (!existsSync(source)) continue
    const destination = `${logPath}.${generation}`
    // renameSync does not replace an existing destination reliably on Windows.
    rmSync(destination, { force: true })
    renameSync(source, destination)
  }
  return true
}

export function createBoundedBackendLogWriter(
  logPath,
  {
    maxBytes = BACKEND_LOG_MAX_BYTES,
    backupCount = BACKEND_LOG_BACKUP_COUNT
  } = {}
) {
  if (!Number.isSafeInteger(maxBytes) || maxBytes < 1) {
    throw new RangeError('backend log byte limit must be a positive safe integer')
  }

  let currentBytes = 0
  try {
    currentBytes = fileSize(logPath)
    if (currentBytes >= maxBytes && rotateBackendLog(logPath, backupCount)) currentBytes = 0
  } catch {
    // Logging must never prevent the application from starting. A later write retries rotation.
  }

  return (value) => {
    const line = String(value)
    const incomingBytes = Buffer.byteLength(line)
    if (currentBytes > 0 && currentBytes + incomingBytes > maxBytes) {
      try {
        if (rotateBackendLog(logPath, backupCount)) currentBytes = 0
      } catch {
        // Preserve the new diagnostic line even if antivirus or another process briefly holds a
        // Windows log file. The next write will attempt the bounded rotation again.
      }
    }
    appendFileSync(logPath, line)
    currentBytes += incomingBytes
  }
}
