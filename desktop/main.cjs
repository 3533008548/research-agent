const {
  app,
  BrowserWindow,
  ipcMain,
  session,
  dialog,
  safeStorage,
  Tray,
  Menu,
  nativeImage,
  shell,
} = require("electron");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
// Packaged acceptance tests use an isolated profile and never touch user data.
if (process.env.RESEARCH_ASSISTANT_USER_DATA)
  app.setPath(
    "userData",
    path.resolve(process.env.RESEARCH_ASSISTANT_USER_DATA),
  );
let win,
  tray,
  updates,
  backend,
  origin,
  token,
  quitting = false,
  restarting = false;
const metaPath = () => path.join(app.getPath("userData"), "desktop.json");
const credentialsPath = () =>
  path.join(app.getPath("userData"), "credentials.bin");
function metadata() {
  try {
    return JSON.parse(fs.readFileSync(metaPath(), "utf8"));
  } catch {
    return {};
  }
}
function credentials() {
  if (!fs.existsSync(credentialsPath())) return {};
  return JSON.parse(
    safeStorage.decryptString(fs.readFileSync(credentialsPath())),
  );
}
function dataDirectory() {
  return metadata().dataDir || path.join(app.getPath("userData"), "runtime");
}
function validSender(event) {
  const url = event.senderFrame?.url || "";
  if (
    event.sender !== win?.webContents ||
    !(origin && url.startsWith(origin + "/"))
  )
    throw Error("不允许的窗口请求");
}
function showError(message) {
  if (!win || win.isDestroyed()) return;
  win.webContents
    .executeJavaScript(
      `document.getElementById('error')?.replaceChildren(document.createTextNode(${JSON.stringify(message)}));document.getElementById('retry')?.removeAttribute('hidden');`,
    )
    .catch(() => {});
}
async function stopBackend() {
  const child = backend;
  if (!child) return;
  backend = null;
  if (child.exitCode !== null || child.signalCode !== null) return;
  let timer;
  const exited = new Promise((resolve) => child.once("exit", resolve));
  try {
    await fetch(origin + "/api/v1/desktop/shutdown", {
      method: "POST",
      headers: { "X-API-Key": token },
      signal: AbortSignal.timeout(3000),
    });
  } catch {}
  await Promise.race([
    exited,
    new Promise((resolve) => {
      timer = setTimeout(resolve, 25000);
    }),
  ]);
  clearTimeout(timer);
  if (child.exitCode === null && child.signalCode === null) {
    child.kill();
    await Promise.race([
      exited,
      new Promise((resolve) => {
        timer = setTimeout(resolve, 5000);
      }),
    ]);
    clearTimeout(timer);
  }
}
async function startBackend() {
  origin = null;
  await win.loadFile(path.join(__dirname, "startup.html"));
  const dataDir = dataDirectory();
  fs.mkdirSync(dataDir, { recursive: true });
  let secrets;
  try {
    secrets = credentials();
  } catch {
    showError("无法解密模型密钥，请使用保存密钥时的 Windows 账号。");
    return;
  }
  token = crypto.randomBytes(32).toString("hex");
  const root = path.resolve(__dirname, "..");
  const executable = app.isPackaged
    ? path.join(process.resourcesPath, "backend", "research-backend.exe")
    : process.env.DESKTOP_PYTHON || "python";
  const frontend = app.isPackaged
    ? path.join(process.resourcesPath, "frontend")
    : path.join(root, "frontend", "dist");
  const args = [
    ...(app.isPackaged ? [] : [path.join(root, "desktop_backend.py")]),
    "--data-dir",
    dataDir,
    "--frontend",
    frontend,
    "--port",
    String(metadata().backendPort || 0),
  ];
  const env = {
    ...process.env,
    PYTHONUTF8: "1",
    PYTHON_DOTENV_DISABLED: "1",
    APP_DATA_DIR: dataDir,
    API_AUTH_TOKEN: token,
    DEEPSEEK_API_KEY: secrets.deepseekKey || "desktop-unconfigured",
    OPENALEX_API_KEY: secrets.openalexKey || "",
  };
  // A placeholder is only for local reading before setup; no network run is admitted.
  if (!secrets.deepseekKey) delete env.DEEPSEEK_API_KEY;
  delete env.REDIS_URL;
  delete env.ELECTRON_RUN_AS_NODE;
  backend = spawn(executable, args, {
    cwd: dataDir,
    env,
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
  });
  const child = backend;
  let buffer = "",
    port,
    readyLoaded = false;
  const log = fs.createWriteStream(
    path.join(app.getPath("userData"), "backend.log"),
    { flags: "w" },
  );
  child.stderr.on("data", (chunk) => log.write(chunk));
  const ready = new Promise((resolve, reject) => {
    const deadline = setTimeout(
      () => reject(Error("后台启动超时，请查看本地后台日志后重试。")),
      120000,
    );
    child.once("error", (error) => {
      clearTimeout(deadline);
      reject(error);
    });
    child.once("exit", (code) => {
      clearTimeout(deadline);
      log.end();
      if (readyLoaded && backend === child && !quitting && !restarting) {
        backend = null;
        origin = null;
        void win
          .loadFile(path.join(__dirname, "startup.html"))
          .then(() =>
            showError(
              "后台意外退出（代码 " +
                code +
                "）。已保存文件和笔记仍在，可重试启动。",
            ),
          );
      }
      reject(Error("后台服务退出，代码：" + code));
    });
    child.stdout.on("data", (chunk) => {
      buffer += chunk.toString();
      const lines = buffer.split(/\r?\n/);
      buffer = lines.pop();
      for (const line of lines) {
        if (line.startsWith("DESKTOP_READY=")) {
          try {
            port = JSON.parse(line.slice(14)).port;
            fs.writeFileSync(
              metaPath(),
              JSON.stringify({ ...metadata(), backendPort: port }),
            );
            clearTimeout(deadline);
            resolve();
          } catch {}
        } else log.write(line + "\n");
      }
    });
  });
  try {
    await ready;
    origin = "http://127.0.0.1:" + port;
    for (let attempt = 0; attempt < 120; attempt++) {
      try {
        const response = await fetch(origin + "/api/v1/desktop/status", {
          headers: { "X-API-Key": token },
          signal: AbortSignal.timeout(1000),
        });
        if (response.ok) {
          await win.loadURL(origin);
          readyLoaded = true;
          return;
        }
      } catch {}
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    throw Error("后台未能完成初始化");
  } catch (error) {
    await stopBackend();
    origin = null;
    showError("启动失败：" + error.message);
  }
}
async function restart() {
  if (restarting) return;
  restarting = true;
  try {
    await stopBackend();
    await startBackend();
  } finally {
    restarting = false;
  }
}
async function confirmRestart() {
  try {
    const response = await fetch(origin + "/api/v1/desktop/status", {
      headers: { "X-API-Key": token },
      signal: AbortSignal.timeout(3000),
    });
    const status = await response.json();
    if (status.queued > 0) {
      const result = await dialog.showMessageBox(win, {
        type: "question",
        buttons: ["取消", "重启后台"],
        defaultId: 0,
        cancelId: 0,
        message: "当前有 " + status.queued + " 项运行中或排队中的任务。",
        detail:
          "重启会停止正在执行的任务；尚未开始的任务保存在当前数据目录中。",
      });
      return result.response === 1;
    }
  } catch {}
  return true;
}
if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on("second-instance", () => {
    win?.show();
    win?.focus();
  });
  app
    .whenReady()
    .then(async () => {
      fs.mkdirSync(app.getPath("userData"), { recursive: true });
      win = new BrowserWindow({
        width: 1440,
        height: 940,
        minWidth: 800,
        minHeight: 600,
        title: "科研助手",
        show: !app.commandLine.hasSwitch("headless"),
        icon: path.join(__dirname, "icon.png"),
        backgroundColor: "#191d23",
        webPreferences: {
          preload: path.join(__dirname, "preload.cjs"),
          contextIsolation: true,
          nodeIntegration: false,
          sandbox: true,
        },
      });
      win.setMenuBarVisibility(false);
      session.defaultSession.webRequest.onBeforeSendHeaders(
        (details, callback) => {
          const headers = { ...details.requestHeaders };
          if (origin && details.url.startsWith(origin + "/api/"))
            headers["X-API-Key"] = token;
          callback({ requestHeaders: headers });
        },
      );
      session.defaultSession.setPermissionRequestHandler(
        (_wc, _permission, callback) => callback(false),
      );
      win.webContents.setWindowOpenHandler(({ url }) => {
        if (/^https?:\/\//.test(url) && !url.startsWith(origin + "/"))
          shell.openExternal(url);
        return { action: "deny" };
      });
      win.webContents.on("will-navigate", (event, url) => {
        if (!origin || !url.startsWith(origin + "/")) event.preventDefault();
      });
      win.on("close", (event) => {
        if (!quitting) {
          event.preventDefault();
          win.hide();
        }
      });
      const icon = nativeImage.createFromPath(path.join(__dirname, "tray.png"));
      tray = new Tray(icon);
      tray.setToolTip("科研助手 · 后台任务继续运行");
      tray.on("double-click", () => win.show());
      tray.setContextMenu(
        Menu.buildFromTemplate([
          { label: "打开科研助手", click: () => win.show() },
          { label: "退出", click: () => app.quit() },
        ]),
      );
      ipcMain.handle("desktop:info", (event) => {
        validSender(event);
        return {
          dataDir: dataDirectory(),
          configured: !!credentials().deepseekKey,
          version: app.getVersion(),
        };
      });
      ipcMain.on("desktop:renderer-error", (event, message) => {
        try {
          validSender(event);
          if (typeof message !== "string") return;
          fs.appendFile(path.join(app.getPath("userData"), "renderer.log"),
            `${new Date().toISOString()} ${message.slice(0, 4000)}\n`, () => {});
        } catch {}
      });
      const { autoUpdater } = require("electron-updater");
      let updateStoppedBackend = false;
      updates = require("./updater.cjs").createUpdater({
        updater: autoUpdater,
        version: app.getVersion(),
        supported: app.isPackaged && process.platform === "win32",
        notify: (state) => {
          if (win && !win.isDestroyed()) win.webContents.send("desktop:update-status", state);
        },
        prepareInstall: async () => {
          if (restarting || quitting) throw Error("后台正在重启，请稍后安装更新。");
          if (backend) {
            const response = await fetch(origin + "/api/v1/desktop/status", {
              headers: { "X-API-Key": token }, signal: AbortSignal.timeout(3000),
            });
            if (!response.ok) throw Error("无法确认后台任务状态，请稍后重试。");
            const status = await response.json();
            if (status.queued > 0) throw Error(`当前还有 ${status.queued} 项任务，请待完成或取消后安装更新。`);
          }
          restarting = true;
          updateStoppedBackend = true;
          await stopBackend();
        },
        recoverInstall: () => {
          if (updateStoppedBackend && !quitting) {
            updateStoppedBackend = false;
            void startBackend().finally(() => { restarting = false; });
          }
        },
      });
      for (const [name, action] of Object.entries({
        status: () => updates.snapshot(), check: () => updates.check(),
        download: () => updates.download(), install: () => updates.install(),
      })) {
        ipcMain.handle(`desktop:update-${name}`, async (event) => { validSender(event); return action(); });
      }
      ipcMain.handle("desktop:configure", async (event, value) => {
        validSender(event);
        if (!safeStorage.isEncryptionAvailable())
          throw Error("系统凭据加密不可用");
        if (!(await confirmRestart())) return { saved: false };
        const previous = credentials();
        for (const key of ["deepseekKey", "openalexKey"]) {
          if (typeof value?.[key] === "string" && value[key].trim()) {
            if (value[key].length > 1000) throw Error("密钥长度无效");
            previous[key] = value[key].trim();
          }
        }
        fs.writeFileSync(
          credentialsPath(),
          safeStorage.encryptString(JSON.stringify(previous)),
        );
        setTimeout(() => restart(), 150);
        return { saved: true };
      });
      ipcMain.handle("desktop:restart", async (event) => {
        validSender(event);
        if (!(await confirmRestart())) return { restarting: false };
        setTimeout(() => restart(), 150);
        return { restarting: true };
      });
      ipcMain.handle("desktop:data-directory", async (event) => {
        validSender(event);
        const result = await dialog.showOpenDialog(win, {
          title: "选择数据目录（包含 primary 的目录，或空目录）",
          properties: ["openDirectory", "createDirectory"],
        });
        if (result.canceled || !(await confirmRestart()))
          return { changed: false };
        const selected = result.filePaths[0];
        const choice = await dialog.showMessageBox(win, {
          type: "question",
          buttons: ["取消", "使用此目录"],
          defaultId: 0,
          cancelId: 0,
          message: "切换到此数据目录？",
          detail:
            selected +
            "\n\n现有文件和笔记保留在原目录，此操作不会搬移文件；可随时切换回来。",
        });
        if (choice.response !== 1) return { changed: false };
        fs.accessSync(selected, fs.constants.W_OK);
        fs.writeFileSync(
          metaPath(),
          JSON.stringify({ ...metadata(), dataDir: selected }),
        );
        setTimeout(() => restart(), 150);
        return { changed: true };
      });
      ipcMain.handle("desktop:retry", (event) => {
        if (event.sender !== win.webContents) throw Error("invalid window");
        setTimeout(() => restart(), 0);
      });
      win.webContents.on("did-finish-load", () => {
        if (!origin)
          win.webContents
            .executeJavaScript(
              "document.getElementById('retry')?.addEventListener('click',()=>window.desktop.retry())",
            )
            .catch(() => {});
      });
      await startBackend();
      if (app.isPackaged && !process.env.RESEARCH_ASSISTANT_USER_DATA)
        setTimeout(() => { void updates.check(); }, 30000).unref();
    })
    .catch((error) => {
      dialog.showErrorBox("科研助手启动失败", error.message);
      app.quit();
    });
  app.on("before-quit", (event) => {
    if (quitting) return;
    event.preventDefault();
    quitting = true;
    stopBackend().finally(() => {
      tray?.destroy();
      win?.destroy();
      app.quit();
    });
  });
  app.on("window-all-closed", () => {});
}
