// Keep update operations in the main process; the renderer receives status only.
function createUpdater({ updater, version, supported, notify, prepareInstall, recoverInstall }) {
  let state = { status: supported ? 'idle' : 'unsupported', currentVersion: version,
    availableVersion: '', releaseNotes: '', percent: 0, error: '' };
  let downloaded = false;
  const snapshot = () => ({ ...state });
  function set(change) { state = { ...state, ...change }; notify(snapshot()); }
  function fail(error) {
    const installing = state.status === 'installing';
    const raw = String(error?.message || error);
    const message = /404|No published versions|ERR_UPDATER_NO_PUBLISHED_VERSIONS|ERR_UPDATER_LATEST_VERSION_NOT_FOUND/i.test(raw)
      ? '更新源尚未发布可用版本，请稍后重试。'
      : /ENOTFOUND|ECONN|ETIMEDOUT|net::/.test(raw)
        ? '暂时无法连接更新服务，请检查网络后重试。'
        : /checksum|sha512|ERR_UPDATER_INVALID_SIGNATURE/i.test(raw)
          ? '更新文件校验失败，请重新下载。'
          : raw.slice(0, 400);
    set({ status: downloaded ? 'downloaded' : 'error', error: message });
    if (installing) recoverInstall();
  }
  function release(info) {
    const notes = typeof info.releaseNotes === 'string' ? info.releaseNotes
      : Array.isArray(info.releaseNotes) ? info.releaseNotes.map(item => item.note || '').join('\n') : '';
    return { availableVersion: info.version, releaseNotes: notes.slice(0, 6000) };
  }
  updater.autoDownload = false;
  updater.autoInstallOnAppQuit = false;
  updater.allowPrerelease = false;
  updater.allowDowngrade = false;
  updater.on('error', fail);
  updater.on('checking-for-update', () => { if (!downloaded) set({ status: 'checking', error: '' }); });
  updater.on('update-available', info => { if (!downloaded) set({ ...release(info), status: 'available', error: '' }); });
  updater.on('update-not-available', () => { if (!downloaded) set({ status: 'up-to-date', availableVersion: '', releaseNotes: '', percent: 0, error: '' }); });
  updater.on('download-progress', progress => set({ status: 'downloading', percent: Math.min(100, Math.max(0, progress.percent || 0)) }));
  updater.on('update-downloaded', info => { downloaded = true; set({ ...release(info), status: 'downloaded', percent: 100, error: '' }); });
  return {
    snapshot,
    async check() {
      if (!supported || downloaded || ['checking', 'downloading', 'installing'].includes(state.status)) return snapshot();
      set({ status: 'checking', error: '' });
      try { await updater.checkForUpdates(); } catch (error) { fail(error); }
      return snapshot();
    },
    async download() {
      if (!supported || downloaded || !['available', 'error'].includes(state.status) || !state.availableVersion) return snapshot();
      set({ status: 'downloading', percent: 0, error: '' });
      try { await updater.downloadUpdate(); } catch (error) { fail(error); }
      return snapshot();
    },
    async install() {
      if (!supported || !downloaded || state.status !== 'downloaded') return snapshot();
      set({ status: 'installing', error: '' });
      try {
        await prepareInstall();
        updater.quitAndInstall(true, true);
      } catch (error) { fail(error); }
      return snapshot();
    },
  };
}
module.exports = { createUpdater };
