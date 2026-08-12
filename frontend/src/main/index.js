import { app, BrowserWindow, ipcMain, dialog, shell } from 'electron'
import { execFile, spawn } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { createServer } from 'node:net'
import { existsSync, mkdirSync, appendFileSync } from 'node:fs'
import { extname, join, relative, resolve, sep } from 'node:path'

const SOURCE_ROOT = resolve(__dirname, '../../../')
const DEFAULT_BACKEND_PORT = 4817
const BACKEND_HOST = '127.0.0.1'
const BRIDGE_TOKEN = randomBytes(32).toString('hex')
let backendPort = DEFAULT_BACKEND_PORT
let backendProcess = null
let mainWindow = null

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

const PREVIEWABLE_EXTS = new Set([
  '.mp4', '.mov', '.m4v', '.mkv', '.webm', '.avi',
  '.mp3', '.wav', '.m4a', '.aac', '.flac', '.ogg',
  '.jpg', '.jpeg', '.png', '.webp'
])

function ensureManagedPath(targetPath) {
  const root = appRoot()
  const resolved = resolve(String(targetPath || ''))
  const allowedRoots = [
    join(root, 'data', 'downloads'),
    join(root, 'data', 'tts'),
    join(root, 'data', 'seedance'),
    join(root, 'exports'),
    join(root, 'previews'),
    join(root, '.cache')
  ]
  const relation = relative(root, resolved)
  const insideWorkspace = !(relation.startsWith('..') || relation === '' || relation.includes(`..${sep}`))
  if (insideWorkspace && allowedRoots.some((root) => resolved === root || resolved.startsWith(`${root}${sep}`))) {
    return resolved
  }

  // Imported clips live wherever the operator keeps them — Desktop, Photos — so limiting
  // this to the app's own folders made 打开/定位 fail for every imported file. Outside the
  // workspace we still refuse anything that is not media, so no executable can be opened.
  if (PREVIEWABLE_EXTS.has(extname(resolved).toLowerCase())) {
    return resolved
  }
  throw new Error('Only media files can be opened or revealed')
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
  ensureRuntimeDirs()
  if (backendProcess) return
  backendPort = await findBackendPort()
  const root = appRoot()
  const logPath = join(root, 'logs', 'backend.log')
  const python = pythonExecutable()
  const env = {
    ...process.env,
    APP_ROOT: root,
    APP_BACKEND_HOST: BACKEND_HOST,
    APP_BACKEND_PORT: String(backendPort),
    APP_BRIDGE_TOKEN: BRIDGE_TOKEN,
    NUMBA_CACHE_DIR: join(root, '.cache', 'numba'),
    ...(app.isPackaged
      ? {}
      : { PYTHONPATH: join(SOURCE_ROOT, 'backend', 'src') + (process.env.PYTHONPATH ? `:${process.env.PYTHONPATH}` : '') })
  }
  const backendArgs = app.isPackaged ? [] : ['-m', 'automated_video_editing_backend.main']
  backendProcess = spawn(python, backendArgs, {
    cwd: root,
    env,
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true
  })
  const log = (line) => appendFileSync(logPath, line)
  backendProcess.stdout.on('data', (chunk) => log(`[out] ${chunk}`))
  backendProcess.stderr.on('data', (chunk) => log(`[err] ${chunk}`))
  backendProcess.on('exit', (code) => {
    log(`[exit] backend exited with code ${code}\n`)
    backendProcess = null
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
      { name: 'Media', extensions: ['mp4', 'mov', 'mkv', 'webm', 'mp3', 'wav', 'm4a', 'jpg', 'jpeg', 'png'] }
    ]
  })
  return result.canceled ? [] : result.filePaths
})


ipcMain.handle('open-managed-path', async (_event, targetPath) => {
  return openManagedPath(ensureManagedPath(targetPath))
})

ipcMain.handle('reveal-managed-path', (_event, targetPath) => {
  shell.showItemInFolder(ensureManagedPath(targetPath))
  return true
})

ipcMain.handle('trash-managed-path', async (_event, targetPath) => {
  await shell.trashItem(ensureManagedPath(targetPath))
  return true
})

/** Stop the backend for good, not just politely.
 *
 * A plain kill() is SIGTERM, which uvicorn can sit on; the process then outlived the app and
 * kept holding the port, so the next launch quietly started a second backend one port along
 * and the old one stayed forever. */
function stopBackend() {
  const child = backendProcess
  if (!child) return
  backendProcess = null
  try {
    child.kill('SIGTERM')
  } catch {
    return
  }
  const forceTimer = setTimeout(() => {
    try {
      child.kill('SIGKILL')
    } catch {
      // Already gone.
    }
  }, 2000)
  // Do not hold the event loop open waiting to escalate.
  forceTimer.unref?.()
}

// Exactly one copy of the app at a time. Without this, launching again while one was already
// running gave a second window on a second backend, each with its own bridge token.
const hasSingleInstanceLock = app.requestSingleInstanceLock()
if (!hasSingleInstanceLock) {
  app.quit()
} else {
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

  app.on('will-quit', stopBackend)
  app.on('before-quit', stopBackend)
  // before-quit never fires when the process is signalled, which is exactly how a dev run ends.
  for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP']) {
    process.on(signal, () => {
      stopBackend()
      app.quit()
    })
  }
  process.on('exit', stopBackend)
}
