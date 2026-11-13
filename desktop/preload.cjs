const { contextBridge, ipcRenderer } = require("electron");
contextBridge.exposeInMainWorld("desktop", {
  reportError: (message) => ipcRenderer.send("desktop:renderer-error", String(message).slice(0, 4000)),
  info: () => ipcRenderer.invoke("desktop:info"),
  configure: (value) => ipcRenderer.invoke("desktop:configure", value),
  chooseDataDirectory: () => ipcRenderer.invoke("desktop:data-directory"),
  restart: () => ipcRenderer.invoke("desktop:restart"),
  retry: () => ipcRenderer.invoke("desktop:retry"),
  updates: {
    status: () => ipcRenderer.invoke("desktop:update-status"),
    check: () => ipcRenderer.invoke("desktop:update-check"),
    download: () => ipcRenderer.invoke("desktop:update-download"),
    install: () => ipcRenderer.invoke("desktop:update-install"),
    onStatus: (callback) => {
      const listener = (_event, state) => callback(state);
      ipcRenderer.on("desktop:update-status", listener);
      return () => ipcRenderer.removeListener("desktop:update-status", listener);
    },
  },
});
