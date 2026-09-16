'use strict';

const assert = require('assert');
const {EventEmitter} = require('events');
const fs = require('fs');
const http = require('http');
const os = require('os');
const path = require('path');
const {
  validPortRecord,
  backendIdentityMatches,
  backendSpawnEnv,
  createBackendManager,
  requestBackendShutdown,
  resolvePosixEngineExecutable,
  isOwnedPosixEnginePath,
} = require('../electron-backend');
const {createBeforeQuitHandler} = require('../electron-app-boot');

const VERSION = '0.1.0';

function assertIdentityContracts() {
  const record = {
    port: 43125,
    token: 'secret-token',
    pid: 1234,
    version: VERSION,
    instance_id: 'instance-a',
  };
  const health = {
    status: 'ok',
    version: VERSION,
    instance_id: 'instance-a',
  };

  assert.strictEqual(validPortRecord(record, VERSION), true);
  assert.strictEqual(validPortRecord({...record, version: ''}, VERSION), false);
  assert.strictEqual(validPortRecord({...record, instance_id: ''}, VERSION), false);
  assert.strictEqual(validPortRecord(record, '0.2.0'), false);

  assert.strictEqual(backendIdentityMatches(record, health, VERSION), true);
  assert.strictEqual(
    backendIdentityMatches(record, {...health, status: 'starting'}, VERSION),
    false,
  );
  assert.strictEqual(
    backendIdentityMatches(record, {...health, version: '0.2.0'}, VERSION),
    false,
  );
  assert.strictEqual(
    backendIdentityMatches(record, {...health, instance_id: 'instance-b'}, VERSION),
    false,
  );
  assert.strictEqual(backendIdentityMatches(record, {status: 'ok'}, VERSION), false);

  const spawnEnv = backendSpawnEnv(
    {PATH: 'C:\\Windows'},
    'C:\\VARIANT-1 Data',
    42,
    'launch-a',
    'C:\\VARIANT-1 Data\\runtimes\\playwright',
  );
  assert.strictEqual(spawnEnv.PATH, 'C:\\Windows');
  assert.strictEqual(spawnEnv.VARIANT1_DATA_DIR, 'C:\\VARIANT-1 Data');
  assert.strictEqual(spawnEnv.VARIANT1_UI_PID, '42');
  assert.strictEqual(spawnEnv.VARIANT1_BACKEND_INSTANCE_ID, 'launch-a');
  assert.strictEqual(spawnEnv.PYTHONUTF8, '1');
  assert.strictEqual(spawnEnv.PYTHONIOENCODING, 'utf-8');
  assert.strictEqual(
    spawnEnv.PLAYWRIGHT_BROWSERS_PATH,
    'C:\\VARIANT-1 Data\\runtimes\\playwright',
  );
}

function listen(server) {
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      server.removeListener('error', reject);
      resolve(server.address().port);
    });
  });
}

function closeServer(server) {
  return new Promise((resolve, reject) => {
    server.close((err) => err ? reject(err) : resolve());
  });
}

function healthServer(instanceId, isHealthy = () => true) {
  return http.createServer((_req, res) => {
    res.writeHead(200, {'content-type': 'application/json'});
    res.end(JSON.stringify({
      status: isHealthy() ? 'ok' : 'starting',
      version: VERSION,
      instance_id: instanceId,
    }));
  });
}

function writeRecord(portFile, record) {
  fs.writeFileSync(portFile, JSON.stringify(record), 'utf8');
}

function managerFor(portFile, overrides = {}) {
  const logs = overrides.logs || [];
  const killed = overrides.killed || [];
  const manager = createBackendManager({
    app: {isPackaged: false, getVersion: () => VERSION},
    appRoot: path.join(__dirname, '..'),
    getDataDir: () => path.dirname(portFile),
    log: (line) => logs.push(line),
    logStream: () => {},
    getStatusWindows: () => [],
    killProcessTree: (pid) => killed.push(pid),
    killVariant1EngineProcesses: () => {},
    requestGracefulShutdown: async () => false,
    timings: {
      cachedHealthTtlMs: 0,
      probeTimeoutMs: 50,
      healthTimeoutMs: 100,
      restartBaseMs: 10000,
      restartMaxMs: 10000,
      shutdownRequestTimeoutMs: 50,
      shutdownGraceMs: 50,
      shutdownPollMs: 1,
      ...overrides.timings,
    },
    ...overrides.deps,
  });
  manager.setPortFile(portFile);
  return {manager, logs, killed};
}

async function testMissingPackagedBackendNeverSpawnsPython(tempDir) {
  for (const withVenv of [false, true]) {
    const resourcesPath = path.join(tempDir, withVenv ? 'missing-exe-with-venv' : 'missing-exe-without-venv');
    const backendDir = path.join(resourcesPath, 'backend');
    fs.mkdirSync(backendDir, {recursive: true});
    fs.writeFileSync(path.join(backendDir, 'server.py'), '# fixture only; must never execute');
    if (withVenv) {
      const pythonPath = path.join(backendDir, '.venv', process.platform === 'win32' ? 'Scripts' : 'bin', process.platform === 'win32' ? 'python.exe' : 'python');
      fs.mkdirSync(path.dirname(pythonPath), {recursive: true});
      fs.writeFileSync(pythonPath, 'fixture only; must never execute');
    }
    const bundledExe = path.join(backendDir, process.platform === 'win32' ? 'Variant1Backend.exe' : 'Variant1Backend');
    assert.strictEqual(fs.existsSync(bundledExe), false);
    const spawns = [], subprocesses = [], timers = [], statuses = [];
    const result = managerFor(path.join(resourcesPath, 'port.json'), {deps: {
      app: {isPackaged: true, getVersion: () => VERSION}, resourcesPath, appRoot: resourcesPath,
      spawnProcess: (...args) => {spawns.push(args); throw new Error('Fixture forbids process launch');},
      execFileSync: (...args) => {subprocesses.push(args); return '';},
      killVariant1EngineProcesses: () => {},
      setTimer: (callback, delay) => {timers.push({callback, delay}); return timers.length;},
      clearTimer: () => {},
      getStatusWindows: () => [{isDestroyed: () => false, webContents: {send: (_channel, payload) => statuses.push(payload)}}],
    }});
    try {
      await result.manager.startBackend();
      assert.strictEqual(await result.manager.getInfo(), null);
      assert.ok(result.logs.some(line => line.includes('Packaged backend executable is missing: ' + bundledExe)
        && line.includes('Repair or reinstall VARIANT-1.')), 'corrupt packaged install reports a specific repair diagnostic');
      assert.ok(statuses.some(item => item.status === 'failed'), 'boot reports failed instead of an endless starting state');
      assert.deepStrictEqual(spawns, [], 'neither ambient nor bundled-venv Python may be spawned');
      assert.deepStrictEqual(subprocesses, [], 'the fixture must not execute helper processes');
      assert.deepStrictEqual(timers, [], 'missing executable must not schedule a respawn loop');
    } finally {
      await result.manager.stopBackend();
    }
    assert.deepStrictEqual(result.killed, [], 'no real process identity is touched');
  }
}

async function testAdoptedBackendLivenessAndQuitOwnership(tempDir) {
  const firstServer = healthServer('adopted-liveness');
  const firstPort = await listen(firstServer);
  const firstFile = path.join(tempDir, 'liveness.json');
  writeRecord(firstFile, {
    port: firstPort,
    token: 'live-token',
    pid: 41001,
    version: VERSION,
    instance_id: 'adopted-liveness',
  });
  const first = managerFor(firstFile);

  await first.manager.startBackend();
  assert.deepStrictEqual(await first.manager.getInfo(), {
    port: firstPort,
    token: 'live-token',
    pid: 41001,
  });

  await closeServer(firstServer);
  assert.strictEqual(await first.manager.getInfo(), null,
    'a dead adopted backend must be evicted from the renderer cache');
  assert.deepStrictEqual(first.killed, [41001],
    'a failed adopted backend must be terminated before replacement');
  assert.strictEqual(fs.existsSync(firstFile), false,
    'the stale identity file must not remain reconnectable');
  await first.manager.stopBackend();

  const secondServer = healthServer('adopted-quit');
  const secondPort = await listen(secondServer);
  const secondFile = path.join(tempDir, 'quit.json');
  writeRecord(secondFile, {
    port: secondPort,
    token: 'quit-token',
    pid: 41002,
    version: VERSION,
    instance_id: 'adopted-quit',
  });
  const second = managerFor(secondFile);
  await second.manager.startBackend();
  await second.manager.stopBackend();
  assert.deepStrictEqual(second.killed, [41002],
    'quit must terminate a health-verified backend even without a ChildProcess handle');
  assert.strictEqual(fs.existsSync(secondFile), false);
  await closeServer(secondServer);
}

async function testLiveBackendGetsHealthMissGraceBeforeTermination(tempDir) {
  let healthy = true;
  let clock = 1000;
  const instanceId = 'health-miss-grace';
  const server = healthServer(instanceId, () => healthy);
  const port = await listen(server);
  const portFile = path.join(tempDir, 'health-miss-grace.json');
  writeRecord(portFile, {
    port,
    token: 'grace-token',
    pid: 41101,
    version: VERSION,
    instance_id: instanceId,
  });
  const result = managerFor(portFile, {
    timings: {unhealthyGraceMs: 5000},
    deps: {
      now: () => clock,
      isProcessAlive: () => true,
    },
  });

  try {
    await result.manager.startBackend();
    healthy = false;
    clock += 1;

    assert.deepStrictEqual(await result.manager.getInfo(), {
      port,
      token: 'grace-token',
      pid: 41101,
    });
    assert.deepStrictEqual(result.killed, [],
      'one health miss must not kill a still-live backend');
    assert.strictEqual(fs.existsSync(portFile), true);

    clock += 5001;
    assert.strictEqual(await result.manager.getInfo(), null,
      'a continuously unhealthy backend is evicted after the grace window');
    assert.deepStrictEqual(result.killed, [41101]);
    assert.strictEqual(fs.existsSync(portFile), false);
  } finally {
    await result.manager.stopBackend();
    await closeServer(server);
  }
}

async function testAliveStartingBackendRecoversDuringStartupGrace(tempDir) {
  let healthy = false;
  const instanceId = 'startup-grace-recovery';
  const server = healthServer(instanceId, () => healthy);
  const port = await listen(server);
  const portFile = path.join(tempDir, 'startup-grace-recovery.json');
  writeRecord(portFile, {
    port,
    token: 'startup-grace-token',
    pid: 41151,
    version: VERSION,
    instance_id: instanceId,
  });
  let spawnCount = 0;
  const result = managerFor(portFile, {
    timings: {
      healthTimeoutMs: 5,
      healthRetryMs: 2,
      unhealthyGraceMs: 250,
    },
    deps: {
      isProcessAlive: () => true,
      spawnProcess: () => {
        spawnCount += 1;
        throw new Error('an alive backend must not be replaced during grace');
      },
    },
  });

  try {
    setTimeout(() => { healthy = true; }, 30);
    await result.manager.startBackend();
    assert.strictEqual(spawnCount, 0);
    assert.deepStrictEqual(await result.manager.getInfo(), {
      port,
      token: 'startup-grace-token',
      pid: 41151,
    });
    assert.deepStrictEqual(result.killed, [],
      'an exact live backend that becomes ready during grace must be adopted');
    assert.ok(result.logs.some((line) => line.includes('reusing') && line.includes('backend')),
      'the exact live backend may recover during either the initial or grace probe');
  } finally {
    await result.manager.stopBackend();
    await closeServer(server);
  }
}

async function testProbeFailureFallsBackToOs(tempDir) {
  let healthy=true,clock=1000;
  const instanceId='failed-process-probe',server=healthServer(instanceId,()=>healthy),port=await listen(server);
  const portFile=path.join(tempDir,'failed-process-probe.json');
  // Signal zero checks this process without modifying it. Termination is a test stub.
  writeRecord(portFile,{port,token:'test-probe',pid:process.pid,version:VERSION,instance_id:instanceId});
  const result=managerFor(portFile,{timings:{unhealthyGraceMs:20},deps:{now:()=>clock,isProcessAlive:()=>{throw new Error('probe unavailable');}}});
  try {
    await result.manager.startBackend();healthy=false;clock++;
    assert.ok(await result.manager.getInfo(),'OS-confirmed live process retains bounded grace');
    assert.ok(result.logs.some(line=>line.includes('checking OS liveness')),'probe exception is observable');
    clock+=21;assert.equal(await result.manager.getInfo(),null);
    assert.deepEqual(result.killed,[process.pid],'failed custom probe cannot block bounded unhealthy recovery');
  } finally {await result.manager.stopBackend();await closeServer(server);}
}


function testPosixEnginePathWithSpaces() {
  const spaced = '/opt/VARIANT-1 App/bin/llama-server';
  const exe = resolvePosixEngineExecutable(4242, 'ignored argv', undefined, {
    platform: 'linux',
    readlinkSync: (target) => {
      assert.strictEqual(target, '/proc/4242/exe');
      return spaced;
    },
  });
  assert.strictEqual(exe, spaced,
    'POSIX engine path must keep directories that contain spaces');
  assert.strictEqual(
    isOwnedPosixEnginePath(exe, ['/opt/VARIANT-1 App']),
    true,
  );
  assert.strictEqual(
    isOwnedPosixEnginePath(exe, ['/opt/other-root']),
    false,
  );
  const whisper = resolvePosixEngineExecutable(7, '', undefined, {
    platform: 'linux',
    readlinkSync: () => '/data/speech models/whisper-server',
  });
  assert.strictEqual(whisper, '/data/speech models/whisper-server');
}

function testPosixEngineIgnoresArgvLookalikes() {
  const enginePath = '/opt/VARIANT-1 App/bin/llama-server';
  const decoys = [
    `tail -f ${enginePath}`,
    `cat ${enginePath}`,
    `python3 -c "print('${enginePath}')"`,
  ];
  for (const command of decoys) {
    const exe = resolvePosixEngineExecutable(501, command, undefined, {
      platform: 'linux',
      readlinkSync: () => '/usr/bin/tail',
    });
    assert.strictEqual(exe, '',
      `argv lookalike must not select an engine: ${command}`);
  }
  // Even if argv names the engine path, unverified identity skips cleanup.
  const fromArgvOnly = resolvePosixEngineExecutable(502, enginePath + ' --port 1', undefined, {
    platform: 'linux',
    readlinkSync: () => { const err = new Error('ENOENT'); err.code = 'ENOENT'; throw err; },
  });
  assert.strictEqual(fromArgvOnly, '',
    'stale/missing /proc/pid/exe must not fall back to argv');
}

function testPosixEngineMultipleAndStalePids() {
  const map = {
    10: '/opt/app/bin/llama-server',
    11: '/opt/app/bin/whisper-server',
    12: '/opt/app/bin/llama-server',
  };
  const deps = {
    platform: 'linux',
    readlinkSync: (target) => {
      const pid = Number(String(target).split('/')[2]);
      if (!(pid in map)) {
        const err = new Error('ENOENT');
        err.code = 'ENOENT';
        throw err;
      }
      return map[pid];
    },
  };
  assert.strictEqual(resolvePosixEngineExecutable(10, '', undefined, deps), map[10]);
  assert.strictEqual(resolvePosixEngineExecutable(11, '', undefined, deps), map[11]);
  assert.strictEqual(resolvePosixEngineExecutable(12, '', undefined, deps), map[12]);
  assert.strictEqual(resolvePosixEngineExecutable(99, 'llama-server', undefined, deps), '');
  assert.strictEqual(resolvePosixEngineExecutable(0, '', undefined, deps), '');
  assert.strictEqual(
    resolvePosixEngineExecutable(10, '', undefined, {platform: 'win32'}),
    '',
    'non-linux platforms without a resolver must skip',
  );
}
function testPackagedEngineCleanupUsesOwnedRoots(tempDir) {
  if (process.platform !== 'win32') return;
  const calls = [];
  const resourcesPath = path.join(tempDir, 'packaged-resources');
  const dataDir = path.join(tempDir, 'user-data');
  const appRoot = path.join(tempDir, 'developer-root');
  const manager = createBackendManager({
    app: {isPackaged: true, getVersion: () => VERSION},
    appRoot,
    resourcesPath,
    getDataDir: () => dataDir,
    log: () => {},
    logStream: () => {},
    getStatusWindows: () => [],
    execFileSync: (command, args) => calls.push({command, args}),
  });

  manager.killVariant1EngineProcesses();
  const powershell = calls.find((call) => call.command === 'powershell.exe');
  assert.ok(powershell, 'Windows cleanup must execute the owned-process query');
  const script = powershell.args.at(-1);
  assert.match(script, new RegExp(resourcesPath.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')),
    'packaged resources must be an owned cleanup root');
  assert.match(script, new RegExp(
    path.join(dataDir, 'models', 'speech', 'whisper').replace(/[.*+?^${}()|[\]\\]/g, '\\$&'),
  ), 'the user-data speech runtime must be an owned cleanup root');
  assert.match(script, /\.Equals\(\$root,[\s\S]*\.StartsWith\(\$prefix/,
    'cleanup must use exact-root or subtree boundaries instead of a loose prefix');
}

async function testOverlayReceivesOnlyScopedActivityStatus(tempDir) {
  const instanceId = 'scoped-overlay-status';
  const server = healthServer(instanceId);
  const port = await listen(server);
  const portFile = path.join(tempDir, 'scoped-overlay-status.json');
  writeRecord(portFile, {
    port,
    token: 'main-deck-secret',
    activity_token: 'presence-only-secret',
    pid: 41501,
    version: VERSION,
    instance_id: instanceId,
  });
  const deckMessages = [];
  const overlayMessages = [];
  const windowFor = (messages) => ({
    isDestroyed: () => false,
    webContents: {send: (...args) => messages.push(args)},
  });
  const result = managerFor(portFile, {
    deps: {
      getStatusWindows: () => [windowFor(deckMessages)],
      getActivityWindows: () => [windowFor(overlayMessages)],
    },
  });
  try {
    await result.manager.startBackend();
    assert.deepStrictEqual(deckMessages.at(-1), ['backend:status', {
      status: 'ready',
      info: {
        port,
        token: 'main-deck-secret',
        activityToken: 'presence-only-secret',
        pid: 41501,
      },
    }]);
    assert.deepStrictEqual(overlayMessages.at(-1), ['activity:status', {
      status: 'ready',
      info: {port, activityToken: 'presence-only-secret'},
    }]);
    assert.doesNotMatch(JSON.stringify(overlayMessages), /main-deck-secret/,
      'the Main Deck bearer token must never cross the overlay IPC channel');
  } finally {
    await result.manager.stopBackend();
    await closeServer(server);
  }
}

async function testReadinessFailureSchedulesOnlyOneRestart(tempDir) {
  const portFile = path.join(tempDir, 'failed-readiness.json');
  const timerDelays = [];
  const children = new Map();
  let spawnCount = 0;
  const {manager} = managerFor(portFile, {
    timings: {
      portFileTimeoutMs: 5,
      portFilePollMs: 1,
      restartBaseMs: 100,
      restartMaxMs: 100,
    },
    deps: {
      spawnProcess: () => {
        spawnCount += 1;
        const child = new EventEmitter();
        child.pid = 42000 + spawnCount;
        child.stdout = new EventEmitter();
        child.stderr = new EventEmitter();
        children.set(child.pid, child);
        return child;
      },
      killProcessTree: (pid) => {
        const child = children.get(pid);
        if (child) queueMicrotask(() => child.emit('exit', 1, null));
      },
      setTimer: (fn, delay) => {
        timerDelays.push(delay);
        return setTimeout(fn, delay);
      },
      clearTimer: (timer) => clearTimeout(timer),
    },
  });

  await manager.startBackend();
  await new Promise((resolve) => setImmediate(resolve));

  assert.strictEqual(spawnCount, 1);
  assert.strictEqual(
    timerDelays.filter((delay) => delay === 100).length,
    1,
    'readiness catch and child exit must converge on one restart timer',
  );
  await manager.stopBackend();
}

async function testReadinessRestartWaitsForTreeTermination(tempDir) {
  const portFile = path.join(tempDir, 'failed-readiness-kill-fence.json');
  const timers = [];
  let spawnCount = 0;
  let releaseKill;
  const killGate = new Promise((resolve) => { releaseKill = resolve; });
  const {manager} = managerFor(portFile, {
    timings: {
      portFileTimeoutMs: 5,
      portFilePollMs: 1,
      restartBaseMs: 100,
      restartMaxMs: 100,
    },
    deps: {
      spawnProcess: () => {
        spawnCount += 1;
        const child = new EventEmitter();
        child.pid = 42500 + spawnCount;
        child.stdout = new EventEmitter();
        child.stderr = new EventEmitter();
        return child;
      },
      killProcessTree: async () => {
        await killGate;
      },
      setTimer: (fn, delay) => {
        const handle = setTimeout(fn, delay);
        const timer = {handle, delay, cleared: false};
        timers.push(timer);
        return timer;
      },
      clearTimer: (timer) => {
        if (!timer) return;
        timer.cleared = true;
        clearTimeout(timer.handle);
      },
    },
  });

  const starting = manager.startBackend();
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.strictEqual(spawnCount, 1);
  assert.strictEqual(
    timers.some((timer) => !timer.cleared && timer.delay === 100),
    false,
    'replacement restart must not be armed while tree termination is pending',
  );

  releaseKill();
  await starting;
  assert.strictEqual(
    timers.filter((timer) => !timer.cleared && timer.delay === 100).length,
    1,
    'one restart may be armed only after process-tree termination settles',
  );
  await manager.stopBackend();
}

async function testSpawnIdentityDoesNotAssumeLauncherPid(tempDir) {
  const instanceId = 'spawned-instance';
  const server = healthServer(instanceId);
  const port = await listen(server);
  const portFile = path.join(tempDir, 'spawned.json');
  let spawnEnv = null;
  const child = new EventEmitter();
  child.pid = 43001; // venv launcher
  child.stdout = new EventEmitter();
  child.stderr = new EventEmitter();
  const result = managerFor(portFile, {
    timings: {portFileTimeoutMs: 100, portFilePollMs: 1},
    deps: {
      createInstanceId: () => instanceId,
      spawnProcess: (_command, _args, options) => {
        spawnEnv = options.env;
        writeRecord(portFile, {
          port,
          token: 'spawn-token',
          pid: 43099, // real interpreter; intentionally not child.pid
          version: VERSION,
          instance_id: instanceId,
        });
        return child;
      },
    },
  });

  await result.manager.startBackend();

  assert.strictEqual(spawnEnv.VARIANT1_BACKEND_INSTANCE_ID, instanceId);
  assert.deepStrictEqual(await result.manager.getInfo(), {
    port,
    token: 'spawn-token',
    pid: 43099,
  });
  await result.manager.stopBackend();
  assert.deepStrictEqual(result.killed.sort(), [43001, 43099]);
  await closeServer(server);
}

async function testShutdownHttpContract() {
  const seen = {};
  const server = http.createServer((req, res) => {
    seen.method = req.method;
    seen.url = req.url;
    seen.authorization = req.headers.authorization;
    seen.contentLength = req.headers['content-length'];
    req.resume();
    res.writeHead(202, {'content-type': 'application/json'});
    res.end('{"ok":true}');
  });
  const port = await listen(server);
  try {
    assert.strictEqual(await requestBackendShutdown({port, token: 'shutdown-token'}, 250), true);
    assert.deepStrictEqual(seen, {
      method: 'POST',
      url: '/shutdown',
      authorization: 'Bearer shutdown-token',
      contentLength: '0',
    });
    assert.strictEqual(await requestBackendShutdown({port: 0, token: 'shutdown-token'}, 50), false);
    assert.strictEqual(await requestBackendShutdown({port, token: ''}, 50), false);
  } finally {
    await closeServer(server);
  }
}

async function testGracefulShutdownAndForceFallback(tempDir) {
  const gracefulServer = healthServer('graceful-quit');
  const gracefulPort = await listen(gracefulServer);
  const gracefulFile = path.join(tempDir, 'graceful-quit.json');
  const gracefulRecord = {
    port: gracefulPort,
    token: 'graceful-token',
    pid: 44001,
    version: VERSION,
    instance_id: 'graceful-quit',
  };
  writeRecord(gracefulFile, gracefulRecord);
  const gracefulSteps = [];
  const graceful = managerFor(gracefulFile, {
    deps: {
      requestGracefulShutdown: async (info) => {
        gracefulSteps.push('request');
        assert.strictEqual(info.token, gracefulRecord.token);
        return true;
      },
      waitForBackendExit: async () => {
        gracefulSteps.push('wait');
        return true;
      },
      killProcessTree: (pid) => gracefulSteps.push(`kill:${pid}`),
    },
  });
  await graceful.manager.startBackend();
  await graceful.manager.stopBackend();
  assert.deepStrictEqual(gracefulSteps, ['request', 'wait'],
    'a healthy backend must get a graceful request and bounded exit wait before any force kill');
  assert.strictEqual(fs.existsSync(gracefulFile), false);
  await closeServer(gracefulServer);

  const fallbackServer = healthServer('fallback-quit');
  const fallbackPort = await listen(fallbackServer);
  const fallbackFile = path.join(tempDir, 'fallback-quit.json');
  writeRecord(fallbackFile, {
    port: fallbackPort,
    token: 'fallback-token',
    pid: 44002,
    version: VERSION,
    instance_id: 'fallback-quit',
  });
  const fallbackSteps = [];
  const fallback = managerFor(fallbackFile, {
    deps: {
      requestGracefulShutdown: async () => {
        fallbackSteps.push('request');
        return true;
      },
      waitForBackendExit: async () => {
        fallbackSteps.push('wait');
        return false;
      },
      killProcessTree: (pid) => fallbackSteps.push(`kill:${pid}`),
    },
  });
  await fallback.manager.startBackend();
  await fallback.manager.stopBackend();
  assert.deepStrictEqual(fallbackSteps, ['request', 'wait', 'kill:44002'],
    'force kill must remain a fallback after the graceful wait expires');
  assert.strictEqual(fs.existsSync(fallbackFile), false);
  await closeServer(fallbackServer);
}

async function testQuitDuringReadinessUsesVerifiedPortRecord(tempDir) {
  const instanceId = 'readiness-quit';
  const server = healthServer(instanceId);
  const port = await listen(server);
  const portFile = path.join(tempDir, 'readiness-quit.json');
  const child = new EventEmitter();
  child.pid = 44501;
  child.stdout = new EventEmitter();
  child.stderr = new EventEmitter();
  const steps = [];
  const result = managerFor(portFile, {
    timings: {portFileTimeoutMs: 100, portFilePollMs: 1},
    deps: {
      createInstanceId: () => instanceId,
      spawnProcess: () => {
        writeRecord(portFile, {
          port,
          token: 'readiness-token',
          pid: 44599,
          version: VERSION,
          instance_id: instanceId,
        });
        return child;
      },
      requestGracefulShutdown: async (info) => {
        steps.push(`request:${info.pid}`);
        return true;
      },
      waitForBackendExit: async () => {
        steps.push('wait');
        return true;
      },
      killProcessTree: (pid) => steps.push(`kill:${pid}`),
    },
  });
  try {
    const starting = result.manager.startBackend();
    await result.manager.stopBackend();
    await starting;
    assert.deepStrictEqual(steps, ['request:44599', 'wait'],
      'quit must verify and gracefully stop a backend whose readiness adoption is in flight');
    assert.strictEqual(fs.existsSync(portFile), false);
  } finally {
    await result.manager.stopBackend();
    await closeServer(server);
  }
}

async function testRestartBudgetNeedsSustainedHealth(tempDir) {
  const instanceId = 'restart-stability';
  let stabilityHealthy = true;
  const server = healthServer(instanceId, () => stabilityHealthy);
  const port = await listen(server);
  const portFile = path.join(tempDir, 'restart-stability.json');
  const timers = [];
  const children = [];
  let spawnCount = 0;
  const result = managerFor(portFile, {
    timings: {
      portFileTimeoutMs: 100,
      portFilePollMs: 1,
      restartBaseMs: 100,
      restartMaxMs: 1000,
      restartResetMs: 5000,
    },
    deps: {
      createInstanceId: () => instanceId,
      spawnProcess: () => {
        spawnCount += 1;
        const child = new EventEmitter();
        child.pid = 45000 + spawnCount;
        child.stdout = new EventEmitter();
        child.stderr = new EventEmitter();
        children.push(child);
        writeRecord(portFile, {
          port,
          token: `restart-token-${spawnCount}`,
          pid: child.pid,
          version: VERSION,
          instance_id: instanceId,
        });
        return child;
      },
      setTimer: (fn, delay) => {
        const timer = {fn, delay, cleared: false, fired: false};
        timers.push(timer);
        return timer;
      },
      clearTimer: (timer) => { if (timer) timer.cleared = true; },
    },
  });

  try {
    await result.manager.startBackend();
    fs.unlinkSync(portFile);
    children[0].emit('exit', 1, null);
    const firstRestart = timers.find((timer) => !timer.cleared && timer.delay === 100);
    assert.ok(firstRestart, 'first crash must consume restart attempt #1');
    firstRestart.fired = true;
    firstRestart.fn();

    for (let i = 0; i < 50 && spawnCount < 2; i += 1) {
      await new Promise((resolve) => setImmediate(resolve));
    }
    for (let i = 0; i < 50
      && !timers.some((timer) => !timer.cleared && timer.delay === 5000); i += 1) {
      await new Promise((resolve) => setImmediate(resolve));
    }
    assert.strictEqual(spawnCount, 2);
    const stabilityReset = timers.find((timer) => !timer.cleared && timer.delay === 5000);
    assert.ok(stabilityReset, 'readiness must arm a delayed stability reset');

    stabilityHealthy = false;
    stabilityReset.fired = true;
    await stabilityReset.fn();
    const rearmedStabilityReset = timers.find((timer) => (
      timer !== stabilityReset && !timer.cleared && timer.delay === 5000
    ));
    assert.ok(rearmedStabilityReset,
      'a transient failed stability probe must re-arm one full reset interval');

    fs.unlinkSync(portFile);
    children[1].emit('exit', 1, null);
    assert.strictEqual(rearmedStabilityReset.cleared, true,
      'a pre-stability crash must cancel the re-armed budget reset');
    assert.ok(timers.some((timer) => !timer.cleared && timer.delay === 200),
      'a quick post-ready crash must back off as restart attempt #2');
  } finally {
    await result.manager.stopBackend();
    await closeServer(server);
  }
}

async function testBeforeQuitWaitsExactlyOnce() {
  let resolveStop;
  const stopGate = new Promise((resolve) => { resolveStop = resolve; });
  let stopCalls = 0;
  let quitCalls = 0;
  const handler = createBeforeQuitHandler({
    setQuitting: () => {},
    stopBackend: () => {
      stopCalls += 1;
      return stopGate;
    },
    unregisterShortcuts: () => {},
    quit: () => { quitCalls += 1; },
    log: () => {},
  });
  const first = {prevented: false, preventDefault() { this.prevented = true; }};
  const duplicate = {prevented: false, preventDefault() { this.prevented = true; }};
  handler(first);
  handler(duplicate);
  await Promise.resolve();
  assert.strictEqual(first.prevented, true);
  assert.strictEqual(duplicate.prevented, true);
  assert.strictEqual(stopCalls, 1, 'duplicate before-quit events must share one shutdown');
  assert.strictEqual(quitCalls, 0, 'app quit must wait for backend shutdown to settle');

  resolveStop();
  await new Promise((resolve) => setImmediate(resolve));
  assert.strictEqual(quitCalls, 1);
  const recursive = {prevented: false, preventDefault() { this.prevented = true; }};
  handler(recursive);
  assert.strictEqual(recursive.prevented, false,
    'the recursive app.quit event must pass after cleanup completes');
  assert.strictEqual(stopCalls, 1);
  assert.strictEqual(quitCalls, 1);
}

async function main() {
  assertIdentityContracts();
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'variant1-electron-backend-'));
  try {
    await testMissingPackagedBackendNeverSpawnsPython(tempDir);
    if (process.argv.includes('--packaged-missing-only')) {
      console.log('Packaged missing backend: explicit repair failure, zero Python/helper spawns, no retry timers passed');
      return;
    }
    await testShutdownHttpContract();
    await testAdoptedBackendLivenessAndQuitOwnership(tempDir);
    await testLiveBackendGetsHealthMissGraceBeforeTermination(tempDir);
    await testAliveStartingBackendRecoversDuringStartupGrace(tempDir);
    await testProbeFailureFallsBackToOs(tempDir);
    testPosixEnginePathWithSpaces();
    testPackagedEngineCleanupUsesOwnedRoots(tempDir);
    await testOverlayReceivesOnlyScopedActivityStatus(tempDir);
    await testReadinessFailureSchedulesOnlyOneRestart(tempDir);
    await testReadinessRestartWaitsForTreeTermination(tempDir);
    await testSpawnIdentityDoesNotAssumeLauncherPid(tempDir);
    await testGracefulShutdownAndForceFallback(tempDir);
    await testQuitDuringReadinessUsesVerifiedPortRecord(tempDir);
    await testRestartBudgetNeedsSustainedHealth(tempDir);
    await testBeforeQuitWaitsExactlyOnce();
  } finally {
    fs.rmSync(tempDir, {recursive: true, force: true});
  }
  console.log('electron backend identity and lifecycle tests passed');
}

main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
