import { app, BrowserWindow, ipcMain, dialog, shell } from 'electron'
import { execFile, spawn } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { createServer } from 'node:net'
import { existsSync, mkdirSync, copyFileSync, readFileSync, writeFileSync } from 'node:fs'
import { delimiter, dirname, extname, isAbsolute, join, relative, resolve, sep } from 'node:path'
import {
  beginBackendShutdown,
  createAppQuitCoordinator,
  finishBackendShutdown,
  runBestEffort,
  settleBackendChild
} from './backend-shutdown-policy.js'
import { createBoundedBackendLogWriter } from './backend-log-policy.js'
import { ensureDeletableManagedPath, ensureInspectableMediaPath } from './path-policy.js'
import {
  MEDIA_IMPORT_DIALOG_BUTTONS,
  MEDIA_PICKER_EXTENSIONS,
  mediaImportModeForDialogResponse
} from '../shared/media-policy.js'

const SOURCE_ROOT = resolve(__dirname, '../../../')
const DEFAULT_BACKEND_PORT = 4817
const BACKEND_HOST = '127.0.0.1'
const BRIDGE_TOKEN = randomBytes(32).toString('hex')
let backendPort = DEFAULT_BACKEND_PORT
let backendProcess = null
let backendShutdown = null
let backendStartupError = null
let backendLogWriter = null
let mainWindow = null
let appQuitCoordinator = null

function appRoot() {
  // Installed applications cannot write beside app.asar under Program Files. Keep every local
  // setting, cache and export in the user's own application-data folder instead.
  return app.isPackaged ? join(app.getPath('userData'), 'runtime') : SOURCE_ROOT
}

function ensureRuntimeDirs() {
  const root = appRoot()
  for (const name of ['data', '.cache', 'logs', 'exports', 'previews']) {
    mkdirSync(join(root, name), { recursive: true })
  }
}

function isExternalMediaToolsBuild() {
  if (process.env.AVE_EXTERNAL_MEDIA_TOOLS === '1') return true
  return app.isPackaged && existsSync(join(process.resourcesPath, 'external-media-tools.json'))
}

function externalToolConfigPath() {
  return join(appRoot(), 'data', 'external-media-tools.local.json')
}

function readExternalToolDirectory() {
  try {
    const parsed = JSON.parse(readFileSync(externalToolConfigPath(), 'utf8'))
    return typeof parsed?.directory === 'string' ? parsed.directory : ''
  } catch {
    return ''
  }
}

function executableDirectoryCandidates(selected = '') {
  const directories = [
    selected,
    selected ? join(selected, 'bin') : '',
    process.env.FFMPEG_BIN ? dirname(process.env.FFMPEG_BIN) : '',
    process.env.FFPROBE_BIN ? dirname(process.env.FFPROBE_BIN) : '',
    readExternalToolDirectory(),
    ...String(process.env.PATH || '').split(delimiter)
  ]
  return [...new Set(directories.filter(Boolean).map((entry) => resolve(entry)))]
}

function runTool(path, args) {
  return new Promise((resolveRun) => {
    execFile(path, args, { windowsHide: true, timeout: 15000 }, (error, stdout, stderr) => {
      resolveRun({ error, output: `${stdout || ''}\n${stderr || ''}` })
    })
  })
}

async function inspectExternalTools(selected = '') {
  let firstIncompatible = null
  for (const directory of executableDirectoryCandidates(selected)) {
    const ffmpeg = join(directory, process.platform === 'win32' ? 'ffmpeg.exe' : 'ffmpeg')
    const ffprobe = join(directory, process.platform === 'win32' ? 'ffprobe.exe' : 'ffprobe')
    if (!existsSync(ffmpeg) || !existsSync(ffprobe)) continue

    const [version, filters, encoders, probe] = await Promise.all([
      runTool(ffmpeg, ['-hide_banner', '-version']),
      runTool(ffmpeg, ['-hide_banner', '-filters']),
      runTool(ffmpeg, ['-hide_banner', '-encoders']),
      runTool(ffprobe, ['-hide_banner', '-version'])
    ])
    if (version.error || filters.error || encoders.error || probe.error) continue
    const hasSubtitleFilter = /\b(?:ass|subtitles)\b/i.test(filters.output)
    const hasX264 = /\blibx264\b/i.test(encoders.output)
    if (!hasSubtitleFilter || !hasX264) {
      firstIncompatible ||= {
        ok: false,
        directory,
        reason: [
          !hasSubtitleFilter ? '缺少字幕所需的 libass（ass/subtitles 滤镜）' : '',
          !hasX264 ? '缺少导出所需的 libx264 编码器' : ''
        ].filter(Boolean).join('；')
      }
      continue
    }
    return { ok: true, directory, ffmpeg, ffprobe }
  }
  return firstIncompatible || {
    ok: false,
    reason: '没有找到同一文件夹内的 ffmpeg.exe 和 ffprobe.exe'
  }
}

async function resolveExternalMediaTools() {
  if (!isExternalMediaToolsBuild()) return {}

  let last = await inspectExternalTools()
  if (last.ok) return { FFMPEG_BIN: last.ffmpeg, FFPROBE_BIN: last.ffprobe }

  while (true) {
    const choice = await dialog.showMessageBox({
      type: 'warning',
      title: '首次启动准备',
      message: '此安装包不内置 FFmpeg 媒体工具。',
      detail: `请先自行准备 64 位 FFmpeg 与 FFprobe，再选择它们所在的文件夹。\n\n需要：libass 字幕滤镜、libx264 编码器。\n当前检测：${last.reason}\n\n软件不会替您下载或接受第三方许可。`,
      buttons: ['选择工具文件夹', '重新检测', '打开 FFmpeg 官网', '退出应用'],
      defaultId: 0,
      cancelId: 3,
      noLink: true
    })
    if (choice.response === 3) throw new Error('尚未准备 FFmpeg/FFprobe，已取消启动。')
    if (choice.response === 2) {
      await shell.openExternal('https://ffmpeg.org/download.html')
      continue
    }
    if (choice.response === 1) {
      last = await inspectExternalTools()
      if (last.ok) return { FFMPEG_BIN: last.ffmpeg, FFPROBE_BIN: last.ffprobe }
      continue
    }

    const selected = await dialog.showOpenDialog({
      title: '选择包含 ffmpeg.exe 与 ffprobe.exe 的文件夹',
      properties: ['openDirectory']
    })
    if (selected.canceled || !selected.filePaths[0]) continue
    last = await inspectExternalTools(selected.filePaths[0])
    if (!last.ok) continue
    mkdirSync(join(appRoot(), 'data'), { recursive: true })
    writeFileSync(
      externalToolConfigPath(),
      `${JSON.stringify({ directory: last.directory }, null, 2)}\n`,
      { encoding: 'utf8', mode: 0o600 }
    )
    return { FFMPEG_BIN: last.ffmpeg, FFPROBE_BIN: last.ffprobe }
  }
}

function migrateLegacySettings() {
  if (!app.isPackaged) return

  const target = join(appRoot(), 'data', 'settings.local.json')
  if (existsSync(target)) return

  // Electron has used both the package name and the Chinese product name as userData folder
  // names across earlier builds. Credentials must survive a normal same-machine upgrade, even
  // if that naming convention changed. Copy only a recognisable settings object, never log its
  // contents, and never overwrite settings already created by the current installation.
  const appData = app.getPath('appData')
  const roots = [
    app.getPath('userData'),
    join(appData, 'automated-video-editing-frontend'),
    join(appData, '自动视频剪辑')
  ]
  const candidates = [...new Set(roots.flatMap((root) => [
    join(root, 'runtime', 'data', 'settings.local.json'),
    join(root, 'data', 'settings.local.json')
  ]))]

  for (const candidate of candidates) {
    if (candidate === target || !existsSync(candidate)) continue
    try {
      const parsed = JSON.parse(readFileSync(candidate, 'utf8'))
      const recognised = parsed && typeof parsed === 'object' &&
        ['llm', 'tts', 'seedance', 'robot', 'automation'].some((key) => key in parsed)
      if (!recognised) continue
      mkdirSync(join(appRoot(), 'data'), { recursive: true })
      copyFileSync(candidate, target)
      return
    } catch {
      // An invalid legacy file is ignored; SettingsService will create a clean current file.
    }
  }
}

function ensureManagedPath(targetPath) {
  return ensureInspectableMediaPath(appRoot(), targetPath)
}

const VIDEO_FILE_EXTS = new Set(['.mp4', '.mov', '.m4v', '.mkv', '.webm', '.avi'])
const AUDIO_FILE_EXTS = new Set(['.mp3', '.wav', '.m4a', '.aac', '.flac', '.ogg'])

function isInsideDirectory(directory, targetPath) {
  const relation = relative(resolve(directory), resolve(targetPath))
  return relation === '' || (!(relation === '..' || relation.startsWith(`..${sep}`)) && !isAbsolute(relation))
}

/** Hidden sidecars are part of the media item, even though the library deliberately does not
 * show them as separate assets. Move them to the trash with their owner so an old subtitle or
 * narration manifest can never attach itself to a future file that reuses the same stem. */
function managedCompanionPaths(targetPath) {
  const extension = extname(targetPath).toLowerCase()
  const stem = extension ? targetPath.slice(0, -extension.length) : targetPath
  const root = appRoot()
  if (VIDEO_FILE_EXTS.has(extension) && isInsideDirectory(join(root, 'exports'), targetPath)) {
    return [`${stem}.ass`, `${stem}.subtitles.json`]
  }
  if (VIDEO_FILE_EXTS.has(extension) && (
    isInsideDirectory(join(root, 'data', 'downloads'), targetPath)
    || isInsideDirectory(join(root, 'data', 'capture_segments'), targetPath)
  )) {
    // Capture notes use the complete media filename, including its extension.
    return [`${targetPath}.capture.json`, `${targetPath}.gimbal.json`]
  }
  if (AUDIO_FILE_EXTS.has(extension) && isInsideDirectory(join(root, 'data', 'tts'), targetPath)) {
    return [`${stem}.json`]
  }
  return []
}

function pythonExecutable() {
  if (app.isPackaged) {
    return join(process.resourcesPath, 'backend', 'automated-video-editing-backend.exe')
  }
  if (process.env.PYTHON_BIN) return process.env.PYTHON_BIN
  const venvPython = process.platform === 'win32'
    ? join(SOURCE_ROOT, '.venv', 'Scripts', 'python.exe')
    : join(SOURCE_ROOT, '.venv', 'bin', 'python')
  return existsSync(venvPython) ? venvPython : 'python3'
}

function canListen(port) {
  return new Promise((resolveCanListen) => {
    const server = createServer()
    server.once('error', () => resolveCanListen(false))
    server.once('listening', () => server.close(() => resolveCanListen(true)))
    server.listen(port, BACKEND_HOST)
  })
}

async function findBackendPort() {
  for (let port = DEFAULT_BACKEND_PORT; port < DEFAULT_BACKEND_PORT + 50; port += 1) {
    if (await canListen(port)) return port
  }
  throw new Error('No free local backend port found')
}

async function waitForBackend(timeoutMs = 60000) {
  const deadline = Date.now() + timeoutMs
  let lastError = null
  while (Date.now() < deadline) {
    if (backendStartupError) throw backendStartupError
    if (!backendProcess) throw new Error('The bundled backend exited during startup')
    try {
      const response = await fetch(`http://${BACKEND_HOST}:${backendPort}/api/health`, {
        headers: { 'x-bridge-token': BRIDGE_TOKEN },
        signal: AbortSignal.timeout(1500)
      })
      if (response.ok) return
      lastError = new Error(`Backend health check returned ${response.status}`)
    } catch (error) {
      lastError = error
    }
    await new Promise((resolveWait) => setTimeout(resolveWait, 250))
  }
  throw new Error(`Backend did not become ready: ${lastError?.message || 'timed out'}`)
}

async function startBackend() {
  migrateLegacySettings()
  ensureRuntimeDirs()
  if (backendProcess) return
  backendStartupError = null
  const externalMediaTools = await resolveExternalMediaTools()
  backendPort = await findBackendPort()
  const root = appRoot()
  const logPath = join(root, 'logs', 'backend.log')
  const log = createBoundedBackendLogWriter(logPath)
  backendLogWriter = log
  const python = pythonExecutable()
  const env = {
    ...process.env,
    APP_ROOT: root,
    APP_BACKEND_HOST: BACKEND_HOST,
    APP_BACKEND_PORT: String(backendPort),
    APP_BRIDGE_TOKEN: BRIDGE_TOKEN,
    APP_MANAGED_BY_ELECTRON: '1',
    NUMBA_CACHE_DIR: join(root, '.cache', 'numba'),
    ...externalMediaTools,
    ...(app.isPackaged
      ? {}
      : { PYTHONPATH: join(SOURCE_ROOT, 'backend', 'src') + (process.env.PYTHONPATH ? `:${process.env.PYTHONPATH}` : '') })
  }
  const backendArgs = app.isPackaged ? [] : ['-m', 'automated_video_editing_backend.main']
  const child = spawn(python, backendArgs, {
    cwd: root,
    env,
    stdio: ['pipe', 'pipe', 'pipe'],
    windowsHide: true
  })
  backendProcess = child
  child.stdout.on('data', (chunk) => runBestEffort(() => log(`[out] ${chunk}`)))
  child.stderr.on('data', (chunk) => runBestEffort(() => log(`[err] ${chunk}`)))
  const settleChild = () => {
    const settled = settleBackendChild(child, backendProcess, backendShutdown)
    backendProcess = settled.activeChild
    backendShutdown = settled.pending
    if (settled.owned) appQuitCoordinator?.backendFinished()
  }
  child.on('error', (error) => {
    // A missing/quarantined executable emits ChildProcess 'error' rather than throwing from
    // spawn(). Release ownership first so the existing readiness failure can quit cleanly.
    settleChild()
    backendStartupError = error instanceof Error ? error : new Error(String(error))
    runBestEffort(() => log(`[error] ${backendStartupError.message}\n`))
  })
  child.on('exit', (code) => {
    settleChild()
    runBestEffort(() => log(`[exit] backend exited with code ${code}\n`))
  })
  // A frozen Python process imports the media-analysis stack before Uvicorn can listen. Do not
  // show a renderer that immediately fires API requests into a port that is not ready yet.
  await waitForBackend()
}

function preloadPath() {
  const cjsPreload = join(__dirname, '../preload/index.cjs')
  if (existsSync(cjsPreload)) return cjsPreload
  const esmPreload = join(__dirname, '../preload/index.mjs')
  return existsSync(esmPreload) ? esmPreload : join(__dirname, '../preload/index.js')
}

function openQuickTime(managedPath) {
  return new Promise((resolveOpen, rejectOpen) => {
    execFile('/usr/bin/open', ['-b', 'com.apple.QuickTimePlayerX', managedPath], (error, _stdout, stderr) => {
      if (error) {
        rejectOpen(new Error(stderr || error.message || 'QuickTime Player could not open this file'))
        return
      }
      resolveOpen(true)
    })
  })
}

async function openManagedPath(managedPath) {
  const videoExts = new Set(['.mp4', '.m4v', '.mov'])
  if (process.platform === 'darwin' && videoExts.has(extname(managedPath).toLowerCase())) {
    return openQuickTime(managedPath)
  }
  const error = await shell.openPath(managedPath)
  if (error) throw new Error(error)
  return true
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1120,
    minHeight: 720,
    backgroundColor: '#0b0f14',
    icon: app.isPackaged
      ? join(process.resourcesPath, 'icon.png')
      : join(SOURCE_ROOT, 'frontend', 'build', 'icon.png'),
    webPreferences: {
      preload: preloadPath(),
      contextIsolation: true,
      nodeIntegration: false
    }
  })

  if (process.env.ELECTRON_RENDERER_URL) {
    mainWindow.loadURL(process.env.ELECTRON_RENDERER_URL)
  } else {
    mainWindow.loadFile(join(__dirname, '../renderer/index.html'))
  }
}

ipcMain.handle('get-api-config', () => ({
  baseUrl: `http://${BACKEND_HOST}:${backendPort}/api`,
  wsUrl: `ws://${BACKEND_HOST}:${backendPort}/ws`,
  token: BRIDGE_TOKEN
}))

ipcMain.handle('select-media-files', async (event) => {
  const win = BrowserWindow.fromWebContents(event.sender)
  const result = await dialog.showOpenDialog(win, {
    properties: ['openFile', 'multiSelections'],
    filters: [
      { name: 'Media', extensions: [...MEDIA_PICKER_EXTENSIONS] }
    ]
  })
  return result.canceled ? [] : result.filePaths
})

ipcMain.handle('choose-media-import-mode', async (event, fileCount = 1) => {
  const win = BrowserWindow.fromWebContents(event.sender)
  const result = await dialog.showMessageBox(win, {
    type: 'warning',
    title: '导入本地媒体',
    message: `是否把选中的 ${Number(fileCount) || 1} 个文件复制到应用媒体库？`,
    detail: '复制后，即使原文件被移动、改名或外接硬盘断开，应用内副本仍可使用。仅引用不会占用额外空间，但原文件变化后素材会失效。',
    buttons: [...MEDIA_IMPORT_DIALOG_BUTTONS],
    defaultId: 0,
    cancelId: 2,
    noLink: true
  })
  return mediaImportModeForDialogResponse(result.response)
})


ipcMain.handle('open-managed-path', async (_event, targetPath) => {
  return openManagedPath(ensureManagedPath(targetPath))
})

ipcMain.handle('reveal-managed-path', (_event, targetPath) => {
  shell.showItemInFolder(ensureManagedPath(targetPath))
  return true
})

ipcMain.handle('trash-managed-path', async (_event, targetPath) => {
  const managedTarget = ensureDeletableManagedPath(appRoot(), targetPath)
  const companions = managedCompanionPaths(managedTarget).filter((path) => existsSync(path))
  await shell.trashItem(managedTarget)
  const companionFailures = []
  for (const companion of companions) {
    try {
      await shell.trashItem(ensureDeletableManagedPath(appRoot(), companion, true))
    } catch (error) {
      companionFailures.push({ path: companion, message: error?.message || String(error) })
    }
  }
  return { trashed: true, companionFailures }
})

/** Ask the backend to stop safely, then enforce the bounded deadline.
 *
 * Windows treats child.kill('SIGTERM') as immediate termination, so the graceful path is a
 * private one-line stdin command. SIGKILL is reserved for the force timer after the complete
 * camera-safe backend budget. */
function stopBackend() {
  const child = backendProcess || backendShutdown?.child
  if (!child) return
  backendShutdown = beginBackendShutdown(child, backendShutdown, {
    onStdinError: (error) => {
      runBestEffort(() => {
        backendLogWriter ||= createBoundedBackendLogWriter(
          join(appRoot(), 'logs', 'backend.log')
        )
        backendLogWriter(`[shutdown-stdin-error] ${error?.message || String(error)}\n`)
      })
    },
    onForceComplete: (forcedChild) => {
      if (backendProcess === forcedChild) backendProcess = null
      backendShutdown = finishBackendShutdown(backendShutdown, forcedChild)
      appQuitCoordinator?.backendFinished()
    }
  })
  // Normal quit hooks and process signals converge here. Removing the active handle only after
  // the single shutdown request exists keeps repeated calls idempotent.
  if (backendShutdown?.child === child) backendProcess = null
}

// Exactly one copy of the app at a time. Without this, launching again while one was already
// running gave a second window on a second backend, each with its own bridge token.
const hasSingleInstanceLock = app.requestSingleInstanceLock()
if (!hasSingleInstanceLock) {
  app.quit()
} else {
  appQuitCoordinator = createAppQuitCoordinator({
    hasBackend: () => Boolean(backendProcess || backendShutdown),
    stopBackend,
    quit: () => app.quit()
  })

  app.on('second-instance', () => {
    if (!mainWindow) return
    if (mainWindow.isMinimized()) mainWindow.restore()
    mainWindow.focus()
  })

  app.whenReady().then(async () => {
    try {
      await startBackend()
      createWindow()
    } catch (error) {
      dialog.showErrorBox('自动视频剪辑无法启动', String(error?.message || error))
      app.quit()
    }
  })

  // Including macOS, where the default is to keep the app alive with no windows. Leaving that
  // default meant a closed window looked shut down while its backend kept running.
  app.on('window-all-closed', () => {
    app.quit()
  })

  app.on('before-quit', (event) => appQuitCoordinator.beforeQuit(event))
  // Translate process signals into the same coordinated app.quit path used by the window UI.
  for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP']) {
    process.on(signal, () => {
      appQuitCoordinator.requestQuit()
    })
  }
  process.on('exit', stopBackend)
}
