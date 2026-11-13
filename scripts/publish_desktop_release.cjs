// Build locally first. Upload as a draft; --publish explicitly makes it public.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { execFileSync } = require('node:child_process');
const root = path.resolve(__dirname, '..');

async function verifyArtifacts() {
  const { load } = require('../desktop/node_modules/js-yaml');
  const pkg = JSON.parse(fs.readFileSync(path.join(root, 'desktop/package.json'), 'utf8'));
  const release = path.join(root, 'desktop/release');
  const manifest = path.join(release, 'latest.yml');
  const info = load(fs.readFileSync(manifest, 'utf8'));
  const filename = `Research-Assistant-Setup-${pkg.version}.exe`;
  const installer = path.join(release, filename);
  const blockmap = installer + '.blockmap';
  const entry = info.files?.find(file => file.url === filename);
  if (info.version !== pkg.version || !entry || info.path !== filename)
    throw Error('版本或安装包名称不匹配，请重新构建。');
  if (fs.statSync(installer).size !== entry.size || fs.statSync(blockmap).size === 0)
    throw Error('安装包或增量更新文件不完整。');
  const hash = crypto.createHash('sha512');
  for await (const chunk of fs.createReadStream(installer)) hash.update(chunk);
  const digest = hash.digest('base64');
  if (digest !== entry.sha512 || digest !== info.sha512)
    throw Error('安装包校验失败，请重新构建。');
  return { version: pkg.version, repository: `${pkg.build.publish.owner}/${pkg.build.publish.repo}`,
    artifacts: [installer, blockmap, manifest] };
}

async function main() {
  const args = process.argv.slice(2);
  if (args.some(arg => !['--verify-only', '--publish'].includes(arg)))
    throw Error('用法: node scripts/publish_desktop_release.cjs [--verify-only | --publish]');
  const result = await verifyArtifacts();
  console.log(`已验证 v${result.version}：安装包、SHA-512 和更新元数据。`);
  if (args.includes('--verify-only')) return;
  const run = (command, values) => execFileSync(command, values, { cwd: root, encoding: 'utf8', stdio: ['ignore', 'pipe', 'inherit'] }).trim();
  if (run('git', ['status', '--porcelain'])) throw Error('请先提交代码和更新说明，再发布与提交对应的安装包。');
  const commit = run('git', ['rev-parse', 'HEAD']);
  const notes = path.join(root, `desktop/releases/${result.version}.md`);
  if (!fs.existsSync(notes)) throw Error(`缺少更新说明：${notes}`);
  const tag = `v${result.version}`;
  // The release stays hidden until all three files have uploaded successfully.
  console.log(run('gh', ['release', 'create', tag, ...result.artifacts, '--repo', result.repository,
    '--target', commit, '--title', `Research Assistant ${result.version}`, '--notes-file', notes, '--draft']));
  if (args.includes('--publish')) {
    console.log(run('gh', ['release', 'edit', tag, '--repo', result.repository, '--draft=false', '--latest']));
    console.log('版本已发布，客户端可在设置中检查更新。');
  } else console.log('已创建草稿。核对后在 GitHub Releases 发布，客户端才会收到更新。');
}

if (require.main === module) main().catch(error => { console.error(error.message); process.exitCode = 1; });
module.exports = { verifyArtifacts };
