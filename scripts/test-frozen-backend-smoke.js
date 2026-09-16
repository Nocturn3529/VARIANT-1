'use strict';

/**
 * Opt-in smoke test for the freshly frozen backend executable.
 *
 * Launches the packaged backend hidden, waits for its atomic port-file
 * handshake, verifies /health belongs to the same process instance, and then
 * terminates only the child started by this test.
 */
const assert = require('assert');
const fs = require('fs');
const http = require('http');
const os = require('os');
const path = require('path');
const {execFileSync, spawn} = require('child_process');
const {randomUUID} = require('crypto');

const root = path.join(__dirname, '..');
const executable = path.join(
  root,
  'backend',
  'dist',
  'Variant1Backend',
  process.platform === 'win32' ? 'Variant1Backend.exe' : 'Variant1Backend',
);
const archiveViewer = path.join(
  root,
  'backend',
  '.venv',
  'Scripts',
  process.platform === 'win32' ? 'pyi-archive_viewer.exe' : 'pyi-archive_viewer',
);
const scratch = path.join(os.tmpdir(), `variant1-frozen-smoke-${randomUUID()}`);
const portFile = path.join(scratch, 'backend.json');
const smokeData = path.join(scratch, 'data');
const mcpAudit = path.join(scratch, 'mcp-audit.jsonl');
const mcpFixture = path.join(root, 'experiments', 'live-canary', 'fixture_mcp_server.py');
const backendPython = path.join(
  root, 'backend', '.venv', 'Scripts',
  process.platform === 'win32' ? 'python.exe' : 'python',
);
const deadline = Date.now() + 60000;
let output = '';

fs.mkdirSync(scratch, {recursive: false});
assert.ok(fs.existsSync(executable), `missing frozen backend: ${executable}`);
assert.ok(fs.existsSync(archiveViewer), `missing PyInstaller archive viewer: ${archiveViewer}`);

const archive = execFileSync(
  archiveViewer,
  ['-r', '-b', executable],
  {cwd: root, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024},
);
const hasModule = name => new RegExp(
  `^ ${name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`,
  'm',
).test(archive);
for (const required of [
  'fastapi', 'uvicorn', 'httpx', 'websockets', 'anyio._backends._asyncio',
  'docx', 'pptx', 'openpyxl', 'reportlab.pdfgen.canvas', 'pypdf',
  'lxml.html', 'xlsxwriter', 'mcp.client.session', 'mcp.client.stdio',
  'mcp.client.streamable_http', 'mcp.client.sse',
  'browser_fabric.provisioning', 'speech.assets',
  process.platform === 'win32' ? 'mss.windows' : 'mss.linux',
]) {
  assert.ok(hasModule(required), `frozen backend is missing runtime module ${required}`);
}
for (const forbidden of [
  'kokoro_onnx', 'phonemizer', 'espeakng_loader', 'onnxruntime', 'neutts', 'kittentts', 'piper', 'soundfile',
  'tokenizers', 'fastapi.testclient', 'anyio.pytest_plugin',
  'mss.__main__', 'openpyxl.utils.dataframe', 'reportlab.graphics.samples',
  'reportlab.graphics.barcode.test', 'reportlab.lib.testutils',
  'mcp.cli', 'mcp.client.__main__', 'mcp.client.auth',
  'mcp.client.websocket', 'mcp.server.__main__',
  'mcp.server.websocket', 'mcp.shared.memory',
]) {
  assert.ok(!hasModule(forbidden), `frozen backend retained optional module ${forbidden}`);
}
// The official MCP package initializer re-exports FastMCP/server APIs before
// Python resolves any mcp.client.* submodule. Those upstream-coupled modules
// stay until the SDK offers a client-only root; VARIANT-1 does not collect their
// independent CLI, client-auth/WebSocket, server-main/WebSocket, or memory demo.
fs.mkdirSync(path.join(smokeData, 'config'), {recursive: true});

const frozenInternal = path.join(path.dirname(executable), '_internal');
for (const optional of ['kokoro_onnx', 'phonemizer', 'espeakng_loader', 'onnxruntime']) {
  assert.ok(!fs.existsSync(path.join(frozenInternal, optional)), `speech runtime leaked into baseline: ${optional}`);
}
let speechServer = null;

const child = spawn(executable, ['--port-file', portFile, '--port', '0'], {
  cwd: path.join(root, 'backend'),
  env: {
    ...process.env,
    VARIANT1_DATA_DIR: smokeData,
    PYTHONUTF8: '1',
    PYTHONIOENCODING: 'utf-8',
  },
  stdio: ['ignore', 'pipe', 'pipe'],
  windowsHide: true,
});
child.stdout.on('data', chunk => { output += chunk.toString(); });
child.stderr.on('data', chunk => { output += chunk.toString(); });

function delay(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function getHealth(port) {
  return new Promise((resolve, reject) => {
    const request = http.get({
      host: '127.0.0.1',
      port,
      path: '/health',
      timeout: 3000,
    }, response => {
      let body = '';
      response.on('data', chunk => { body += chunk; });
      response.on('end', () => {
        try { resolve(JSON.parse(body)); }
        catch (error) { reject(error); }
      });
    });
    request.on('error', reject);
    request.on('timeout', () => request.destroy(new Error('health request timeout')));
  });
}

function openWebSocket(port, token) {
  return new Promise((resolve, reject) => {
    const socket = new WebSocket(
      `ws://127.0.0.1:${port}/ws?token=${encodeURIComponent(token)}`,
    );
    const timer = setTimeout(() => {
      socket.close();
      reject(new Error('timed out opening frozen backend WebSocket'));
    }, 10000);
    socket.addEventListener('open', () => {
      clearTimeout(timer);
      resolve(socket);
    }, {once: true});
    socket.addEventListener('error', event => {
      clearTimeout(timer);
      reject(event.error || new Error('frozen backend WebSocket failed'));
    }, {once: true});
  });
}

function websocketRequest(socket, payload, timeoutMs = 45000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      cleanup();
      reject(new Error(`timed out waiting for ${payload.request_id}`));
    }, timeoutMs);
    const onMessage = event => {
      try {
        const raw = typeof event.data === 'string'
          ? event.data
          : Buffer.from(event.data).toString('utf8');
        const message = JSON.parse(raw);
        if (message.request_id !== payload.request_id) return;
        cleanup();
        resolve(message);
      } catch (_) {
        // Ignore unrelated/non-JSON backend traffic.
      }
    };
    const cleanup = () => {
      clearTimeout(timer);
      socket.removeEventListener('message', onMessage);
    };
    socket.addEventListener('message', onMessage);
    socket.send(JSON.stringify(payload));
  });
}

async function waitForPortRecord() {
  while (Date.now() < deadline) {
    if (child.exitCode != null) {
      throw new Error(`frozen backend exited early (${child.exitCode})\n${output.slice(-4000)}`);
    }
    try {
      const record = JSON.parse(fs.readFileSync(portFile, 'utf8'));
      if (Number(record.port) > 0) return record;
    } catch {
      // The backend has not completed its atomic startup handshake yet.
    }
    await delay(250);
  }
  throw new Error(`timed out waiting for frozen backend\n${output.slice(-4000)}`);
}

async function waitForHealth(record) {
  while (Date.now() < deadline) {
    if (child.exitCode != null) {
      throw new Error(`frozen backend exited early (${child.exitCode})\n${output.slice(-4000)}`);
    }
    try {
      const health = await getHealth(Number(record.port));
      if (health && health.status === 'ok' && health.ready === true) return health;
    } catch {
      // Uvicorn publishes the identity record before the readiness-gated
      // lifespan completes, so connection refusal is transient here.
    }
    await delay(250);
  }
  throw new Error(`timed out waiting for frozen backend health\n${output.slice(-4000)}`);
}

(async () => {
  try {
    const record = await waitForPortRecord();
    const health = await waitForHealth(record);
    assert.strictEqual(Number(record.pid), child.pid, 'port-file PID must match the spawned child');
    assert.strictEqual(health.status, 'ok');
    assert.strictEqual(health.ready, true);
    assert.strictEqual(health.version, record.version);
    assert.strictEqual(health.instance_id, record.instance_id);
    const socket = await openWebSocket(record.port, record.token);
    const serverId = `frozen-smoke-${randomUUID()}`;
    try {
      const connected = await websocketRequest(socket, {
        type: 'mcp-v2:server',
        request_id: `connect-${serverId}`,
        action: 'connect',
        server_id: serverId,
        spec: {
          transport: 'stdio',
          command: [backendPython, mcpFixture],
          env: {
            VARIANT1_ASTB_MCP_RECORDS: JSON.stringify({frozen_record: 'frozen-mcp-ready'}),
            VARIANT1_ASTB_MCP_AUDIT: mcpAudit,
            PYTHONUTF8: '1',
            PYTHONIOENCODING: 'utf-8',
          },
        },
      });
      assert.strictEqual(connected.type, 'extension-v2:accepted', JSON.stringify(connected));

      const catalog = await websocketRequest(socket, {
        type: 'mcp-v2:catalog',
        request_id: `catalog-${serverId}`,
        server_id: serverId,
        kind: 'tool',
        query: 'compose_record',
      });
      assert.strictEqual(catalog.type, 'extension-v2:accepted', JSON.stringify(catalog));
      const item = (catalog.result || []).find(row => row.name === 'compose_record');
      assert.ok(item && item.lease, `frozen MCP catalog is incomplete: ${JSON.stringify(catalog)}`);

      const invoked = await websocketRequest(socket, {
        type: 'mcp-v2:invoke',
        request_id: `invoke-${serverId}`,
        mcp_request_id: `effect-${serverId}`,
        operation: 'call',
        lease: item.lease,
        arguments: {record_id: 'frozen_record'},
      });
      assert.strictEqual(invoked.type, 'extension-v2:accepted', JSON.stringify(invoked));
      assert.match(JSON.stringify(invoked.result), /frozen-mcp-ready/);
      assert.match(fs.readFileSync(mcpAudit, 'utf8'), /"event": "compose_record"/);

      const removed = await websocketRequest(socket, {
        type: 'mcp-v2:server',
        request_id: `remove-${serverId}`,
        action: 'remove',
        server_id: serverId,
      });
      assert.strictEqual(removed.type, 'extension-v2:accepted', JSON.stringify(removed));

      const missing = await websocketRequest(socket, {
        type: 'tts:preview', request_id: `missing-${randomUUID()}`, text: 'Optional speech check.'});
      assert.ok(missing.error && !missing.audio, 'baseline must report unconfigured speech, not fake audio');
      const fixtureWav = Buffer.alloc(46);
      fixtureWav.write('RIFF'); fixtureWav.writeUInt32LE(38, 4); fixtureWav.write('WAVEfmt ', 8);
      fixtureWav.writeUInt32LE(16, 16); fixtureWav.writeUInt16LE(1, 20); fixtureWav.writeUInt16LE(1, 22);
      fixtureWav.writeUInt32LE(24000, 24); fixtureWav.writeUInt32LE(48000, 28);
      fixtureWav.writeUInt16LE(2, 32); fixtureWav.writeUInt16LE(16, 34); fixtureWav.write('data', 36);
      fixtureWav.writeUInt32LE(2, 40);
      let externalCalls = 0;
      speechServer = http.createServer((request, response) => {
        const chunks = [];
        request.on('data', chunk => chunks.push(chunk));
        request.on('end', () => {
          if (request.method === 'GET' && request.url === '/v1/audio/voices') {
            response.writeHead(200, {'Content-Type': 'application/json'});
            response.end(JSON.stringify({voices: ['af_nova']})); return;
          }
          if (request.url !== '/v1/audio/speech') {response.writeHead(404); response.end(); return;}
          const payload = JSON.parse(Buffer.concat(chunks).toString());
          assert.strictEqual(payload.model, 'kokoro'); assert.strictEqual(payload.response_format, 'wav');
          externalCalls++; response.writeHead(200, {'Content-Type': 'audio/wav'}); response.end(fixtureWav);
        });
      });
      await new Promise(resolve => speechServer.listen(0, '127.0.0.1', resolve));
      const configured = await websocketRequest(socket, {type: 'tts:set', request_id: `speech-config-${randomUUID()}`,
        key: 'tts_options', value: {provider: 'kokoro', fields: {model: 'kokoro',
          base_url: `http://127.0.0.1:${speechServer.address().port}/v1`}}});
      assert.strictEqual(configured.type, 'speech:accepted', JSON.stringify(configured));
      const voiceRequestId = `voice-${randomUUID()}`;
      const preview = await websocketRequest(socket, {
        type: 'tts:preview',
        request_id: voiceRequestId,
        purpose: 'frozen-smoke',
        text: 'VARIANT-1 local voice is ready.',
        voice: 'af_nova',
      }, 120000);
      assert.strictEqual(preview.type, 'tts:preview', JSON.stringify(preview));
      assert.ok(!preview.error, `frozen local TTS failed: ${preview.error}`);
      const wav = Buffer.from(preview.audio || '', 'base64');
      assert.ok(wav.length > 44, 'frozen local TTS returned an empty WAV');
      assert.strictEqual(wav.subarray(0, 4).toString('ascii'), 'RIFF');
      assert.strictEqual(wav.subarray(8, 12).toString('ascii'), 'WAVE');
      assert.strictEqual(wav.readUInt32LE(24), 24000, 'unexpected speech fixture sample rate');
      assert.strictEqual(externalCalls, 1, 'configured speech must use the external server exactly once');
    } finally {
      socket.close();
    }
    console.log(
      `frozen backend smoke: status=${health.status}, ready=${health.ready}, ` +
      `version=${health.version}, identity=matched, mcp=stdio-ok, optional-speech=missing-ok/external-http-fixture-ok`,
    );
  } finally {
    if (speechServer) {speechServer.closeAllConnections(); await new Promise(resolve => speechServer.close(resolve));}
    if (child.exitCode == null) child.kill();
    await Promise.race([
      new Promise(resolve => child.once('exit', resolve)),
      delay(5000),
    ]);
    fs.rmSync(scratch, {recursive: true, force: true});
  }
})().catch(error => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
