import assert from 'node:assert/strict'
import {
  existsSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync
} from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'

import { createBoundedBackendLogWriter } from '../src/main/backend-log-policy.js'

function logFixture(context) {
  const directory = mkdtempSync(join(tmpdir(), 'ave-backend-log-'))
  context.after(() => rmSync(directory, { recursive: true, force: true }))
  return join(directory, 'backend.log')
}

test('backend log rotates before a write would exceed its byte limit', (context) => {
  const logPath = logFixture(context)
  writeFileSync(logPath, 'old')
  const writeLog = createBoundedBackendLogWriter(logPath, { maxBytes: 8, backupCount: 3 })

  writeLog('12')
  assert.equal(readFileSync(logPath, 'utf8'), 'old12')
  writeLog('3456')

  assert.equal(readFileSync(`${logPath}.1`, 'utf8'), 'old12')
  assert.equal(readFileSync(logPath, 'utf8'), '3456')
})

test('startup rotation retains only the configured complete generations', (context) => {
  const logPath = logFixture(context)
  writeFileSync(logPath, 'current-over-limit')
  writeFileSync(`${logPath}.1`, 'previous-1')
  writeFileSync(`${logPath}.2`, 'previous-2')
  writeFileSync(`${logPath}.3`, 'expired')

  const writeLog = createBoundedBackendLogWriter(logPath, { maxBytes: 5, backupCount: 3 })

  assert.equal(existsSync(logPath), false)
  assert.equal(readFileSync(`${logPath}.1`, 'utf8'), 'current-over-limit')
  assert.equal(readFileSync(`${logPath}.2`, 'utf8'), 'previous-1')
  assert.equal(readFileSync(`${logPath}.3`, 'utf8'), 'previous-2')

  writeLog('fresh')
  assert.equal(readFileSync(logPath, 'utf8'), 'fresh')
})

test('byte accounting uses UTF-8 rather than JavaScript character count', (context) => {
  const logPath = logFixture(context)
  const writeLog = createBoundedBackendLogWriter(logPath, { maxBytes: 7, backupCount: 2 })

  writeLog('中文')
  writeLog('ab')

  assert.equal(readFileSync(`${logPath}.1`, 'utf8'), '中文')
  assert.equal(readFileSync(logPath, 'utf8'), 'ab')
})
