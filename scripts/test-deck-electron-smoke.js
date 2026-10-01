'use strict';

/** Real Electron smoke for the one-socket Deck and Hermes-style workbench. */
const assert = require('assert');
const fs = require('fs');
const http = require('http');
const os = require('os');
const path = require('path');
const {spawn} = require('child_process');

const root = path.join(__dirname, '..');
const executableOverride = String(process.env.VARIANT1_E2E_EXECUTABLE || '').trim();
const electron = executableOverride || require('electron');
// The workbench deliberately persists its layout. Every smoke run therefore
// needs an isolated profile even when it launches the development Electron
// binary, otherwise a developer's previous pane state can invert toggle tests.
const isolatedAppData = fs.mkdtempSync(path.join(os.tmpdir(), 'variant1-e2e-'));
const port = Number(process.env.VARIANT1_E2E_CDP_PORT || 9339);
const deadline = Date.now() + 90000;
let output = '';

const args = [
  // Native window tests intentionally cover each other during transfer. Keep
  // compositor frames available to capture checks in that test environment.
  '--disable-backgrounding-occluded-windows',
  `--remote-debugging-port=${port}`,
  `--user-data-dir=${path.join(isolatedAppData, 'Chromium')}`,
];
if (!executableOverride) args.push(root);
const env = {...process.env, VARIANT1_E2E_SMOKE: '1'};
env.APPDATA = path.join(isolatedAppData, 'Roaming');
env.LOCALAPPDATA = path.join(isolatedAppData, 'Local');
fs.mkdirSync(env.APPDATA, {recursive: true});
fs.mkdirSync(env.LOCALAPPDATA, {recursive: true});
env.VARIANT1_E2E_DATA_DIR = path.join(isolatedAppData, 'VariantData');
const child = spawn(electron, args, {cwd: root, env, stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true});
child.stdout.on('data', chunk => { output += chunk.toString(); });
child.stderr.on('data', chunk => { output += chunk.toString(); });

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

function getJson(urlPath) {
  return new Promise((resolve, reject) => {
    const request = http.get({host: '127.0.0.1', port, path: urlPath, timeout: 1000}, response => {
      let body = '';
      response.on('data', chunk => { body += chunk; });
      response.on('end', () => { try { resolve(JSON.parse(body)); } catch (error) { reject(error); } });
    });
    request.on('error', reject);
    request.on('timeout', () => request.destroy(new Error('CDP timeout')));
  });
}

async function waitForTarget() {
  while (Date.now() < deadline) {
    if (child.exitCode != null) throw new Error(`Electron exited early (${child.exitCode})\n${output.slice(-5000)}`);
    try {
      const targets = await getJson('/json/list');
      const deck = targets.find(target => String(target.url || '').includes('/frontend/main-deck/index.html'));
      if (deck?.webSocketDebuggerUrl) return deck;
    } catch { /* DevTools not ready. */ }
    await delay(200);
  }
  throw new Error(`Timed out waiting for Main Deck\n${output.slice(-5000)}`);
}

class CdpClient {
  constructor(url) {
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = new Map();
    this.socket = new WebSocket(url);
    this.socket.onclose = () => {
      for (const pending of this.pending.values()) { clearTimeout(pending.timer); pending.reject(new Error('CDP target closed')); }
      this.pending.clear();
    };
    this.socket.onmessage = event => {
      const message = JSON.parse(String(event.data));
      if (!message.id) {
        for (const listener of this.listeners.get(message.method) || []) listener(message.params || {});
        return;
      }
      const pending = this.pending.get(message.id);
      if (!pending) return;
      this.pending.delete(message.id);
      clearTimeout(pending.timer);
      if (message.error) pending.reject(new Error(message.error.message));
      else pending.resolve(message.result);
    };
  }
  on(method, listener) {
    this.listeners.set(method, [...(this.listeners.get(method) || []), listener]);
  }
  async open() {
    if (this.socket.readyState === WebSocket.OPEN) return;
    await new Promise((resolve, reject) => {
      this.socket.onopen = resolve;
      this.socket.onerror = () => reject(new Error('CDP WebSocket failed'));
    });
  }
  call(method, params = {}) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error(`CDP ${method} timed out`)); }, 30000);
      this.pending.set(id, {resolve, reject, timer});
      this.socket.send(JSON.stringify({id, method, params}));
    });
  }
  async evaluate(expression) {
    const result = await this.call('Runtime.evaluate', {expression, awaitPromise: true, returnByValue: true});
    if (result.exceptionDetails) {
      const detail = result.exceptionDetails.exception?.description || result.exceptionDetails.text || 'Runtime evaluation failed';
      throw new Error(detail);
    }
    return result.result?.value;
  }
  close() { this.socket.close(); }
}

async function waitFor(client, expression, label, timeoutMs = 15000) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    if (await client.evaluate(`Boolean(${expression})`)) return;
    await delay(80);
  }
  let state;
  try {state=await client.evaluate(`(() => ({
    url:location.href,readyState:document.readyState,viewport:{width:innerWidth,height:innerHeight},
    backend:document.body?.dataset.backendState || null,browserHost:document.body?.dataset.browserHost || null,
    startup:document.body?.dataset.startup || null,startupText:document.querySelector('.startup-cover')?.innerText?.slice(0,4000) || null,
    sockets:document.body?.dataset.deckLiveSockets || null,maxSockets:document.body?.dataset.deckMaxConcurrentSockets || null,
    roots:document.querySelectorAll('#variant1-react-root').length,workbenches:document.querySelectorAll('.workbench').length,
    chatWorkspaces:document.querySelectorAll('.workbench .chat-workspace').length,
    displayedChat:!!document.querySelector('.history-item__select[aria-current="true"]'),
    frames:[...document.querySelectorAll('iframe')].slice(0,10).map(frame=>({src:frame.getAttribute('src'),visible:!!frame.getClientRects().length})),
  }))()`);}catch(error){state={diagnosticError:error.message};}
  throw new Error(`Timed out waiting for ${label}\nReadiness state: ${JSON.stringify(state)}\n${output.slice(-3000)}`);
}

async function clickWhen(client, selector, label, timeoutMs = 15000) {
  const encoded = JSON.stringify(selector);
  await client.evaluate(`new Promise((resolve, reject) => {
    const deadline = Date.now() + ${timeoutMs};
    const poll = () => {
      const control = document.querySelector(${encoded});
      if (control) { control.click(); resolve(true); return; }
      if (Date.now() >= deadline) { reject(new Error(${JSON.stringify(`Timed out waiting for ${label}`)})); return; }
      setTimeout(poll, 40);
    };
    poll();
  })`);
}

async function waitForExit(ms = 15000) {
  if (child.exitCode != null) return;
  await Promise.race([new Promise(resolve => child.once('exit', resolve)), delay(ms)]);
  if (child.exitCode == null) child.kill();
}

async function removeIsolatedProfile() {
  const target = path.resolve(isolatedAppData);
  const tempRoot = `${path.resolve(os.tmpdir())}${path.sep}`;
  if (!target.startsWith(tempRoot) || !path.basename(target).startsWith('variant1-e2e-')) {
    throw new Error(`Refusing to remove unexpected smoke profile: ${target}`);
  }
  for (let attempt = 0; attempt < 12; attempt += 1) {
    try {
      fs.rmSync(target, {recursive: true, force: true, maxRetries: 4, retryDelay: 100});
      return;
    } catch (error) {
      if (attempt === 11) {
        console.warn(`Smoke profile cleanup deferred: ${error.message}`);
        return;
      }
      await delay(250);
    }
  }
}

(async () => {
  let client;
  const rendererErrors = [];
  const websocketFrames = [];
  try {
    const target = await waitForTarget();
    client = new CdpClient(target.webSocketDebuggerUrl);
    await client.open();
    client.on('Runtime.exceptionThrown', params => rendererErrors.push(
      params?.exceptionDetails?.exception?.description || params?.exceptionDetails?.text || 'unknown renderer exception',
    ));
    client.on('Network.webSocketFrameSent', params => {
      const payload = String(params?.response?.payloadData || '');
      if (payload) websocketFrames.push(`${new Date().toISOString()} > ${payload.slice(0, 1200)}`);
    });
    client.on('Network.webSocketFrameReceived', params => {
      const payload = String(params?.response?.payloadData || '');
      if (payload) websocketFrames.push(`${new Date().toISOString()} < ${payload.slice(0, 1200)}`);
    });
    await client.call('Runtime.enable');
    await client.call('Network.enable');
    // Pin the viewport before asserting any layout.
    //
    // The workbench goes compact at DECK_BREAKPOINT.narrow (830px) and hides
    // every side pane when it does, so `.workbench-files` stops being rendered
    // at all. Without this, the assertions below silently depended on whatever
    // default window size the runner happened to produce: it passed on
    // windows-latest and ubuntu-latest, then failed on macos-latest with
    // "Files must be a standing live pane" because that window came up narrow.
    // Setting the metrics makes the layout deterministic on every platform
    // instead of accidentally correct on some of them.
    await client.call('Emulation.setDeviceMetricsOverride', {
      width: 1440, height: 900, deviceScaleFactor: 1, mobile: false,
    });
    await waitFor(client,
      `window.innerWidth > 830`, 'wide workbench viewport', 15000);
    await waitFor(client,
      `document.body.dataset.backendState === 'connected' && document.body.dataset.browserHost === 'registered' && document.querySelector('.workbench .chat-workspace')`,
      'connected workbench', 45000);

    const initial = await client.evaluate(`(() => ({
      roots: document.querySelectorAll('#variant1-react-root').length,
      workbenches: document.querySelectorAll('.workbench').length,
      workspace: document.querySelectorAll('.workbench .chat-workspace').length,
      files: document.querySelectorAll('.workbench-files').length,
      oldPanel: document.querySelectorAll('.context-panel, .browser-fabric-destination').length,
      sockets: Number(document.body.dataset.deckLiveSockets || 0),
      maxSockets: Number(document.body.dataset.deckMaxConcurrentSockets || 0),
      groups: document.querySelectorAll('.workbench-group').length,
      sashes: document.querySelectorAll('.workbench-sash').length,
    }))()`);
    assert.deepStrictEqual({roots: initial.roots, workbenches: initial.workbenches, workspace: initial.workspace}, {roots: 1, workbenches: 1, workspace: 1});
    assert.strictEqual(initial.oldPanel, 0);
    assert.strictEqual(initial.sockets, 1);
    assert.strictEqual(initial.maxSockets, 1);
    // The workbench is a resizable multi-pane split. Assert that intent rather
    // than a pane count: the layout collapses its right-hand region at rest, so
    // a fixed group/sash count tracked a design detail and broke whenever the
    // default layout changed.
    assert.ok(initial.groups >= 2 && initial.sashes >= 1,
      `workbench must be a resizable split, saw ${initial.groups} groups and ${initial.sashes} sashes`);

    // The split workbench can render before session hydration and its
    // chat-ownership effect complete. Sending the one-shot shortcut at that
    // point races the listener and can leave Files closed for the entire test.
    // Wait for a displayed chat and its adopted Files layout before exercising
    // the real Ctrl/Cmd+J shortcut. Files is intentionally collapsed at rest.
    await waitFor(client, `document.querySelector('.history-item__select[aria-current="true"]')
      && window.localStorage.getItem('variant1.workbench.layout.v1')?.includes('owned:files:')`,
      'chat-owned Files layout', 45000);
    if (!(await client.evaluate(`!!document.querySelector('.workbench-group[data-group-id="group-files"]')`))) {
      const filesKey = {modifiers: process.platform === 'darwin' ? 4 : 2, key: 'j', code: 'KeyJ', windowsVirtualKeyCode: 74, nativeVirtualKeyCode: 74};
      await client.call('Input.dispatchKeyEvent', {type: 'rawKeyDown', ...filesKey});
      await client.call('Input.dispatchKeyEvent', {type: 'keyUp', ...filesKey});
    }
    await waitFor(client, `document.querySelector('.workbench-group[data-group-id="group-files"] .workbench-pane-layer')`,
      'Files pane group', 20000);
    // The pane renders either the file tree or, when the chat has no project
    // folder, the "No project selected" picker. Both are correct states; a fresh
    // profile has no project, and pickFolder is a native dialog the smoke cannot
    // drive, so requiring the tree here would only encode that gap.
    const filesPane = await client.evaluate(`(() => {
      const group=document.querySelector('.workbench-group[data-group-id="group-files"]');
      return {tree:!!group.querySelector('.workbench-files'),
              picker:!!group.querySelector('.workbench-preview__state'),
              text:(group.textContent||'').trim().slice(0,60)};
    })()`);
    assert.ok(filesPane.tree || filesPane.picker,
      `Files pane rendered neither a tree nor the project picker: ${filesPane.text}`);
    // Rows only exist when there is a real project to list.
    if (filesPane.tree) {
      await waitFor(client, `document.querySelectorAll('.workbench-file-row').length > 0`, 'file tree rows');
    }

    // The workbench-root and directory-read checks go through the preload IPC
    // bridge, so they hold regardless of whether the Files pane renders.
    const rootResult = await client.evaluate(`window.variant1Deck.getWorkbenchRoot()`);
    assert.strictEqual(rootResult?.ok, true, rootResult?.error || 'workbench root failed');
    const dirResult = await client.evaluate(`window.variant1Deck.readWorkbenchDirectory(${JSON.stringify(root)})`);
    assert.strictEqual(dirResult?.ok, true, dirResult?.error || 'directory read failed');
    assert.ok(dirResult.entries.some(row => row.name === 'package.json'));

      await client.evaluate(`(() => {
        if (!document.querySelector('.workbench-browser__guest')) {
          document.querySelector('.chat-workbar__button[aria-label="Browser"]').click();
        }
        return true;
      })()`);
      // The page is a native view owned by the main process; the renderer only
      // keeps a geometry shim (retainedBrowserView.ts creates a div and hands
      // bounds/viewport to workbenchBrowser({action:"attach"})). There is
      // deliberately no <webview> element in the DOM, so assert the shim and
      // then data-browser-ready, which registerWorkbenchBrowser sets once the
      // main process confirms the attach and dom-ready has landed.
      try {
        await waitFor(client, `document.querySelector('.workbench-browser__host > .workbench-browser__guest')`,
          'browser guest shim', 20000);
        await waitFor(client, `document.querySelector('.workbench-browser__guest[data-browser-ready="true"]')`,
          'browser page ready', 30000);
      } catch (error) {
        const diagnostic = await client.evaluate(`JSON.stringify({
          previews: localStorage.getItem('variant1.workbench.preview-tabs.v1'),
          layout: localStorage.getItem('variant1.workbench.layout.v1'),
          labels: [...document.querySelectorAll('.workbench-tab')].map(row => row.textContent),
          browserSurface: !!document.querySelector('.workbench-browser'),
          guestCount: document.querySelectorAll('.workbench-browser__guest').length,
          guests: [...document.querySelectorAll('.workbench-browser__guest')].map(g => ({
            tag: g.tagName,
            ready: g.dataset ? g.dataset.browserReady ?? null : null,
            generation: g.dataset ? g.dataset.browserGeneration ?? null : null,
            document: g.dataset ? g.dataset.browserDocument ?? null : null,
            retained: g.dataset ? g.dataset.retainedBrowser ?? null : null,
            connected: g.isConnected,
            inHost: !!g.closest('.workbench-browser__host'),
            rect: (() => { const r=g.getBoundingClientRect(); return {w:Math.round(r.width),h:Math.round(r.height)}; })(),
          })),
          groups: [...document.querySelectorAll('.workbench-group')].map(g => ({
            id: g.dataset.groupId,
            tabs: [...g.querySelectorAll('[data-pane-tab]')].map(t => t.getAttribute('data-pane-tab')),
            content: ((g.querySelector('.workbench-group__content')||{}).innerHTML||'').slice(0,200),
          })),
        }, null, 1)`);
      throw new Error(`${error.message}\nBrowser state: ${diagnostic}\nRenderer errors:\n${rendererErrors.join('\n')}`);
    }
    let browser = await client.evaluate(`(() => ({
      bars: document.querySelectorAll('.workbench-browser__bar').length,
      guests: document.querySelectorAll('.workbench-browser__guest').length,
      tabs: [...document.querySelectorAll('.workbench-tab')].filter(row => /Browser/.test(row.textContent)).length,
      fabricAdmin: !!document.querySelector('.browser-fabric-destination'),
    }))()`);
    assert.ok(browser.bars >= 1 && browser.guests >= 1 && browser.tabs >= 1,
      `browser workbench disappeared: ${JSON.stringify(browser)}\nrenderer errors:\n${rendererErrors.join('\n')}`);
    assert.strictEqual(browser.fabricAdmin, false);

    await clickWhen(client, '.workbench-browser__bar [aria-label="New browser tab"], .workbench-tabs__new[aria-label="New Browser"]', 'new browser tab control');
    try {
      await waitFor(client, `[...document.querySelectorAll('.workbench-tab')].filter(row => /Browser/.test(row.textContent)).length >= 2`, 'second preview tab');
    } catch (error) {
      const diagnostic = await client.evaluate(`JSON.stringify({
        labels: [...document.querySelectorAll('.workbench-tab')].map(row => row.textContent),
        previews: localStorage.getItem('variant1.workbench.preview-tabs.v1'),
        layout: localStorage.getItem('variant1.workbench.layout.v1'),
      })`);
      throw new Error(`${error.message}\nBrowser state: ${diagnostic}\nRenderer errors:\n${rendererErrors.join('\n')}`);
    }
    browser = await client.evaluate(`(() => ({
      browserTabs: [...document.querySelectorAll('.workbench-tab')].filter(row => /Browser/.test(row.textContent)).length,
      browserGuests: document.querySelectorAll('.workbench-browser__guest').length,
      previewGroups: [...document.querySelectorAll('.workbench-group')].filter(group => group.querySelector('.workbench-browser')).length,
    }))()`);
    assert.ok(browser.browserTabs >= 2);
    assert.ok(browser.browserGuests >= 2, 'inactive browser guests must remain mounted');
    assert.strictEqual(browser.previewGroups, 1, 'subsequent previews must stack into one zone');

    await client.evaluate(`(() => {
      if (!document.querySelector('.workbench-terminal-pane')?.getClientRects().length) {
        document.querySelector('.chat-workbar__button[aria-label="Terminal"]').click();
      }
      return true;
    })()`);
    try {
      await waitFor(client, `document.querySelector('.workbench-terminal-pane')?.getClientRects().length > 0`, 'visible terminal pane');
    } catch (error) {
      const diagnostic = await client.evaluate(`JSON.stringify({
        layout: localStorage.getItem('variant1.workbench.layout.v1'),
        hidden: localStorage.getItem('variant1.workbench.hidden.v1'),
        control: document.querySelector('.chat-workbar__button[aria-label="Terminal"]')?.outerHTML || '',
        groups: [...document.querySelectorAll('.workbench-group')].map(row => ({id: row.dataset.groupId, text: row.textContent?.slice(0, 80)})),
      })`);
      throw new Error(`${error.message}\nTerminal state: ${diagnostic}\nRenderer errors:\n${rendererErrors.join('\n')}`);
    }
    await client.evaluate(`document.querySelector('.workbench-terminal-rail button[aria-label="New terminal"]').click(); true`);
    try {
      // A clean profile concurrently hydrates the provider catalog. Some
      // external account probes are intentionally bounded but can keep the
      // ordered WebSocket command queue busy for more than 20 seconds; the
      // terminal command itself remains durable and must settle afterward.
      await waitFor(client, `document.querySelectorAll('.workbench-terminal-rail button[aria-pressed]').length > 0`, 'durable terminal', 60000);
    } catch (error) {
      const diagnostic = await client.evaluate(`document.querySelector('.workbench-terminal-pane')?.innerText || ''`);
      throw new Error(`${error.message}\nTerminal pane:\n${diagnostic}\nWebSocket frames:\n${websocketFrames.slice(-30).join('\n')}\nRenderer errors:\n${rendererErrors.join('\n')}`);
    }

    await client.evaluate(`document.querySelector('[data-view="settings"]').click(); true`);
    try {
      await waitFor(client, `document.querySelector('.settings-overlay__surface')`, 'Settings overlay');
    } catch (error) {
      const diagnostic = await client.evaluate(`JSON.stringify({
        view: document.querySelector('.deck')?.dataset?.view || '',
        footer: document.querySelector('[data-view="settings"]')?.outerHTML || '',
        overlays: document.querySelectorAll('.settings-overlay').length,
      })`);
      throw new Error(`${error.message}\nSettings state: ${diagnostic}\nRenderer errors:\n${rendererErrors.join('\n')}`);
    }
    assert.strictEqual(await client.evaluate(`document.querySelector('dialog.settings-overlay')?.matches(':modal') && document.querySelector('dialog.settings-overlay').contains(document.activeElement)`), true,
      'the native Settings dialog must own modal stacking and keyboard focus');
    await client.evaluate(`document.querySelector('.settings-overlay__close').click(); true`);
    await waitFor(client, `!document.querySelector('.settings-overlay')`, 'Settings close');

    if (process.env.VARIANT1_TEST_NATIVE_POPOUTS === '1') {
      await require('./test-native-popouts-checks')({client, CdpClient, getJson, waitFor, websocketFrames, rendererErrors, root, getOutput: () => output});
    }

    assert.deepStrictEqual(rendererErrors, [], `uncaught renderer errors:\n${rendererErrors.join('\n')}`);
    try { await client.evaluate(`setTimeout(() => window.variant1Deck.close(), 0); true`); }
    catch (error) { if (!/CDP target closed/.test(error.message)) throw error; }
    console.log('deck Electron smoke: one socket, split workbench, Files, dynamic Browser tabs, durable Terminal, Settings, and no legacy panel');
  } finally {
    if (client) client.close();
    await waitForExit();
    await removeIsolatedProfile();
  }
})().catch(error => {
  console.error(error.stack || error);
  if (child.exitCode == null) child.kill();
  process.exitCode = 1;
});
