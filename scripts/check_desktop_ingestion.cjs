// Exercise real packaged PDF import, OCR, Chroma writes and retrieval.
// The local model only dispatches tools; no paid model or user database is used.
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const root = path.resolve(__dirname, '..');
const driver = process.env.PLAYWRIGHT_NODE_PATH || execFileSync('python', ['-c',
  'import pathlib,playwright; print(pathlib.Path(playwright.__file__).parent/"driver"/"package")'], { encoding: 'utf8' }).trim();
const { _electron } = require(driver);
const profile = path.join(root, '.visual_check', 'desktop-profile-ingestion-' + Date.now());
fs.mkdirSync(path.join(profile, 'runtime', 'primary'), { recursive: true });
fs.writeFileSync(path.join(profile, 'runtime', 'primary', 'settings.json'), JSON.stringify({ rag_enabled: true, daily_search_enabled: false }));
const source = path.resolve(process.argv[2] || path.join(profile, 'source paper.pdf'));
const algorithmNumber = process.argv[3] || '4';
assert.match(algorithmNumber, /^\d+$/);
const figureNumber = process.argv[4];
if (figureNumber) assert.match(figureNumber, /^\d+$/);
if (!process.argv[2]) {
  execFileSync('python', ['-c', [
    'import pymupdf,sys',
    'doc=pymupdf.open(); page=doc.new_page(width=600,height=800)',
    'page.insert_text((50,50),"Algorithm 4: Searchable raster pseudocode")',
    '[page.draw_line((45,y),(305,y)) for y in (36,54,260)]',
    'image=pymupdf.open(); body=image.new_page(width=260,height=180)',
    'body.insert_text((8,20),"1 Function LBC(paths, r):\\n2 for edge in paths do\\n3 add edge to edgeList\\n4 end\\n5 Return paths with smallest averBand",fontsize=10)',
    'page.insert_image(pymupdf.Rect(45,60,305,240),stream=body.get_pixmap(matrix=pymupdf.Matrix(3,3)).tobytes("png"))',
    'doc.save(sys.argv[1])',
  ].join('\n'), source]);
}
const outputs = [];
const dispatched = new Set();
const marker = 'PDF_INGEST_CHECK\n';
let desktop;
const model = http.createServer((req, res) => {
  let body = '';
  req.on('data', chunk => { body += chunk; });
  req.on('end', () => {
    const payload = JSON.parse(body);
    const messages = payload.messages || [];
    const lastUser = messages.findLastIndex(m => m.role === 'user' && typeof m.content === 'string' && m.content.startsWith(marker));
    const toolResults = messages.slice(lastUser + 1).filter(m => m.role === 'tool');
    outputs.push(...toolResults.map(m => String(m.content)));
    let message = { role: 'assistant', content: 'PDF 工具验证完成。' };
    const requestKey = lastUser < 0 ? '' : messages[lastUser].content;
    if (lastUser >= 0 && !dispatched.has(requestKey) && payload.tools?.length) {
      dispatched.add(requestKey);
      const action = JSON.parse(messages[lastUser].content.slice(marker.length));
      message = { role: 'assistant', content: null, reasoning_content: 'Dispatch the requested local test tool.',
        tool_calls: [{ id: 'call_ingestion', type: 'function',
        function: { name: action.name, arguments: JSON.stringify(action.args) } }] };
    } else if (!payload.stream) {
      message.content = JSON.stringify({ verdict: 'pass', issues: [] });
    }
    const finish_reason = message.tool_calls ? 'tool_calls' : 'stop';
    const usage = { prompt_tokens: 10, completion_tokens: 10 };
    if (payload.stream) {
      const delta = message.tool_calls ? { reasoning_content: message.reasoning_content,
        tool_calls: message.tool_calls.map((c, index) => ({ index, ...c })) } : { content: message.content };
      res.writeHead(200, { 'Content-Type': 'text/event-stream; charset=utf-8' });
      res.end('data: ' + JSON.stringify({ choices: [{ delta, finish_reason: null }] }) + '\n\ndata: '
        + JSON.stringify({ choices: [{ delta: {}, finish_reason }], usage }) + '\n\ndata: [DONE]\n\n');
    } else {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ choices: [{ message, finish_reason }], usage }));
    }
  });
});

(async () => {
  await new Promise(resolve => model.listen(0, '127.0.0.1', resolve));
  const env = { ...process.env, RESEARCH_ASSISTANT_USER_DATA: profile,
    DEEPSEEK_API_URL: `http://127.0.0.1:${model.address().port}/chat/completions`, PYTHONUTF8: '1',
    PATH: path.join(process.env.SystemRoot, 'System32'), TESSDATA_PREFIX: path.join(profile, 'not-installed') };
  delete env.ELECTRON_RUN_AS_NODE;
  desktop = await _electron.launch({ executablePath: path.join(root, 'desktop/release/win-unpacked/Research Assistant.exe'),
    args: ['--headless'], env, timeout: 120000 });
  const page = await desktop.firstWindow();
  await page.getByRole('button', { name: '论文库', exact: true }).waitFor({ timeout: 120000 });
  await page.evaluate(() => window.desktop.configure({ deepseekKey: 'local-test-not-a-real-key' }));
  await page.waitForURL('**/startup.html');
  await page.getByRole('button', { name: '论文库', exact: true }).waitFor({ timeout: 120000 });
  const api = (url, body) => page.evaluate(async ({ url, body }) => {
    const response = await fetch('/api/v1' + url, body === undefined ? {} : {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (!response.ok) throw Error(`${response.status}: ${await response.text()}`);
    return response.json();
  }, { url, body });
  async function runTool(name, args) {
    const start = outputs.length;
    const session = await api('/sessions', { title: 'PDF ingestion acceptance' });
    const run = await api(`/sessions/${session.thread_id}/chat-runs`, { message: marker + JSON.stringify({ name, args }) });
    const deadline = Date.now() + 180000;
    while (true) {
      const status = await api(`/runs/${run.run_id}`);
      if (['completed', 'failed', 'cancelled', 'partial_failed'].includes(status.status)) {
        assert.equal(status.status, 'completed', JSON.stringify(status));
        break;
      }
      if (Date.now() > deadline) throw Error(`Timed out: ${name}`);
      await new Promise(resolve => setTimeout(resolve, 300));
    }
    const result = outputs.slice(start).join('\n');
    assert.ok(result, `${name}: model never received a tool result`);
    assert.ok(!result.includes('❌'), result);
    console.log(`${name}: passed`);
    return result;
  }
  const firstRead = await runTool('read_pdf', { url_or_path: '"' + source + '"', max_pages: 100 });
  assert.match(firstRead, /list_paper_images/);
  const library = await api('/library');
  assert.equal(library.documents.length, 1);
  const document = library.documents[0];
  const listing = await runTool('list_papers', {});
  assert.ok(listing.includes(`library/${document.id}.pdf`));
  // Re-ingest by its managed path: source mapping survives and duplicates replace safely.
  await runTool('read_pdf', { url_or_path: `library/${document.id}.pdf`, max_pages: 100 });
  const papers = await api('/workspace/papers');
  assert.equal(papers.length, 1);
  assert.ok(papers[0].chunks > 0);
  const answer = await runTool('query_papers', { query: 'Algorithm ' + algorithmNumber, paper_id_or_title: papers[0].paper_id, top_k: 1 });
  assert.match(answer, new RegExp('Algorithm ' + algorithmNumber + '\\b', 'i'));
  assert.match(answer, /Return/i);
  if (figureNumber) {
    const images = await runTool('list_paper_images', { paper_id_or_title: papers[0].paper_id, figure: figureNumber });
    const imagePath = images.match(/图片路径：([^\r\n]+)/)?.[1];
    assert.ok(imagePath && fs.existsSync(imagePath), images);
    assert.match(images, /整页原图/);
    fs.unlinkSync(imagePath);
    const vision = await runTool('describe_image', { paper_id_or_title: papers[0].paper_id, figure: figureNumber });
    assert.match(vision, new RegExp('Figure ' + figureNumber + '\\b'));
    assert.ok(fs.existsSync(imagePath), 'Deleted image was not regenerated');
  }
  console.log(JSON.stringify({ profile, pages: document.pages, indexedPapers: papers.length, chunks: papers[0].chunks,
    checks: ['quoted absolute path import', 'bundled OCR without system installation', 'library listing',
      'managed-path reindex', 'duplicate replacement', 'algorithm retrieval',
      ...(figureNumber ? ['figure directory', 'vision by figure', 'image cache recovery'] : [])] }));
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  if (desktop) await desktop.close().catch(() => {});
  model.close();
});
