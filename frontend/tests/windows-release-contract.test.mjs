import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const externalConfigPath = new URL('../electron-builder.external.json', import.meta.url)
const packagePath = new URL('../package.json', import.meta.url)
const workflowPath = new URL('../../.github/workflows/build-windows.yml', import.meta.url)
const collectorPath = new URL('../../scripts/Collect-AVE-RobotDiagnostics.ps1', import.meta.url)
const mainPath = new URL('../src/main/index.js', import.meta.url)

test('external-media-tools installer ships the robot diagnostics collector', () => {
  const config = JSON.parse(readFileSync(externalConfigPath, 'utf8'))
  assert.ok(config.extraResources.some((entry) => (
    entry.from === '../scripts/Collect-AVE-RobotDiagnostics.ps1'
      && entry.to === 'support/Collect-AVE-RobotDiagnostics.ps1'
  )))
})

test('self-contained installer ships the robot diagnostics collector', () => {
  const config = JSON.parse(readFileSync(packagePath, 'utf8')).build
  assert.ok(config.extraResources.some((entry) => (
    entry.from === '../scripts/Collect-AVE-RobotDiagnostics.ps1'
      && entry.to === 'support/Collect-AVE-RobotDiagnostics.ps1'
  )))
})

test('release branches automatically trigger Windows packaging', () => {
  const workflow = readFileSync(workflowPath, 'utf8')
  assert.match(workflow, /- "release\/\*\*"/)
})

test('Windows packaging validates the collector with Windows PowerShell 5.1', () => {
  const workflow = readFileSync(workflowPath, 'utf8')
  const externalJobStart = workflow.indexOf('  package-external-media-tools:')
  assert.ok(externalJobStart >= 0, 'external-media-tools packaging job must exist')
  const externalJob = workflow.slice(externalJobStart)

  assert.match(externalJob, /Validate diagnostics script with Windows PowerShell 5\.1/)
  assert.match(externalJob, /shell: powershell/)
  assert.match(externalJob, /Language\.Parser\]::ParseFile/)
  assert.match(externalJob, /scripts\/Collect-AVE-RobotDiagnostics\.ps1/)
})

test('collector has a conventional Desktop fallback and reads retained backend logs', () => {
  const script = readFileSync(collectorPath, 'utf8')
  assert.match(
    script,
    /IsNullOrWhiteSpace\(\$desktop\)[\s\S]*Join-Path \$env:USERPROFILE "Desktop"/
  )
  for (const generation of [3, 2, 1]) {
    assert.match(script, new RegExp(`backend\\.log\\.${generation}`))
  }
})

test('Electron creates its bounded log writer before spawning the backend', () => {
  const source = readFileSync(mainPath, 'utf8')
  const writer = source.indexOf('createBoundedBackendLogWriter(logPath)')
  const spawn = source.indexOf('const child = spawn(', writer)
  assert.ok(writer >= 0, 'backend log writer must be created at startup')
  assert.ok(spawn > writer, 'log rotation must happen before the backend can emit output')
})
