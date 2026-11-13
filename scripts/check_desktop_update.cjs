// Real packaged updater against a local feed. Never executes an installer.
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { load, dump } = require('../desktop/node_modules/js-yaml');
const root = path.resolve(__dirname, '..');
const driver = process.env.PLAYWRIGHT_NODE_PATH || execFileSync('python', ['-c', 'import pathlib,playwright; print(pathlib.Path(playwright.__file__).parent/"driver"/"package")'], { encoding: 'utf8' }).trim();
const { _electron } = require(driver);
const profile = path.join(root, '.visual_check', 'desktop-profile-update-' + Date.now());
fs.mkdirSync(path.join(profile, 'runtime', 'primary'), { recursive: true });
fs.writeFileSync(path.join(profile, 'runtime', 'primary', 'settings.json'), JSON.stringify({ rag_enabled: false, daily_search_enabled: false }));
const published = load(fs.readFileSync(path.join(root, 'desktop/release/latest.yml'), 'utf8'));
const version = published.version.split('.').map(Number); version[2] += 1;
const nextVersion = version.join('.');
const installer = path.join(root, 'desktop/release', published.path);
let invalidHash = true, missingFeed = true, desktop;
const requests = [];
const server = http.createServer((request, response) => {
  const url = new URL(request.url, 'http://localhost');
  requests.push(url.pathname);
  if (missingFeed) { response.writeHead(404); response.end(); return; }
  if (url.pathname === '/latest.yml') {
    const sha512 = invalidHash ? Buffer.alloc(64).toString('base64') : published.sha512;
    response.writeHead(200, { 'Content-Type': 'text/yaml' });
    response.end(dump({ ...published, version: nextVersion, sha512,
      files: published.files.map(file => ({ ...file, sha512 })),
      releaseNotes: '本地验收：只下载并验证更新，截获安装器启动。' }));
  } else if (url.pathname === '/' + published.path) {
    response.writeHead(200, { 'Content-Type': 'application/octet-stream', 'Content-Length': fs.statSync(installer).size });
    const stream = fs.createReadStream(installer);
    response.on('close', () => stream.destroy()); stream.pipe(response);
  } else { response.writeHead(404); response.end(); }
});

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const env = { ...process.env, RESEARCH_ASSISTANT_USER_DATA: profile,
    LOCALAPPDATA: path.join(profile, 'local'), PYTHONUTF8: '1', PATH: path.join(process.env.SystemRoot, 'System32') };
  delete env.ELECTRON_RUN_AS_NODE;
  desktop = await _electron.launch({ executablePath: path.join(root, 'desktop/release/win-unpacked/Research Assistant.exe'), args: ['--headless'], env, timeout: 120000 });
  const page = await desktop.firstWindow(); page.setDefaultTimeout(30000);
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  await page.getByRole('button', { name: '设置', exact: true }).waitFor({ timeout: 120000 });
  const origin = new URL(page.url()).origin;
  // Use the same updater object as the main process. The production feed is unchanged.
  await desktop.evaluate(({ app }, { feed, profile }) => {
    const { createRequire } = process.getBuiltinModule('node:module');
    const localRequire = createRequire(app.getAppPath() + '/package.json');
    const { autoUpdater } = localRequire('electron-updater');
    Object.defineProperty(autoUpdater.app, 'baseCachePath', { value: profile });
    autoUpdater.setFeedURL({ provider: 'generic', url: feed });
    // Exercise the real NSIS quitAndInstall path, but intercept process spawning.
    autoUpdater.spawnLog = async (file, args) => {
      globalThis.updateHandoff = { file, args };
      return '';
    };
    autoUpdater.app.quit = () => { globalThis.updateQuitRequested = true; };
    globalThis.testUpdater = autoUpdater;
  }, { feed: `http://127.0.0.1:${server.address().port}/`, profile });
  await page.getByRole('button', { name: '设置', exact: true }).click();
  const panel = page.getByRole('region', { name: '应用更新', exact: true });
  await panel.getByRole('button', { name: '检查更新', exact: true }).click();
  await panel.getByRole('alert').filter({ hasText: '尚未发布' }).waitFor();
  missingFeed = false;
  await panel.getByRole('button', { name: '检查更新', exact: true }).click();
  await panel.getByText('发现新版本', { exact: true }).waitFor();
  assert.equal(requests.some(url => url.endsWith('.exe')), false);
  await panel.getByRole('button', { name: '下载更新', exact: true }).click();
  await panel.getByRole('alert').filter({ hasText: '校验失败' }).waitFor({ timeout: 120000 });
  assert.equal(await panel.getByRole('button', { name: '重启并安装', exact: true }).count(), 0);
  assert.equal(await page.evaluate(async () => (await fetch('/api/v1/desktop/status')).status), 200);
  invalidHash = false;
  await panel.getByRole('button', { name: '检查更新', exact: true }).click();
  await panel.getByText('发现新版本', { exact: true }).waitFor();
  await panel.getByRole('button', { name: '下载更新', exact: true }).click();
  await panel.getByText('更新已就绪', { exact: true }).waitFor({ timeout: 120000 });
  assert.equal((await page.evaluate(() => window.desktop.updates.check())).status, 'downloaded');
  const cached = await desktop.evaluate(() => globalThis.testUpdater.installerPath);
  assert.ok(path.resolve(cached).startsWith(profile + path.sep));
  assert.equal(fs.statSync(cached).size, fs.statSync(installer).size);
  // A reserved (running) job counts too. Keep the row owned so the worker cannot consume it.
  const queue = path.join(profile, 'runtime', 'primary', 'db', 'desktop-queue.db');
  execFileSync('python', ['-c', 'import sqlite3,sys,time; c=sqlite3.connect(sys.argv[1]);c.execute("INSERT INTO jobs(kind,payload,owner,renewed) VALUES(?,?,?,?)",("chat","{}","update-test",time.time()));c.commit()', queue]);
  assert.ok((await page.evaluate(async () => (await fetch('/api/v1/desktop/status')).json())).queued > 0);
  await panel.getByRole('button', { name: '重启并安装', exact: true }).click();
  await panel.getByRole('alert').filter({ hasText: /任务/ }).waitFor();
  assert.equal(await desktop.evaluate(() => globalThis.updateHandoff), undefined);
  assert.equal(await page.evaluate(async () => (await fetch('/api/v1/desktop/status')).status), 200);
  execFileSync('python', ['-c', 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]);c.execute("DELETE FROM jobs WHERE owner=?",("update-test",));c.commit()', queue]);
  await page.evaluate(() => localStorage.setItem('update-preserves-data', 'saved'));
  const preview = await desktop.evaluate(async ({ BrowserWindow }) => (await BrowserWindow.getAllWindows()[0].webContents.capturePage()).toPNG().toString('base64'));
  if (preview) fs.writeFileSync(path.join(root, '.visual_check/desktop-update.png'), Buffer.from(preview, 'base64'));
  await panel.getByRole('button', { name: '重启并安装', exact: true }).click();
  await page.waitForFunction(() => document.body.textContent.includes('正在重启安装'));
  for (let i = 0; i < 60; i++) {
    if (await desktop.evaluate(() => globalThis.updateQuitRequested)) break;
    await page.waitForTimeout(250);
  }
  const handoff = await desktop.evaluate(() => ({ ...globalThis.updateHandoff, quit: globalThis.updateQuitRequested }));
  assert.equal(handoff.file, cached); assert.ok(handoff.args.includes('/S')); assert.ok(handoff.args.includes('--force-run')); assert.equal(handoff.quit, true);
  await assert.rejects(fetch(origin + '/api/v1/desktop/status', { signal: AbortSignal.timeout(2000) }));
  assert.equal(await page.evaluate(() => localStorage.getItem('update-preserves-data')), 'saved');
  assert.deepEqual(errors, []);
  console.log('Desktop updater passed: real metadata/download, SHA-512 rejection and retry, isolated cache, UI, active-task gate, backend stop, silent NSIS handoff and preserved storage. Installer execution was intercepted.');
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  if (desktop) await desktop.close().catch(() => {});
  server.closeAllConnections(); server.close();
});
