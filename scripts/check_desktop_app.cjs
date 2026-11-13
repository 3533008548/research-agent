// Real Electron acceptance with isolated data and a local, deterministic model.
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const root = path.resolve(__dirname, '..');
const driver = process.env.PLAYWRIGHT_NODE_PATH || execFileSync('python', ['-c', 'import pathlib,playwright; print(pathlib.Path(playwright.__file__).parent/"driver"/"package")'], { encoding: 'utf8' }).trim();
const { _electron } = require(driver);
const profile = path.join(root, '.visual_check', 'desktop-profile-' + Date.now());
fs.mkdirSync(path.join(profile, 'runtime', 'primary'), { recursive: true });
fs.writeFileSync(path.join(profile, 'runtime', 'primary', 'settings.json'), JSON.stringify({ rag_enabled: false, daily_search_enabled: false }));
const pdf = execFileSync('python', ['-c', 'import fitz,sys; p=fitz.open();p.new_page().insert_text((60,90),"Each annotation links a passage to its page.",fontsize=16);p.new_page().insert_text((60,90),"Second page: preserve reading progress.");sys.stdout.buffer.write(p.tobytes())']);
let desktop;
const requests = [];
const model = http.createServer((request, response) => {
  let body = '';
  request.on('data', chunk => { body += chunk; });
  request.on('end', () => {
    const payload = JSON.parse(body); requests.push(payload);
    if (payload.stream) {
      response.writeHead(200, { 'Content-Type': 'text/event-stream; charset=utf-8' });
      response.end('data: ' + JSON.stringify({ choices: [{ delta: { content: '桌面端对话已连通。' }, finish_reason: null }] }) + '\n\ndata: ' + JSON.stringify({ choices: [{ delta: {}, finish_reason: 'stop' }], usage: { prompt_tokens: 10, completion_tokens: 10 } }) + '\n\ndata: [DONE]\n\n');
    } else {
      response.writeHead(200, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify({ choices: [{ message: { content: '每条批注都将原文片段与所在页关联。' } }], usage: { prompt_tokens: 10, completion_tokens: 10 } }));
    }
  });
});

(async () => {
  await new Promise(resolve => model.listen(0, '127.0.0.1', resolve));
  const env = { ...process.env, RESEARCH_ASSISTANT_USER_DATA: profile, DEEPSEEK_API_URL: `http://127.0.0.1:${model.address().port}/chat/completions`, PYTHONUTF8: '1' };
  delete env.ELECTRON_RUN_AS_NODE;
  const development = process.argv.includes('--dev');
  if (!development) env.PATH = path.join(process.env.SystemRoot, 'System32'); // No Python or Node on the packaged app's PATH.
  desktop = await _electron.launch({
    executablePath: development ? path.join(root, 'desktop/node_modules/electron/dist/electron.exe') : path.join(root, 'desktop/release/win-unpacked/Research Assistant.exe'),
    args: [...(development ? [path.join(root, 'desktop')] : []), '--headless'], env, timeout: 120000,
  });
  const page = await desktop.firstWindow();
  page.setDefaultTimeout(30000);
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.getByRole('button', { name: '论文库', exact: true }).waitFor({ timeout: 120000 });
  const initialOrigin = new URL(page.url()).origin;
  assert.equal((await fetch(initialOrigin + '/api/v1/library')).status, 401);
  assert.equal(await page.evaluate(() => typeof window.require), 'undefined');
  assert.equal((await page.evaluate(() => window.desktop.info())).configured, false);
  const upload = await page.evaluate(async bytes => {
    const form = new FormData(); form.append('file', new Blob([new Uint8Array(bytes)], { type: 'application/pdf' }), 'desktop-client.pdf');
    const response = await fetch('/api/v1/library/documents', { method: 'POST', body: form });
    if (!response.ok) throw Error(await response.text());
    return response.json();
  }, [...pdf]);
  await page.getByRole('button', { name: '论文库', exact: true }).click();
  await page.getByRole('button', { name: 'desktop-client.pdf', exact: true }).click();
  await page.locator('.textLayer span').first().waitFor();
  assert.ok(await page.locator('.pdf-sheet canvas').evaluate(el => el.width > 100));
  const chatTab = await page.locator('.conversation .draggable-chat-tab').boundingBox();
  await page.mouse.move(chatTab.x + 20, chatTab.y + chatTab.height / 2);
  await page.mouse.down();
  await page.mouse.move(chatTab.x + 40, chatTab.y + chatTab.height / 2, { steps: 5 });
  await page.locator('.chat-drop-right').waitFor();
  const dropZone = await page.locator('.chat-drop-right').boundingBox();
  await page.mouse.move(dropZone.x + dropZone.width / 2, dropZone.y + 80, { steps: 8 });
  await page.mouse.up();
  await page.locator('.right-workspace .composer').waitFor();
  assert.ok(await page.locator('.paper-reader').isVisible());
  await page.getByRole('textbox', { name: '消息', exact: true }).fill('停靠期间的对话草稿');
  await page.getByRole('spinbutton', { name: '缩放比例', exact: true }).fill('150');
  await page.getByRole('button', { name: '随手记', exact: true }).click();
  await page.getByRole('textbox', { name: '笔记正文', exact: true }).fill('安装版笔记：原文和解释分开保存。');
  await page.getByRole('button', { name: '保存笔记', exact: true }).click();
  await page.locator('.note-save-status').filter({ hasText: '已保存' }).waitFor();
  await page.getByRole('button', { name: '下一页', exact: true }).click();
  await page.waitForTimeout(800);
  await page.evaluate(() => localStorage.setItem('desktop-restart-check', 'retained'));
  await page.getByRole('button', { name: '设置', exact: true }).click();
  await page.getByRole('button', { name: '每日检索', exact: true }).click();
  await page.getByRole('button', { name: '用户画像', exact: true }).click();
  const testKey = 'desktop-acceptance-fake-key-not-a-credential';
  await page.evaluate(key => window.desktop.configure({ deepseekKey: key }), testKey);
  await page.waitForURL('**/startup.html');
  await page.getByRole('button', { name: '论文库', exact: true }).waitFor({ timeout: 120000 });
  assert.equal(new URL(page.url()).origin, initialOrigin);
  assert.equal(await page.evaluate(() => localStorage.getItem('desktop-restart-check')), 'retained');
  assert.equal((await page.evaluate(() => window.desktop.info())).configured, true);
  assert.equal(fs.readFileSync(path.join(profile, 'credentials.bin')).includes(Buffer.from(testKey)), false);
  await page.locator('.right-workspace .composer').waitFor();
  assert.equal(await page.evaluate(() => JSON.parse(localStorage.getItem('research-agent.workspace-layout.v1')).chatPlacement), 'right');
  await page.getByRole('button', { name: '论文库', exact: true }).click();
  await page.getByRole('button', { name: 'desktop-client.pdf', exact: true }).click();
  await page.locator('.textLayer span').first().waitFor();
  assert.equal(await page.getByRole('spinbutton', { name: '页码', exact: true }).inputValue(), '2');
  assert.equal(await page.getByRole('spinbutton', { name: '缩放比例', exact: true }).inputValue(), '150');
  await page.getByRole('tab', { name: '辅助 / 运行', exact: true }).click();
  await page.getByText('安装版笔记：原文和解释分开保存。', { exact: true }).waitFor();
  await page.locator('.right-workspace .draggable-chat-tab button').click();
  await page.getByRole('button', { name: '上一页', exact: true }).click();
  await page.locator('.textLayer span').filter({ hasText: 'Each annotation' }).waitFor();
  await page.locator('.textLayer').evaluate(el => {
    const range = document.createRange(); range.selectNodeContents(el.querySelector('span'));
    const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
    el.dispatchEvent(new KeyboardEvent('keyup', { key: 'Shift', bubbles: true }));
  });
  await page.getByRole('button', { name: '翻译选段', exact: true }).click();
  await page.locator('.translation-text').filter({ hasText: '每条批注' }).waitFor();
  assert.equal(requests.at(-1).messages.at(-1).content, 'Each annotation links a passage to its page.');
  const preview = await desktop.evaluate(async ({ BrowserWindow }) => (await BrowserWindow.getAllWindows()[0].webContents.capturePage()).toPNG().toString('base64'));
  if (preview) fs.writeFileSync(path.join(root, '.visual_check/desktop-client.png'), Buffer.from(preview, 'base64'));
  await page.locator('.right-workspace .draggable-chat-tab button').click();
  await page.getByRole('textbox', { name: '消息', exact: true }).fill('你好，测试本地客户端对话。');
  await page.getByRole('button', { name: /发送|submit/i }).click();
  await page.locator('.message-assistant').filter({ hasText: '桌面端对话已连通。' }).waitFor();
  assert.ok(await page.locator('.paper-reader').isVisible());
  const dockedPreview = await desktop.evaluate(async ({ BrowserWindow }) => (await BrowserWindow.getAllWindows()[0].webContents.capturePage()).toPNG().toString('base64'));
  if (dockedPreview) fs.writeFileSync(path.join(root, '.visual_check/desktop-chat-docked.png'), Buffer.from(dockedPreview, 'base64'));
  await page.locator('.right-workspace .draggable-chat-tab button').press('Alt+Shift+ArrowLeft');
  await page.locator('.conversation .message-assistant').filter({ hasText: '桌面端对话已连通。' }).waitFor();
  assert.ok(requests.some(request => request.stream));
  const saved = await page.evaluate(async id => (await fetch(`/api/v1/library/documents/${id}/notes`)).json(), upload.id);
  assert.ok(saved.some(note => note.content.startsWith('安装版笔记')));
  await desktop.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close());
  assert.equal(await desktop.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].isVisible()), false);
  assert.equal(await page.evaluate(async () => (await fetch('/api/v1/desktop/status')).status), 200);
  assert.deepEqual(errors, []);
  await desktop.close(); desktop = null;
  await assert.rejects(fetch(initialOrigin + '/api/v1/desktop/status', { signal: AbortSignal.timeout(2000) }));
  console.log('Desktop passed: isolated runtime, auth, real PDF, notes, encrypted setup, same-origin restart, docked chat persistence, translation, queued chat, tab return, tray close and backend shutdown.');
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  if (desktop) await desktop.close().catch(() => {});
  model.close();
});
