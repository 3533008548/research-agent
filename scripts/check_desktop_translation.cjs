// Packaged reader smoke test: bundled math fonts and LaTeX translation rendering.
// Isolated profile, generated PDF and stub translation; no model API calls.
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const driver = process.env.PLAYWRIGHT_NODE_PATH || execFileSync('python', ['-c',
  'import pathlib,playwright; print(pathlib.Path(playwright.__file__).parent/"driver"/"package")'], { encoding: 'utf8' }).trim();
const { _electron } = require(driver);
const profile = path.join(root, '.visual_check', 'desktop-profile-translation-' + Date.now());
fs.mkdirSync(path.join(profile, 'runtime/primary'), { recursive: true });
fs.writeFileSync(path.join(profile, 'runtime/primary/settings.json'), JSON.stringify({ rag_enabled: false, daily_search_enabled: false }));
const pdf = execFileSync('python', ['-c', 'import pymupdf,sys; d=pymupdf.open(); d.new_page().insert_text((50,80),"Definition 1. Delay duration difference model."); sys.stdout.buffer.write(d.tobytes())']);
const translation = String.raw`定义 1。设 \(S_A\) 到节点 \(v_i\) 的延迟区间为 \([d^A_{v_i},D^A_{v_i}]\)。

\[
\alpha_{v_i}(S_A,S_B)=\frac{1}{1+\left|\frac{D^A_{v_i}+d^A_{v_i}}{2}-\frac{D^B_{v_i}+d^B_{v_i}}{2}\right|}.\tag{1}
\]

延迟区间长度的接近度为：

\[
\beta_{v_i}(S_A,S_B)=\frac{1}{1+|(D^A_{v_i}-d^A_{v_i})-(D^B_{v_i}-d^B_{v_i})|}.\tag{2}
\]

两者的值均介于 0 和 1 之间。`;
let desktop;
(async () => {
  const env = { ...process.env, RESEARCH_ASSISTANT_USER_DATA: profile, PYTHONUTF8: '1', PATH: path.join(process.env.SystemRoot, 'System32') };
  delete env.ELECTRON_RUN_AS_NODE;
  desktop = await _electron.launch({ executablePath: path.join(root, 'desktop/release/win-unpacked/Research Assistant.exe'), args: ['--headless'], env, timeout: 120000 });
  const page = await desktop.firstWindow();
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole('button', { name: '论文库', exact: true }).waitFor({ timeout: 120000 });
  const uploaded = await page.evaluate(async bytes => {
    const data = new FormData();
    data.append('file', new Blob([new Uint8Array(bytes)], { type: 'application/pdf' }), 'formula-translation.pdf');
    return (await fetch('/api/v1/library/documents', { method: 'POST', body: data })).status;
  }, Array.from(pdf));
  assert.equal(uploaded, 201);
  await page.route('**/api/v1/library/documents/*/translate', route => route.fulfill({ json: { text: translation } }));
  await page.getByRole('button', { name: '论文库', exact: true }).click();
  await page.getByRole('button', { name: 'formula-translation.pdf', exact: true }).click();
  await page.locator('.textLayer span').first().waitFor();
  await page.locator('.reader-selection').getByText('划选文字后可翻译或添加批注', { exact: true }).waitFor();
  await page.locator('.textLayer').evaluate(el => {
    const range = document.createRange(); range.selectNodeContents(el);
    const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
    el.dispatchEvent(new KeyboardEvent('keyup', { key: 'Shift', bubbles: true }));
  });
  await page.getByRole('button', { name: '翻译选段', exact: true }).click();
  const result = page.locator('.translation-text');
  await result.locator('.katex-display').first().waitFor();
  await page.evaluate(() => document.fonts.ready);
  assert.equal(await result.locator('.katex').count(), 5);
  assert.equal(await result.locator('.katex-display').count(), 2);
  assert.equal(await result.locator('.tag').count(), 2);
  assert.equal(await result.locator('.math-fallback, .katex-error').count(), 0);
  assert.ok(await page.evaluate(() => document.fonts.check('16px KaTeX_Main') && document.fonts.check('italic 16px KaTeX_Math')));
  assert.ok(await result.evaluate(el => el.scrollWidth <= el.clientWidth + 1));
  await page.getByRole('separator', { name: '调整运行检查器宽度', exact: true }).press('End');
  await page.waitForTimeout(200);
  const preview = await desktop.evaluate(async ({ BrowserWindow }) =>
    (await BrowserWindow.getAllWindows()[0].webContents.capturePage()).toPNG().toString('base64'));
  if (preview) fs.writeFileSync(path.join(root, '.visual_check/desktop-translation-math.png'), Buffer.from(preview, 'base64'));
  await page.getByRole('button', { name: '译文加入笔记', exact: true }).click();
  assert.equal(await page.getByRole('textbox', { name: '笔记正文', exact: true }).inputValue(), translation);
  console.log('Packaged translation passed: local PDF selection, inline/display math, fractions, numbered equations, bundled fonts, contained layout and unchanged note source.');
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  if (desktop) await desktop.close().catch(() => {});
});
