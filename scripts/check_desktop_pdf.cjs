// Packaged regression: a stale caption-only index must recover vector pseudocode.
// Isolated profile, real local OCR, deterministic local model, no installed app changes.
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const root = path.resolve(__dirname, '..');
const driver = process.env.PLAYWRIGHT_NODE_PATH || execFileSync('python', ['-c', 'import pathlib,playwright; print(pathlib.Path(playwright.__file__).parent/"driver"/"package")'], { encoding: 'utf8' }).trim();
const { _electron } = require(driver);
const profile = path.join(root, '.visual_check', 'desktop-pdf-' + Date.now());
fs.mkdirSync(profile, { recursive: true });
execFileSync('python', ['-X', 'utf8', '-c', `
import json,sys
from pathlib import Path
import pymupdf,chromadb
from chromadb.config import Settings
root=Path(sys.argv[1])/'runtime'
papers=root/'primary/papers';papers.mkdir(parents=True)
(root/'primary/settings.json').write_text(json.dumps({'rag_enabled':True,'daily_search_enabled':False}))
with pymupdf.open() as pdf:
 p=pdf.new_page(width=600,height=800)
 p.insert_text((50,50),'Algorithm 1 Routing Algorithm')
 p.insert_text((325,80),'Independent native prose in the other column.',fontsize=9)
 for y in (36,54,520):p.draw_line((45,y),(305,y))
 with pymupdf.open() as code:
  cp=code.new_page(width=260,height=450)
  body='1 Function Route(graph):\\n2 Input graph and source node\\n3 Output best path\\n'
  body+='\\n'.join(str(i)+' for edge in graph do keep path' for i in range(4,25))
  body+='\\n25 Return finalWitness'
  cp.insert_text((8,20),body,fontsize=10)
  with pymupdf.open(stream=cp.get_svg_image(text_as_path=True).encode(),filetype='svg') as svg:
   with pymupdf.open(stream=svg.convert_to_pdf(),filetype='pdf') as outlined:
    p.show_pdf_page(pymupdf.Rect(45,60,305,510),outlined,0)
 pdf.save(papers/'vector-algorithm.pdf')
 assert 'finalWitness' not in p.get_text()
db=root/'derived/chroma';db.mkdir(parents=True)
c=chromadb.PersistentClient(path=str(db),settings=Settings(anonymized_telemetry=False))
collection=c.get_or_create_collection('papers',metadata={'hnsw:space':'cosine'})
collection.add(ids=['old-caption'],documents=['Algorithm 1 Routing Algorithm'],embeddings=[[1.0]+[0.0]*383],metadatas=[{'paper_id':'fixture-paper','title':'Routing Fixture','chunk_index':0,'section':'Methods','page':1}])
artifacts=root/'derived/paper_artifacts/fixture-paper';artifacts.mkdir(parents=True)
(artifacts/'document_map.json').write_text(json.dumps({'paper_id':'fixture-paper','title':'Routing Fixture','source_file':'vector-algorithm.pdf','extraction_version':2,'processed_pages':1,'total_pages':1,'pages':[],'chunks':[]}))
`, profile], { cwd: root, stdio: ['ignore', 'pipe', 'pipe'] });
let desktop;
let recovered = '', transcribed = false;
const requests = [];
const model = http.createServer((request, response) => {
  let body = '';
  request.on('data', chunk => { body += chunk; });
  request.on('end', () => {
    try {
      const payload = JSON.parse(body); requests.push(payload);
      if (!payload.stream) {
        const content = payload.messages.at(-1).content;
        if (Array.isArray(content) && content.some(part => part.type === 'image_url')) {
          assert.match(content[0].text, /逐行转写/);
          transcribed = true;
        }
        response.writeHead(200, { 'Content-Type': 'application/json' });
        response.end(JSON.stringify({ choices: [{ message: { content: transcribed ? '1 Function Route(graph)\n25 Return finalWitness' : '核验通过。' } }] }));
        return;
      }
      const toolResults = payload.messages.filter(message => message.role === 'tool');
      if (requests.length <= 4) fs.writeFileSync(path.join(profile, 'model-input-' + requests.length + '.json'), JSON.stringify(payload.messages));
      assert.ok(requests.length <= 8, 'Unexpected repeated tool loop');
      const evidence = toolResults.find(message => String(message.content).includes('算法原页取证'));
      let call;
      if (!evidence) {
        call = { name: 'query_papers', arguments: JSON.stringify({ query: 'Algorithm 1', paper_id_or_title: 'fixture-paper', top_k: 1 }) };
      } else if (!transcribed) {
        recovered = String(evidence.content);
        assert.match(recovered, /finalWitness/);
        assert.match(recovered, /OCR/);
        const crop = recovered.match(/原页区域[^\n]*?：([^\r\n]+)/)?.[1];
        assert.ok(crop && fs.existsSync(crop), recovered);
        call = { name: 'describe_image', arguments: JSON.stringify({ image_path: crop, task: 'transcribe_algorithm' }) };
      }
      const delta = call ? { reasoning_content: 'Read and verify source evidence.', tool_calls: [{ index: 0, id: 'pdf-check-' + requests.length, type: 'function', function: call }] } : { content: '算法原页已完整读取并核对：finalWitness。' };
      response.writeHead(200, { 'Content-Type': 'text/event-stream; charset=utf-8' });
      response.end('data: ' + JSON.stringify({ choices: [{ delta, finish_reason: null }] }) + '\n\ndata: ' + JSON.stringify({ choices: [{ delta: {}, finish_reason: call ? 'tool_calls' : 'stop' }] }) + '\n\ndata: [DONE]\n\n');
    } catch (error) { response.writeHead(500); response.end(String(error)); }
  });
});

(async () => {
  await new Promise(resolve => model.listen(0, '127.0.0.1', resolve));
  const development = process.argv.includes('--dev');
  const env = { ...process.env, RESEARCH_ASSISTANT_USER_DATA: profile, DEEPSEEK_API_URL: `http://127.0.0.1:${model.address().port}/chat/completions`, PYTHONUTF8: '1' };
  delete env.ELECTRON_RUN_AS_NODE;
  if (!development) env.PATH = path.join(process.env.SystemRoot, 'System32');
  desktop = await _electron.launch({ executablePath: development ? path.join(root, 'desktop/node_modules/electron/dist/electron.exe') : path.join(root, 'desktop/release/win-unpacked/Research Assistant.exe'), args: [...(development ? [path.join(root, 'desktop')] : []), '--headless'], env, timeout: 120000 });
  const page = await desktop.firstWindow(); page.setDefaultTimeout(30000);
  await page.getByRole('button', { name: '设置', exact: true }).waitFor({ timeout: 120000 });
  await page.evaluate(() => window.desktop.configure({ deepseekKey: 'desktop-pdf-fake-key-not-a-credential' }));
  await page.waitForURL('**/startup.html');
  await page.getByRole('textbox', { name: '消息', exact: true }).waitFor({ timeout: 120000 });
  await page.getByRole('textbox', { name: '消息', exact: true }).fill('解释 Routing Fixture 的算法一，核对最后一行。');
  await page.getByRole('button', { name: /发送|submit/i }).click();
  await page.locator('.message-assistant').filter({ hasText: '算法原页已完整读取并核对：finalWitness。' }).waitFor({ timeout: 120000 });
  assert.ok(recovered.includes('finalWitness') && transcribed);
  const original = JSON.parse(fs.readFileSync(path.join(profile, 'runtime/derived/paper_artifacts/fixture-paper/document_map.json'), 'utf8'));
  assert.equal(original.extraction_version, 2);
  assert.deepEqual(original.chunks, []);
  assert.ok(fs.readdirSync(path.join(profile, 'runtime/derived/paper_artifacts/fixture-paper')).some(name => name.startsWith('algorithms-v')));
  console.log('Desktop PDF passed: stale index, punctuationless caption, vector pseudocode, bundled OCR, complete tail, source crop, dedicated transcription and unchanged original index.');
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  if (desktop) await desktop.close().catch(() => {});
  model.close();
});
