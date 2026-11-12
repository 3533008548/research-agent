const { contextBridge, ipcRenderer } = require("electron");
contextBridge.exposeInMainWorld("desktop", {
  info: () => ipcRenderer.invoke("desktop:info"),
  configure: (value) => ipcRenderer.invoke("desktop:configure", value),
  chooseDataDirectory: () => ipcRenderer.invoke("desktop:data-directory"),
  restart: () => ipcRenderer.invoke("desktop:restart"),
  retry: () => ipcRenderer.invoke("desktop:retry"),
});
