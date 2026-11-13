// Real PDF extraction -> bounded preview -> app restart -> catalog/search/read.
// Uses an isolated profile and deterministic local model; no user data changes.
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const root = path.resolve(__dirname, '..');
const driver = process.env.PLAYWRIGHT_NODE_PATH || execFileSync('python', ['-c',
  'import pathlib,playwright; print(pathlib.Path(playwright.__file__).parent/"driver"/"package")'], { encoding: 'utf8' }).trim();
const { _electron } = require(driver);
const profile = path.join(root, '.visual_check', 'desktop-profile-results-' + Date.now());
fs.mkdirSync(path.join(profile, 'runtime/primary'), { recursive: true });
fs.writeFileSync(path.join(profile, 'runtime/primary/settings.json'), JSON.stringify({ rag_enabled: false, daily_search_enabled: false }));
const source = path.join(profile, 'long-source.pdf');
execFileSync('python', ['-X', 'utf8', '-c', `
import pymupdf,sys
with pymupdf.open() as pdf:
 for i in range(12):
  p=pdf.new_page(width=600,height=800)
  p.insert_text((45,40),'Durable Tool Result Fixture',fontsize=12)
  lines=['Source page '+str(i+1)+' line '+str(j)+': complete original evidence remains recoverable.' for j in range(48)]
  if i==11:lines[-1]='TAIL_WITNESS_SOURCE_END is the final evidence line.'
  p.insert_text((45,70),'\\n'.join(lines),fontsize=9)
 pdf.save(sys.argv[1])
`, source]);
const outputs = [];
const dispatched = new Set();
const marker = 'RESULT_STORE_CHECK\n';
let desktop, page;
const model = http.createServer((req, res) => {
  let body = '';
  req.on('data', chunk => { body += chunk; });
  req.on('end', () => {
    try {
      const payload = JSON.parse(body);
      const messages = payload.messages || [];
      const index = messages.findLastIndex(m => m.role === 'user' && typeof m.content === 'string' && m.content.startsWith(marker));
      outputs.push(...messages.slice(index + 1).filter(m => m.role === 'tool').map(m => String(m.content)));
      let message = { content: '工具结果验证完成。' };
      const key = index < 0 ? '' : messages[index].content;
      if (index >= 0 && payload.tools?.length && !dispatched.has(key)) {
        dispatched.add(key);
        const action = JSON.parse(key.slice(marker.length));
        message = { reasoning_content: 'Read the saved evidence.', tool_calls: [{ index: 0, id: 'result-' + dispatched.size,
          type: 'function', function: { name: action.name, arguments: JSON.stringify(action.args) } }] };
      }
      if (payload.stream) {
        res.writeHead(200, { 'Content-Type': 'text/event-stream; charset=utf-8' });
        res.end('data: ' + JSON.stringify({ choices: [{ delta: message, finish_reason: null }] })
          + '\n\ndata: ' + JSON.stringify({ choices: [{ delta: {}, finish_reason: message.tool_calls ? 'tool_calls' : 'stop' }] }) + '\n\ndata: [DONE]\n\n');
      } else {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ choices: [{ message: { content: 'OK' } }] }));
      }
    } catch (error) { res.writeHead(500); res.end(String(error)); }
  });
});
const meta = value => JSON.parse(value.split('\n')[0].slice('[tool_result] '.length));

(async () => {
  await new Promise(resolve => model.listen(0, '127.0.0.1', resolve));
  const development = process.argv.includes('--dev');
  const env = { ...process.env, RESEARCH_ASSISTANT_USER_DATA: profile, PYTHONUTF8: '1',
    DEEPSEEK_API_URL: `http://127.0.0.1:${model.address().port}/chat/completions` };
  delete env.ELECTRON_RUN_AS_NODE;
  if (!development) env.PATH = path.join(process.env.SystemRoot, 'System32');
  async function launch() {
    desktop = await _electron.launch({ executablePath: development ? path.join(root, 'desktop/node_modules/electron/dist/electron.exe')
      : path.join(root, 'desktop/release/win-unpacked/Research Assistant.exe'),
      args: [...(development ? [path.join(root, 'desktop')] : []), '--headless'], env, timeout: 120000 });
    page = await desktop.firstWindow();
    await page.getByRole('button', { name: '论文库', exact: true }).waitFor({ timeout: 120000 });
  }
  await launch();
  await page.evaluate(() => window.desktop.configure({ deepseekKey: 'local-result-test-not-a-real-key' }));
  await page.waitForURL('**/startup.html');
  await page.getByRole('button', { name: '论文库', exact: true }).waitFor({ timeout: 120000 });
  const api = (url, body) => page.evaluate(async ({ url, body }) => {
    const response = await fetch('/api/v1' + url, body === undefined ? {} : {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (!response.ok) throw Error(`${response.status}: ${await response.text()}`);
    return response.json();
  }, { url, body });
  const session = await api('/sessions', { title: 'Durable results acceptance' });
  async function runTool(name, args, sessionId = session.thread_id, expectError = false) {
    const start = outputs.length;
    const run = await api(`/sessions/${sessionId}/chat-runs`, { message: marker + JSON.stringify({ name, args, nonce: start }) });
    const deadline = Date.now() + 120000;
    while (true) {
      const status = await api(`/runs/${run.run_id}`);
      if (['completed', 'failed', 'cancelled', 'partial_failed'].includes(status.status)) {
        assert.equal(status.status, 'completed', JSON.stringify(status));
        break;
      }
      if (Date.now() > deadline) throw Error('Timed out: ' + name);
      await new Promise(resolve => setTimeout(resolve, 300));
    }
    const result = outputs.slice(start).at(-1);
    assert.ok(result, name + ': no tool result reached the model');
    assert.equal(result.startsWith('❌'), expectError, result);
    return result;
  }
  const preview = await runTool('read_pdf', { url_or_path: source, max_pages: 100 });
  const reference = meta(preview);
  assert.equal(reference.has_more, true);
  assert.ok(preview.length <= 12000);
  assert.ok(reference.total_chars > 30000);
  assert.ok(!preview.includes('TAIL_WITNESS_SOURCE_END'));
  await desktop.close();
  desktop = null;
  await launch();
  const listing = await runTool('list_tool_results', { query: 'TAIL_WITNESS_SOURCE_END' });
  assert.ok(listing.includes(reference.result_id), listing);
  const found = await runTool('search_tool_result', { result_id: reference.result_id, query: 'TAIL_WITNESS_SOURCE_END' });
  const hits = JSON.parse(found.split('\n').at(-1));
  assert.equal(hits.length, 1);
  assert.ok(hits[0].match_start > reference.end);
  const tail = await runTool('read_tool_result', { result_id: reference.result_id, offset: hits[0].offset, limit: 4000 });
  assert.ok(tail.includes('TAIL_WITNESS_SOURCE_END'));
  assert.equal(meta(tail).has_more, false);
  const stranger = await api('/sessions', { title: 'Other conversation' });
  await runTool('read_tool_result', { result_id: reference.result_id }, stranger.thread_id, true);
  console.log(JSON.stringify({ result: 'passed', sourceCharacters: reference.total_chars,
    checks: ['real long PDF extraction', 'bounded preview with complete saved source', 'app restart',
      'directory search beyond preview', 'literal tail search', 'source range recovery', 'session isolation'] }));
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  if (desktop) await desktop.close().catch(() => {});
  model.close();
});
