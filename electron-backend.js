'use strict';

/**
 * Python backend subprocess lifecycle for VARIANT-1's Electron main process.
 * Owns spawn, port-file poll, health check, restart backoff, and status fan-out.
 */

const path = require('path');
const fs = require('fs');
const http = require('http');
const crypto = require('crypto');
const { spawn, execFile, execFileSync } = require('child_process');

const MAX_BACKEND_RESTARTS = 5;
const DEFAULT_BACKEND_TIMINGS = Object.freeze({
  cachedHealthTtlMs: 2000,
  probeTimeoutMs: 1500,
  unhealthyGraceMs: 30000,
  portFileTimeoutMs: 45000,
  healthTimeoutMs: 45000,
  portFilePollMs: 150,
  healthRetryMs: 200,
  restartBaseMs: 1000,
  restartMaxMs: 8000,
  restartResetMs: 30000,
  shutdownRequestTimeoutMs: 1500,
  shutdownGraceMs: 8000,
  shutdownPollMs: 100,
  forceKillTimeoutMs: 5000,
});

function validPortRecord(data, expectedVersion = '') {
  if (!data || !Number.isFinite(data.port) || !data.token) return false;
  if (typeof data.version !== 'string' || !data.version) return false;
  if (typeof data.instance_id !== 'string' || !data.instance_id) return false;
  return !expectedVersion || data.version === expectedVersion;
}

function backendIdentityMatches(record, health, expectedVersion = '') {
  return validPortRecord(record, expectedVersion)
    && !!health
    && health.status === 'ok'
    && health.version === record.version
    && health.instance_id === record.instance_id;
}

function backendSpawnEnv(
  baseEnv,
  dataDir,
  uiPid,
  instanceId = '',
  playwrightBrowsersPath = '',
) {
  const env = {
    ...(baseEnv || {}),
    VARIANT1_DATA_DIR: dataDir,
    VARIANT1_UI_PID: String(uiPid),
    // Python otherwise inherits the active Windows code page when stdout is a
    // pipe. A Unicode window title can then make an ordinary diagnostic print
    // raise UnicodeEncodeError inside a desktop/screenshot operation.
    PYTHONUTF8: '1',
    PYTHONIOENCODING: 'utf-8',
  };
  if (instanceId) env.VARIANT1_BACKEND_INSTANCE_ID = String(instanceId);
  if (playwrightBrowsersPath) {
    env.PLAYWRIGHT_BROWSERS_PATH = String(playwrightBrowsersPath);
  }
  return env;
}

function requestBackendShutdown(info, timeoutMs = 1500) {
  return new Promise((resolve) => {
    const port = Number(info && info.port);
    const token = info && info.token != null ? String(info.token) : '';
    if (!Number.isInteger(port) || port < 1 || port > 65535 || !token) {
      resolve(false);
      return;
    }
    let settled = false;
    const finish = (ok) => {
      if (settled) return;
      settled = true;
      resolve(!!ok);
    };
    const req = http.request({
      method: 'POST',
      host: '127.0.0.1',
      port,
      path: '/shutdown',
      timeout: timeoutMs,
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Length': '0',
      },
    }, (res) => {
      res.resume();
      finish(res.statusCode >= 200 && res.statusCode < 300);
    });
    req.on('error', () => finish(false));
    req.on('timeout', () => {
      req.destroy();
      finish(false);
    });
    req.end();
  });
}

/**
 * @param {object} deps
 * @param {import('electron').App} deps.app
 * @param {string} deps.appRoot
 * @param {() => string} deps.getDataDir
 * @param {(msg: string) => void} deps.log
 * @param {(prefix: string, chunk: Buffer|string) => void} deps.logStream
 * @param {() => void} [deps.flushLogStreams]
 * @param {() => import('electron').BrowserWindow[]} deps.getStatusWindows
 * @param {() => import('electron').BrowserWindow[]} [deps.getActivityWindows]
 */
function createBackendManager(deps) {
  const {
    app, appRoot, getDataDir, log, logStream, getStatusWindows,
    getActivityWindows = () => [],
    flushLogStreams = () => {},
  } = deps;
  const expectedVersion = typeof app.getVersion === 'function' ? String(app.getVersion() || '') : '';
  const resourcesPath = deps.resourcesPath || process.resourcesPath;
  const spawnProcess = typeof deps.spawnProcess === 'function' ? deps.spawnProcess : spawn;
  const runFileSync = typeof deps.execFileSync === 'function' ? deps.execFileSync : execFileSync;
  const setTimer = typeof deps.setTimer === 'function' ? deps.setTimer : setTimeout;
  const clearTimer = typeof deps.clearTimer === 'function' ? deps.clearTimer : clearTimeout;
  const now = typeof deps.now === 'function' ? deps.now : Date.now;
  const createInstanceId = typeof deps.createInstanceId === 'function'
    ? deps.createInstanceId
    : () => crypto.randomBytes(16).toString('hex');
  const configuredTimings = deps.timings && typeof deps.timings === 'object'
    ? deps.timings : {};
  const timings = {...DEFAULT_BACKEND_TIMINGS, ...configuredTimings};

  let backendProc = null;
  let backendInfo = null;
  let backendRestarts = 0;
  let backendStopping = false;
  let portFile = null;
  let backendLastVerifiedAt = 0;
  let backendHealthMissStartedAt = 0;
  let backendHealthProbe = null;
  let backendStartPromise = null;
  let restartTimer = null;
  let restartBudgetTimer = null;
  let restartStabilityTimer = null;
  let backendStopPromise = null;

  function setPortFile(p) { portFile = p; }

  /** Return connection details only while the cached backend is still live. */
  async function getInfo() {
    if (backendInfo && backendInfo.port && backendInfo.token) {
      const cached = backendInfo;
      if (now() - backendLastVerifiedAt < timings.cachedHealthTtlMs) {
        return plainInfo(cached);
      }
      if (!backendHealthProbe) {
        backendHealthProbe = verifyCachedBackend(cached);
      }
      const probe = backendHealthProbe;
      try {
        return await probe;
      } finally {
        if (backendHealthProbe === probe) backendHealthProbe = null;
      }
    }
    const data = readPortFile();
    if (!data) return null;
    const ok = await pingHealth(data.port, timings.probeTimeoutMs, data);
    if (!ok) return null;
    if (backendStopping) return null;
    adoptBackend(data, 're-attached to');
    return plainInfo(backendInfo);
  }

  async function verifyCachedBackend(cached) {
    const record = readPortFile();
    const ok = infoMatchesRecord(cached, record)
      && await pingHealth(record.port, timings.probeTimeoutMs, record);
    if (backendInfo !== cached || backendStopping) return plainInfo(backendInfo);
    if (ok) {
      backendLastVerifiedAt = now();
      backendHealthMissStartedAt = 0;
      return plainInfo(cached);
    }

    const missedAt = now();
    if (processIsAlive(cached.pid)) {
      if (!backendHealthMissStartedAt) backendHealthMissStartedAt = missedAt;
      const missAge = missedAt - backendHealthMissStartedAt;
      if (missAge < timings.unhealthyGraceMs) {
        // A busy asyncio loop is not evidence that the process died. Keep the
        // verified identity during a bounded grace interval and probe again
        // after the normal cache TTL instead of destroying active work.
        backendLastVerifiedAt = missedAt;
        log(`[backend] health probe missed; process alive, kill deferred (${missAge}ms)`);
        return plainInfo(cached);
      }
    }

    log(`[backend] cached backend at 127.0.0.1:${cached.port} is no longer healthy`);
    const proc = backendProc;
    backendProc = null;
    backendInfo = null;
    backendLastVerifiedAt = 0;
    backendHealthMissStartedAt = 0;
    setBackendStatus('down');
    await terminateProcessTree((proc && proc.pid) || cached.pid);
    removePortFileFor(cached);
    scheduleRestart();
    return null;
  }

  function pingHealth(port, timeoutMs, record) {
    return new Promise((resolve) => {
      const req = http.get(
        {host: '127.0.0.1', port, path: '/health', timeout: timeoutMs},
        (res) => {
          let body = '';
          res.on('data', (c) => (body += c));
          res.on('end', () => {
            try {
              const j = JSON.parse(body);
              resolve(res.statusCode === 200
                && backendIdentityMatches(record, j, expectedVersion));
            } catch (_) {
              resolve(false);
            }
          });
        },
      );
      req.on('error', () => resolve(false));
      req.on('timeout', () => {
        req.destroy();
        resolve(false);
      });
    });
  }

  function plainInfo(info) {
    if (!info || typeof info !== 'object' || typeof info.then === 'function') return null;
    const port = Number(info.port);
    const token = info.token != null ? String(info.token) : '';
    if (!Number.isFinite(port) || !token) return null;
    const out = {port, token};
    const activityToken = info.activity_token != null
      ? String(info.activity_token)
      : (info.activityToken != null ? String(info.activityToken) : '');
    if (activityToken) out.activityToken = activityToken;
    if (info.pid != null && Number.isFinite(Number(info.pid))) out.pid = Number(info.pid);
    return out;
  }

  function plainActivityInfo(info) {
    const full = plainInfo(info);
    if (!full || !full.activityToken) return null;
    return {port: full.port, activityToken: full.activityToken};
  }

  function infoMatchesRecord(info, record) {
    return !!info
      && validPortRecord(record, expectedVersion)
      && Number(info.port) === Number(record.port)
      && String(info.token || '') === String(record.token || '')
      && String(info.version || '') === String(record.version || '')
      && String(info.instance_id || '') === String(record.instance_id || '');
  }

  function removePortFileFor(info) {
    try {
      const record = readPortFile();
      if (infoMatchesRecord(info, record) && fs.existsSync(portFile)) {
        fs.unlinkSync(portFile);
      }
    } catch (_) {}
  }

  function setBackendStatus(status, extra) {
    log('[backend] status: ' + status + (extra ? ' ' + extra : ''));
    // Always send a structured-clone-safe payload (no Promises / class instances).
    const payload = {status, info: plainInfo(backendInfo)};
    if (extra != null && (typeof extra === 'string' || typeof extra === 'number' || typeof extra === 'boolean')) {
      payload.detail = extra;
    }
    for (const win of getStatusWindows() || []) {
      if (win && !win.isDestroyed()) {
        try { win.webContents.send('backend:status', payload); } catch (_) {}
      }
    }
    // The overlay never receives the Main Deck bearer token.  Its separate
    // status channel carries only the scoped subscribe-only activity token.
    const activityPayload = {status, info: plainActivityInfo(backendInfo)};
    if (payload.detail != null) activityPayload.detail = payload.detail;
    for (const win of getActivityWindows() || []) {
      if (win && !win.isDestroyed()) {
        try { win.webContents.send('activity:status', activityPayload); } catch (_) {}
      }
    }
  }

  function resolveBackendCommand() {
    if (app.isPackaged) {
      const exe = path.join(resourcesPath, 'backend',
        process.platform === 'win32' ? 'Variant1Backend.exe' : 'Variant1Backend');
      if (fs.existsSync(exe)) {
        return { command: exe, args: ['--port-file', portFile], label: 'bundled exe' };
      }
      throw new Error('Packaged backend executable is missing: ' + exe + '. Repair or reinstall VARIANT-1.');
    }
    const backendDir = app.isPackaged
      ? path.join(resourcesPath, 'backend')
      : path.join(appRoot, 'backend');
    const serverPy = path.join(backendDir, 'server.py');
    const venvPy = process.platform === 'win32'
      ? path.join(backendDir, '.venv', 'Scripts', 'python.exe')
      : path.join(backendDir, '.venv', 'bin', 'python');
    if (fs.existsSync(venvPy)) {
      return { command: venvPy, args: [serverPy, '--port-file', portFile], label: 'venv python' };
    }
    const sysPy = process.platform === 'win32' ? 'python' : 'python3';
    return { command: sysPy, args: [serverPy, '--port-file', portFile], label: 'system python' };
  }

  function terminateProcessTree(pid) {
    if (!pid) return Promise.resolve(false);
    if (typeof deps.killProcessTree === 'function') {
      try {
        return Promise.resolve(deps.killProcessTree(Number(pid)))
          .then(() => true, () => false);
      } catch (_) {
        return Promise.resolve(false);
      }
    }
    if (process.platform === 'win32') {
      return new Promise((resolve) => {
        try {
          execFile('taskkill', ['/PID', String(pid), '/T', '/F'], {
            windowsHide: true,
            timeout: timings.forceKillTimeoutMs,
          }, (error) => resolve(!error));
        } catch (_) {
          resolve(false);
        }
      });
    }
    try {
        process.kill(pid, 'SIGTERM');
      return Promise.resolve(true);
    } catch (_) {
      return Promise.resolve(false); // already gone
    }
  }

  function killVariant1EngineProcesses() {
    if (typeof deps.killVariant1EngineProcesses === 'function') {
      try { deps.killVariant1EngineProcesses(); } catch (_) {}
      return;
    }
    if (process.platform !== 'win32') return;
    const roots = [appRoot];
    if (app.isPackaged && resourcesPath) {
      roots.push(resourcesPath);
    }
    try {
      const dataRoot = getDataDir();
      if (dataRoot) {
        roots.push(path.join(dataRoot, 'models', 'speech', 'whisper'));
      }
    } catch (_) {}
    const uniqueRoots = [...new Set(
      roots.filter(Boolean).map((value) => path.resolve(String(value)))
    )];
    const psRoots = uniqueRoots
      .map((value) => `'${value.replace(/'/g, "''")}'`)
      .join(',');
    const ps = [
      `$roots = @(${psRoots});`,
      "$names = 'llama-server.exe','whisper-server.exe';",
      'foreach ($name in $names) {',
      '  Get-CimInstance Win32_Process -Filter "Name=$name" | ForEach-Object {',
      '    $candidate = $_.ExecutablePath;',
      '    $owned = $false;',
      '    if ($candidate) {',
      '      foreach ($root in $roots) {',
      "        $prefix = $root.TrimEnd('\\') + '\\';",
      '        if ($candidate.Equals($root, [System.StringComparison]::OrdinalIgnoreCase) -or $candidate.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) { $owned = $true; break }',
      '      }',
      '    }',
      '    if ($owned) {',
      '      Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue',
      '    }',
      '  }',
      '}',
    ].join(' ');
    try {
      runFileSync('powershell.exe', ['-NoProfile', '-Command', ps], {
        windowsHide: true,
        stdio: 'ignore',
        timeout: timings.forceKillTimeoutMs,
      });
    } catch (_) {}
    // Managed SearXNG is a Docker container (not a project .exe). Backend stops
    // it on graceful shutdown; this is a crash / hard-quit safety net.
    try {
      runFileSync('docker', ['stop', 'variant1-searxng'], { windowsHide: true, stdio: 'ignore', timeout: 15000 });
    } catch (_) {}
  }

  function readPortFile() {
    try {
      const raw = fs.readFileSync(portFile, 'utf-8');
      const data = JSON.parse(raw);
      if (validPortRecord(data, expectedVersion)) return data;
    } catch (_) { /* not written yet */ }
    return null;
  }

  function pollPortFile(timeoutMs, expectedInstanceId = '') {
    return new Promise((resolve, reject) => {
      const deadline = now() + timeoutMs;
      const tick = () => {
        const data = readPortFile();
        if (data && (!expectedInstanceId || data.instance_id === expectedInstanceId)) {
          return resolve(data);
        }
        if (now() > deadline) return reject(new Error('port-file timeout'));
        setTimer(tick, timings.portFilePollMs);
      };
      tick();
    });
  }

  function healthCheck(port, timeoutMs, record) {
    return new Promise((resolve, reject) => {
      const deadline = now() + timeoutMs;
      const attempt = () => {
        const req = http.get(
          { host: '127.0.0.1', port, path: '/health', timeout: 1500 },
          (res) => {
            let body = '';
            res.on('data', (c) => (body += c));
            res.on('end', () => {
              try {
                const j = JSON.parse(body);
                if (res.statusCode === 200
                    && backendIdentityMatches(record, j, expectedVersion)) return resolve(j);
              } catch (_) { /* retry */ }
              retry();
            });
          }
        );
        req.on('error', retry);
        req.on('timeout', () => { req.destroy(); retry(); });
      };
      const retry = () => {
        if (now() > deadline) return reject(new Error('health timeout'));
        setTimer(attempt, timings.healthRetryMs);
      };
      attempt();
    });
  }

  function adoptBackend(data, label) {
    backendInfo = {
      port: data.port,
      token: data.token,
      activity_token: data.activity_token,
      pid: data.pid,
      version: data.version,
      instance_id: data.instance_id,
    };
    backendLastVerifiedAt = now();
    backendHealthMissStartedAt = 0;
    clearScheduledRestarts();
    scheduleStableRestartReset();
    log(`[backend] ${label} 127.0.0.1:${data.port} (pid ${data.pid || '?'})`);
    setBackendStatus('ready');
  }

  function clearStableRestartReset() {
    if (restartStabilityTimer !== null) {
      clearTimer(restartStabilityTimer);
      restartStabilityTimer = null;
    }
  }

  function scheduleStableRestartReset() {
    clearStableRestartReset();
    if (backendRestarts <= 0) return;
    restartStabilityTimer = setTimer(async () => {
      restartStabilityTimer = null;
      const candidate = backendInfo;
      if (backendStopping || !candidate) return;
      let healthy = false;
      try {
        healthy = await pingHealth(
          candidate.port,
          timings.probeTimeoutMs,
          candidate,
        );
      } catch (_) {
        healthy = false;
      }
      if (backendStopping || backendInfo !== candidate) return;
      if (!healthy) {
        // A loaded-but-live backend can miss one bounded health probe. Start a
        // fresh full stability interval rather than retaining a consumed
        // restart budget forever. Identity checks above prevent an old probe
        // from arming a timer for a replacement process.
        log('[backend] stability probe missed; restart budget reset deferred');
        scheduleStableRestartReset();
        return;
      }
      backendRestarts = 0;
      log('[backend] remained healthy; restart budget reset');
    }, timings.restartResetMs);
  }

  function clearScheduledRestarts() {
    if (restartTimer !== null) {
      clearTimer(restartTimer);
      restartTimer = null;
    }
    if (restartBudgetTimer !== null) {
      clearTimer(restartBudgetTimer);
      restartBudgetTimer = null;
    }
    clearStableRestartReset();
  }

  async function startBackend() {
    backendStopping = false;
    if (backendStartPromise) return backendStartPromise;
    backendStartPromise = startBackendOnce();
    try {
      return await backendStartPromise;
    } finally {
      backendStartPromise = null;
    }
  }

  async function startBackendOnce() {
    // Reuse a still-healthy backend instead of kill/respawn (respawn races were
    // leaving the Deck stuck on "Backend offline — reconnecting").
    const existing = readPortFile();
    if (existing) {
      try {
        await healthCheck(existing.port, Math.min(4000, timings.healthTimeoutMs), existing);
        if (backendStopping) return;
        adoptBackend(existing, 'reusing live backend at');
        // No ChildProcess handle exists for an adopted process. getInfo keeps
        // it health-checked, and stopBackend owns its verified PID on quit.
        return;
      } catch (_) {
        if (processIsAlive(existing.pid)) {
          log(
            '[backend] existing backend is alive but still starting/unhealthy; '
            + `waiting ${timings.unhealthyGraceMs}ms before replacement`,
          );
          try {
            await healthCheck(
              existing.port,
              Math.max(1, timings.unhealthyGraceMs),
              existing,
            );
            if (backendStopping) return;
            adoptBackend(existing, 'reusing recovered backend at');
            return;
          } catch (_) {
            // The exact PID remained unready for the full grace interval.
          }
        }
        log('[backend] existing port-file backend not healthy; spawning fresh');
        if (backendStopping) return;
        await terminateProcessTree(existing.pid);
        if (backendStopping) return;
      }
    }

    backendInfo = null;
    backendLastVerifiedAt = 0;
    killVariant1EngineProcesses();
    try { if (fs.existsSync(portFile)) fs.unlinkSync(portFile); } catch (_) {}

    let launch;
    try { launch = resolveBackendCommand(); }
    catch (error) { log('[backend] ' + error.message); setBackendStatus('failed'); return; }
    const { command, args, label } = launch;
    const launchInstanceId = createInstanceId();
    log(`[backend] launching via ${label}: ${command} ${args.join(' ')}`);
    setBackendStatus('starting');

    let child;
    try {
      const spawnCwd = app.isPackaged ? resourcesPath : appRoot;
      const playwrightBrowsersPath = app.isPackaged
        ? path.join(getDataDir(), 'runtimes', 'playwright')
        : path.join(appRoot, 'backend', '.playwright-browsers');
      child = spawnProcess(command, args, {
        cwd: spawnCwd,
        windowsHide: true,
        env: backendSpawnEnv(
          process.env,
          getDataDir(),
          process.pid,
          launchInstanceId,
          playwrightBrowsersPath,
        ),
      });
      backendProc = child;
    } catch (err) {
      log('[backend] spawn threw: ' + err.message);
      return scheduleRestart();
    }

    if (child.stdout && typeof child.stdout.on === 'function') {
      child.stdout.on('data', (d) => logStream('[backend] ', d));
    }
    if (child.stderr && typeof child.stderr.on === 'function') {
      child.stderr.on('data', (d) => logStream('[backend:err] ', d));
    }

    let rejectProcessFailure;
    const processFailure = new Promise((_, reject) => { rejectProcessFailure = reject; });
    let processFailureReported = false;
    const reportProcessFailure = (err) => {
      if (processFailureReported) return;
      processFailureReported = true;
      rejectProcessFailure(err);
    };

    child.on('error', (err) => {
      flushLogStreams();
      log('[backend] process error: ' + err.message +
        (err.code === 'ENOENT' ? ' (Python not found — install deps: pip install -r backend/requirements.txt)' : ''));
      reportProcessFailure(err);
    });

    child.on('exit', (code, signal) => {
      flushLogStreams();
      log(`[backend] exited code=${code} signal=${signal}`);
      const isCurrent = backendProc === child;
      const wasReady = !!backendInfo;
      if (isCurrent) {
        backendProc = null;
        backendInfo = null;
        backendLastVerifiedAt = 0;
      }
      // Startup failure is settled by startBackendOnce(), which awaits the
      // process-tree kill before arming a replacement. Only an already-ready
      // generation owns restart scheduling directly from its exit event.
      if (isCurrent && wasReady && !backendStopping) {
        setBackendStatus('down');
        scheduleRestart();
      }
      reportProcessFailure(new Error(`backend exited before ready (${code ?? signal ?? 'unknown'})`));
    });

    try {
      const readiness = (async () => {
        // Windows venv launchers may wait on a separately spawned interpreter,
        // so ChildProcess.pid is not a stable backend identity. The per-attempt
        // instance nonce distinguishes this launch from late/stale writers.
        const data = await pollPortFile(
          timings.portFileTimeoutMs, launchInstanceId);
        await healthCheck(data.port, timings.healthTimeoutMs, data);
        return data;
      })();
      const data = await Promise.race([readiness, processFailure]);
      if (backendStopping || backendProc !== child) return;
      adoptBackend(data, 'ready on');
    } catch (err) {
      log('[backend] failed to become ready: ' + err.message);
      if (backendProc === child) backendProc = null;
      await terminateProcessTree(child.pid);
      killVariant1EngineProcesses();
      backendInfo = null;
      backendLastVerifiedAt = 0;
      // Both readiness failure and process exit converge on this one timer.
      if (!backendStopping) scheduleRestart();
    }
  }

  function scheduleRestart() {
    clearStableRestartReset();
    if (backendStopping || restartTimer !== null || restartBudgetTimer !== null) return;
    if (backendRestarts >= MAX_BACKEND_RESTARTS) {
      log('[backend] giving up after ' + backendRestarts + ' restarts.');
      setBackendStatus('failed');
      // Soft-reset the budget so a later Deck open / getInfo can try again
      // after a cool-down instead of leaving the app permanently offline.
      restartBudgetTimer = setTimer(() => {
        restartBudgetTimer = null;
        if (backendStopping || backendInfo || backendProc) return;
        backendRestarts = 0;
        log('[backend] restart budget reset; trying again');
        startBackend();
      }, timings.restartResetMs);
      return;
    }
    backendRestarts += 1;
    const delay = Math.min(
      timings.restartBaseMs * 2 ** (backendRestarts - 1),
      timings.restartMaxMs,
    );
    log(`[backend] restart #${backendRestarts} in ${delay}ms`);
    restartTimer = setTimer(() => {
      restartTimer = null;
      startBackend();
    }, delay);
  }

  function processIsAlive(pid) {
    if (!Number.isInteger(Number(pid)) || Number(pid) <= 0) return false;
    if (typeof deps.isProcessAlive === 'function') {
      try { return !!deps.isProcessAlive(Number(pid)); }
      catch (error) { log('[backend] process liveness probe failed; checking OS liveness: ' + String(error?.message || error)); }
    }
    try {
      process.kill(Number(pid), 0);
      return true;
    } catch (error) {
      return !!(error && error.code === 'EPERM');
    }
  }

  function pause(ms) {
    return new Promise((resolve) => setTimer(resolve, ms));
  }

  async function waitForBackendExit(proc, info, timeoutMs) {
    if (typeof deps.waitForBackendExit === 'function') {
      return !!(await deps.waitForBackendExit(proc, info, timeoutMs));
    }
    const pids = new Set();
    if (proc && proc.pid) pids.add(Number(proc.pid));
    if (info && info.pid) pids.add(Number(info.pid));
    const deadline = now() + timeoutMs;
    do {
      if (pids.size > 0) {
        if ([...pids].every((pid) => !processIsAlive(pid))) return true;
      } else if (info && info.port) {
        const healthy = await pingHealth(
          info.port,
          Math.min(timings.probeTimeoutMs, 300),
          info,
        );
        if (!healthy) return true;
      } else if (!proc || proc.exitCode != null || proc.signalCode != null) {
        return true;
      }
      if (now() >= deadline) return false;
      await pause(Math.min(timings.shutdownPollMs, Math.max(1, deadline - now())));
    } while (now() <= deadline);
    return false;
  }

  async function stopBackendOnce() {
    backendStopping = true;
    clearScheduledRestarts();
    const proc = backendProc;
    let info = backendInfo;
    const candidate = info ? null : readPortFile();
    backendProc = null;
    backendInfo = null;
    backendLastVerifiedAt = 0;
    backendHealthProbe = null;
    // Quit can race startup after the authenticated record is published but
    // before readiness adopts it. Verify that identity before treating its PID
    // as owned or sending the bearer-authenticated shutdown request.
    if (!info && candidate) {
      try {
        if (await pingHealth(candidate.port, timings.probeTimeoutMs, candidate)) {
          info = candidate;
        }
      } catch (_) {}
    }
    const requestShutdown = typeof deps.requestGracefulShutdown === 'function'
      ? deps.requestGracefulShutdown
      : requestBackendShutdown;
    let stoppedGracefully = false;
    if (info && info.port && info.token) {
      try {
        const accepted = await requestShutdown(info, timings.shutdownRequestTimeoutMs);
        if (accepted) {
          stoppedGracefully = await waitForBackendExit(
            proc,
            info,
            timings.shutdownGraceMs,
          );
        }
      } catch (error) {
        log('[backend] graceful shutdown request failed: ' +
          String(error && error.message || error));
      }
    }
    const pids = new Set();
    if (proc && proc.pid) pids.add(Number(proc.pid));
    if (info && info.pid) pids.add(Number(info.pid));
    if (!stoppedGracefully) {
      if (pids.size) log('[backend] graceful shutdown unavailable or timed out; forcing process tree');
      await Promise.all([...pids].map((pid) => terminateProcessTree(pid)));
      killVariant1EngineProcesses();
    } else {
      log('[backend] graceful shutdown complete');
    }
    if (info) removePortFileFor(info);
    else {
      try { if (portFile && fs.existsSync(portFile)) fs.unlinkSync(portFile); } catch (_) {}
    }
  }

  function stopBackend() {
    backendStopping = true;
    if (backendStopPromise) return backendStopPromise;
    backendStopPromise = stopBackendOnce().finally(() => {
      backendStopPromise = null;
    });
    return backendStopPromise;
  }

  return {
    setPortFile,
    getInfo,
    setBackendStatus,
    startBackend,
    stopBackend,
    killVariant1EngineProcesses,
  };
}

/**
 * Register backend:getInfo for renderers. Lives next to the backend manager so
 * main.js does not hold IPC bodies for the Python hub.
 *
 * @param {object} opts
 * @param {import('electron').IpcMain} opts.ipcMain
 * @param {(event: any, win?: any) => boolean} opts.isTrustedIpcSender
 * @param {() => object|null} opts.getInfo
 */
function registerBackendIpc({ ipcMain, isTrustedIpcSender, getInfo }) {
  ipcMain.handle('backend:getInfo', async (event) => {
    if (!isTrustedIpcSender(event)) return null;
    try {
      return await getInfo();
    } catch (_) {
      return null;
    }
  });
}

module.exports = {
  createBackendManager,
  registerBackendIpc,
  validPortRecord,
  backendIdentityMatches,
  backendSpawnEnv,
  requestBackendShutdown,
};
