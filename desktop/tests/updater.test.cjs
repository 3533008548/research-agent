const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { createUpdater } = require('../updater.cjs');

function setup(options = {}) {
  const engine = new EventEmitter();
  const calls = [], states = [];
  engine.checkForUpdates = async () => { calls.push('check'); engine.emit('update-available', { version: '0.3.0', releaseNotes: '<b>Plain text notes</b>' }); };
  engine.downloadUpdate = async () => { calls.push('download'); engine.emit('download-progress', { percent: 42 }); engine.emit('update-downloaded', { version: '0.3.0' }); };
  engine.quitAndInstall = (...args) => calls.push(['install', ...args]);
  const service = createUpdater({ updater: engine, version: '0.2.2', supported: true,
    notify: state => states.push(state), prepareInstall: async () => calls.push('stop backend'),
    recoverInstall: () => calls.push('recover'), ...options });
  return { service, engine, calls, states };
}

test('explicit download, progress and backend shutdown before silent install', async () => {
  const { service, engine, calls, states } = setup();
  assert.equal(engine.autoDownload, false);
  assert.equal(engine.autoInstallOnAppQuit, false);
  assert.equal(engine.allowDowngrade, false);
  await service.install(); await service.download();
  assert.deepEqual(calls, []);
  assert.equal((await service.check()).status, 'available');
  assert.deepEqual(calls, ['check']);
  await service.download();
  assert.ok(states.some(s => s.status === 'downloading' && s.percent === 42));
  assert.equal(service.snapshot().status, 'downloaded');
  await service.check(); await service.download();
  assert.deepEqual(calls, ['check', 'download']);
  await service.install(); await service.install();
  assert.deepEqual(calls.slice(2), ['stop backend', ['install', true, true]]);
});

test('deduplicates overlapping requests', async () => {
  const { service, engine, calls } = setup();
  let finish;
  engine.checkForUpdates = () => { calls.push('check'); return new Promise(resolve => { finish = resolve; }); };
  const pending = service.check();
  await service.check();
  assert.deepEqual(calls, ['check']);
  engine.emit('update-available', { version: '0.3.0' }); finish(); await pending;
  engine.downloadUpdate = () => { calls.push('download'); return new Promise(resolve => { finish = resolve; }); };
  const downloading = service.download();
  await service.download(); await service.check();
  assert.deepEqual(calls, ['check', 'download']);
  engine.emit('update-downloaded', { version: '0.3.0' }); finish(); await downloading;
});

test('failed download can retry and cannot be installed until verified', async () => {
  const { service, engine, calls } = setup();
  await service.check();
  const download = engine.downloadUpdate;
  engine.downloadUpdate = async () => { throw Error('sha512 checksum mismatch'); };
  assert.equal((await service.download()).status, 'error');
  await service.install();
  assert.deepEqual(calls, ['check']);
  engine.downloadUpdate = download;
  assert.equal((await service.download()).status, 'downloaded');
  assert.equal(service.snapshot().error, '');
});

test('running tasks keep ready update and prevent installation', async () => {
  let running = true;
  const { service, calls } = setup({ prepareInstall: async () => { if (running) throw Error('任务正在运行'); } });
  await service.check(); await service.download();
  assert.equal((await service.install()).status, 'downloaded');
  assert.match(service.snapshot().error, /任务/);
  assert.equal(calls.some(c => Array.isArray(c)), false);
  running = false; await service.install();
  assert.deepEqual(calls.at(-1), ['install', true, true]);
});

test('installer handoff failure restores service and allows retry', async () => {
  const { service, engine, calls } = setup();
  await service.check(); await service.download();
  engine.quitAndInstall = () => { engine.emit('error', Error('Cannot launch installer')); };
  assert.equal((await service.install()).status, 'downloaded');
  assert.equal(calls.at(-1), 'recover');
});

test('missing release, offline service and current version have distinct states', async () => {
  const { service, engine } = setup();
  engine.checkForUpdates = async () => { throw Error('404 latest.yml'); };
  assert.match((await service.check()).error, /尚未发布/);
  engine.checkForUpdates = async () => { throw Error('net::ERR_CONNECTION_REFUSED'); };
  assert.match((await service.check()).error, /网络/);
  engine.checkForUpdates = async () => engine.emit('update-not-available');
  const state = await service.check();
  assert.equal(state.status, 'up-to-date'); assert.equal(state.error, '');
});

test('development mode never contacts the updater', async () => {
  const { service, calls } = setup({ supported: false });
  await service.check(); await service.download(); await service.install();
  assert.equal(service.snapshot().status, 'unsupported');
  assert.deepEqual(calls, []);
});
