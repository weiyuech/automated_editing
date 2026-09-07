import { contextBridge, ipcRenderer } from 'electron'

contextBridge.exposeInMainWorld('desktopApi', {
  getApiConfig: () => ipcRenderer.invoke('get-api-config'),
  selectMediaFiles: () => ipcRenderer.invoke('select-media-files'),
  chooseMediaImportMode: (fileCount) => ipcRenderer.invoke('choose-media-import-mode', fileCount),
  openPath: (path) => ipcRenderer.invoke('open-managed-path', path),
  revealPath: (path) => ipcRenderer.invoke('reveal-managed-path', path),
  trashPath: (path) => ipcRenderer.invoke('trash-managed-path', path)
})
