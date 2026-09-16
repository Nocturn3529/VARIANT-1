'use strict';

// The production Deck bundle and real WebSocket transport against a local,
// deterministic admission peer. No model, real backend, or observer socket.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const output = require('./native-test-artifacts')(root, 'artifacts/frontend-post-rerun-2026-09-06/chat-native', 'chat-ownership');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

async function launch() {
  const {spawn} = require('node:child_process');
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'variant1-chat-ownership-'));
  fs.mkdirSync(output, {recursive: true});
  const child = spawn(require('electron'), [__filename], {
    cwd: root, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
    env: {...process.env, VARIANT1_CHAT_TEST_DIR: temporary},
  });
  let log = '';
  for (const stream of [child.stdout, child.stderr]) stream.on('data', chunk => {log += chunk; process.stdout.write(chunk);});
  const deadline = setTimeout(() => child.kill(), 90000);
  try {
    const code = await new Promise((resolve, reject) => {child.once('error', reject); child.once('exit', resolve);});
    process.exitCode = code === 0 ? 0 : 1;
  } finally {
    clearTimeout(deadline);
    fs.writeFileSync(path.join(output, 'native.log'), log);
    if (path.dirname(temporary) === path.resolve(os.tmpdir()) && path.basename(temporary).startsWith('variant1-chat-ownership-')) {
      fs.rmSync(temporary, {recursive: true, force: true, maxRetries: 5, retryDelay: 150});
    }
  }
}

async function runNative() {
  const {app, BrowserWindow, protocol} = require('electron');
  const {WebSocketServer} = require('ws');
  const boot = require('../electron-app-boot');
  const temporary = path.resolve(process.env.VARIANT1_CHAT_TEST_DIR || '');
  assert.equal(path.dirname(temporary), path.resolve(os.tmpdir()));
  assert.ok(path.basename(temporary).startsWith('variant1-chat-ownership-'));
  boot.applyGpuFlags(app);
  boot.registerVariant1Scheme(protocol);
  app.setPath('userData', path.join(temporary, 'profile'));
  let win, peer, server, liveSockets = 0, peakSockets = 0, connections = 0, passed = false;
  const commands = [], events = [], checks = [], errors = [];
  const sessions = new Map(['A', 'B'].map(id => [id, {id, title: 'Native chat ' + id, messages: [], runtime: {busy: false}}]));
  const pending = new Map(['A', 'B'].map(id => [id, []]));
  const created = new Map();
  let sharedView = 'A', loseNewAck = true, rejectNew = false, admissionCount = 0;
  const emit = (message, socket = peer) => {events.push(message); if (socket?.readyState === 1) socket.send(JSON.stringify(message));};
  const list = socket => emit({type: 'chat:sessions', active_id: sharedView, items: [...sessions.values()].map(({id, title, messages}) => ({id, title, message_count: messages.length}))}, socket);
  const snapshot = (id, navigation, socket) => emit({type: 'chat:session', session: sessions.get(id), ...(navigation ? {navigation} : {})}, socket);
  const route = id => ({session_id: id, client_id: sessions.get(id).clientId, source: 'chat', admission_id: sessions.get(id).runtime.active_admission_id, run_id: sessions.get(id).runtime.active_run_id});
  const token = (id, text) => emit({type: 'token', ...route(id), token: text});
  const complete = (id, text) => {
    const owner = route(id), session = sessions.get(id);
    session.runtime = {busy: false};
    session.messages.push({role: 'assistant', text, ts: Date.now() / 1000});
    emit({type: 'done', ...owner, text});
  };
  const question = (chatId, id) => ({type: 'clarification:request', chat_id: chatId, id, run_id: 'run-' + chatId,
    questions: [{id: 'q', header: 'Native chat ' + chatId, question: 'Choose the next step for chat ' + chatId,
      multiSelect: false, options: [{label: 'Inspect', description: ''}, {label: 'Continue', description: 'Keep working'}]}]});
  try {
    await app.whenReady();
    boot.registerVariant1Protocol(protocol, root);
    server = new WebSocketServer({port: 0, host: '127.0.0.1', path: '/ws'});
    await new Promise(resolve => server.once('listening', resolve));
    server.on('connection', socket => {
      peer = socket; connections++; liveSockets++; peakSockets = Math.max(peakSockets, liveSockets);
      socket.on('close', () => {liveSockets--;});
      socket.on('message', bytes => {
        try {
          const command = JSON.parse(String(bytes)); commands.push({connection: connections, ...command});
          const id = command.id || sharedView;
          switch (command.type) {
            case 'chat:sessions': list(socket); break;
            case 'chat:session:get': snapshot(id, null, socket); break;
            case 'chat:session:switch':
              assert.ok(sessions.has(id)); sharedView = id;
              snapshot(id, {request_id: command.request_id, requested_id: id, effective_id: id, status: 'switched'}, socket); break;
            case 'chat:session:new': {
              assert.ok(command.request_id);
              if (rejectNew) {emit({type: 'chat:new:result', request_id: command.request_id, requested_id: '', effective_id: sharedView, status: 'rejected', error: 'Local fixture rejected creation'}, socket); break;}
              if (!created.has(command.request_id)) {
                const nextId = 'C' + (created.size + 1); created.set(command.request_id, nextId);
                sessions.set(nextId, {id: nextId, title: 'New native chat', messages: [], runtime: {busy: false}});
              }
              const nextId = created.get(command.request_id);
              if (loseNewAck) {loseNewAck = false; sharedView = 'A'; socket.close(1012, 'fixture loses creation acknowledgement'); break;}
              sharedView = nextId;
              snapshot(nextId, {request_id: command.request_id, requested_id: '', effective_id: nextId, status: 'created'}, socket); list(socket); break;
            }
            case 'chat:runtime:get': emit({type: 'chat:runtime', id, runtime: sessions.get(id)?.runtime || {busy: false}}, socket); break;
            case 'chat': {
              assert.ok(command.session_id, 'real UI inputs carry explicit ownership');
              const session = sessions.get(command.session_id); assert.ok(session);
              if (session.runtime.busy) {
                assert.equal(command.admission_id, session.runtime.active_admission_id);
                assert.equal(command.run_id, session.runtime.active_run_id);
                emit({type: 'chat:queued', ...route(session.id), ticket_id: command.ticket_id, delivery: command.delivery}, socket);
              } else {
                assert.equal(command.admission_id, undefined, 'new input cannot inherit a previous run');
                admissionCount++;
                session.clientId = command.client_id;
                session.messages.push({role: 'user', text: command.text, ts: Date.now() / 1000});
                session.runtime = {busy: true, active_admission_id: 'admission-' + admissionCount, active_run_id: 'run-' + admissionCount};
                emit({type: 'start', ...route(session.id)}, socket); token(session.id, session.id + ' live prefix');
              }
              break;
            }
            case 'chat:pause':
              assert.equal(command.admission_id, sessions.get(command.session_id).runtime.active_admission_id);
              assert.equal(command.run_id, sessions.get(command.session_id).runtime.active_run_id);
              emit({type:'chat:pause_state',...route(command.session_id),request_id:command.request_id,pause_revision:1,state:'pausing',accepted:true},socket);break;
            case 'cancel':
              assert.ok(command.session_id);
              assert.equal(command.admission_id, sessions.get(command.session_id).runtime.active_admission_id);
              emit({type: 'cancelling', ...route(command.session_id), accepted: true}, socket); break;
            case 'clarification:list':
              assert.ok(command.chat_id, 'question hydration is scoped');
              emit({type: 'clarification:snapshot', chat_id: command.chat_id, request_id: command.request_id, pending: pending.get(command.chat_id) || []}, socket); break;
            case 'clarification:response': break; // The driver delivers an intentionally delayed acknowledgement.
            case 'chat:session:annotate': break; // Current backend semantics: annotation never navigates.
          }
        } catch (error) {errors.push(error.stack);}
      });
    });
    const preload = path.join(temporary, 'preload.cjs');
    fs.writeFileSync(preload, `const {contextBridge}=require('electron');contextBridge.exposeInMainWorld('variant1Deck',{getBackendInfo:async()=>({port:${server.address().port},token:'local-fixture'}),getWorkbenchRoot:async()=>({ok:false}),log:message=>console.log(message)});`);
    win = new BrowserWindow({show: false, width: 1250, height: 850, title: 'VARIANT-1 — local acceptance fixture',
      webPreferences: {preload, contextIsolation: true, sandbox: true, nodeIntegration: false, webviewTag: true, backgroundThrottling: false}});
    win.webContents.on('render-process-gone', (_event, details) => errors.push('renderer gone: ' + JSON.stringify(details)));
    win.webContents.on('console-message', event => {if (event.level === 'error' || /uncaught|unhandledrejection|module .* failed/.test(event.message)) errors.push(event.message);});
    const evaluate = source => win.webContents.executeJavaScript(source, true);
    async function until(label, predicate, timeout = 7000) {
      const start = Date.now();
      while (Date.now() - start < timeout) {if (await predicate()) return; if (errors.length) throw new Error(errors.join('\n')); await pause(40);}
      throw new Error('Timed out: ' + label);
    }
    const selected = id => evaluate(`!!document.querySelector('[data-session-id="${id}"] .history-item__select[aria-current="true"]')`);
    async function click(selector) {
      await until('enabled ' + selector, () => evaluate(`!!document.querySelector(${JSON.stringify(selector)}) && !document.querySelector(${JSON.stringify(selector)}).disabled`));
      await evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`);
    }
    async function select(id) {await click(`[data-session-id="${id}"] .history-item__select`); await until('selected ' + id, () => selected(id));}
    async function type(selector, value) {
      await evaluate(`(()=>{const input=document.querySelector(${JSON.stringify(selector)});input.focus();input.select();})()`);
      await win.webContents.insertText(value);
      await until('typed ' + value, () => evaluate(`document.querySelector(${JSON.stringify(selector)})?.value === ${JSON.stringify(value)}`));
    }
    const messages = () => evaluate(`document.querySelector('#message-column')?.innerText || ''`);
    const last = type => [...commands].reverse().find(command => command.type === type);
    async function send(text, steering = false) {
      await type('textarea[aria-label="Message VARIANT-1"]', text);
      await click(steering ? '[aria-label="Send steering message"]' : '[aria-label="Send message"]');
      await until('sent ' + text, () => last('chat')?.text === text);
    }
    async function screenshot(name) {fs.writeFileSync(path.join(output, name + '.png'), (await win.webContents.capturePage()).toPNG());}
    await win.loadURL('variant1://app/frontend/main-deck/index.html'); win.showInactive();
    await until('initial A', () => selected('A'));
    await send('Alpha task');
    await until('A stream', async () => (await messages()).includes('A live prefix'));
    await select('B'); await send('Beta task');
    await until('B stream', async () => (await messages()).includes('B live prefix'));
    token('A', ' continues in background'); await pause(100);
    assert.ok(!(await messages()).includes('A live prefix'));
    assert.equal(await evaluate(`document.querySelectorAll('[data-working="1"]').length`), 2);
    await select('A'); assert.ok((await messages()).includes('A live prefix continues in background'));
    await select('B'); assert.ok((await messages()).includes('B live prefix'));
    checks.push('Concurrent A/B streams remain in their owning chat; switching preserves both and both working badges.');

    await type('textarea[aria-label="Message VARIANT-1"]', 'Unsent Beta draft');
    for (const id of ['A', 'B']) {const item = question(id, 'question-' + id); pending.set(id, [item]); emit(item);}
    await until('B question', () => evaluate(`document.querySelector('[data-question-chat="B"]') !== null`));
    assert.equal(await evaluate(`document.activeElement?.value`), 'Unsent Beta draft', 'question arrival cannot steal typed composer focus');
    await type('input[aria-label="Your answer"]', 'Beta answer');
    await select('A'); await until('A question', () => evaluate(`!!document.querySelector('[data-question-chat="A"]')`));
    await select('B'); await until('retained B answer', () => evaluate(`document.querySelector('input[aria-label="Your answer"]')?.value === 'Beta answer'`));
    assert.equal(await evaluate(`document.querySelector('textarea[aria-label="Message VARIANT-1"]').value`), 'Unsent Beta draft');
    await screenshot('scoped-question-and-draft');
    await click('.runtime-clarification__actions button:last-child');
    await until('B answer submitted', () => !!last('clarification:response'));
    const answer = last('clarification:response'); assert.equal(answer.chat_id, 'B'); assert.deepEqual(answer.answers, {q: 'Beta answer'});
    assert.equal(await evaluate(`document.querySelector('.runtime-clarification__actions button:last-child').disabled`), true);
    await select('A'); pending.set('B', []);
    emit({type: 'clarification:response:ack', chat_id: 'B', id: answer.id, request_id: answer.request_id, status: 'resolved'});
    await pause(100); assert.equal(await evaluate(`document.querySelector('[data-question-chat]')?.dataset.questionChat`), 'A');
    checks.push('Questions are chat scoped; typed answers survive navigation/refresh; submission waits for its correlated acknowledgement; late B acknowledgement leaves A visible.');

    await select('B'); await send('Steer Beta', true);
    const steer = last('chat'); assert.equal(steer.session_id, 'B'); assert.equal(steer.admission_id, sessions.get('B').runtime.active_admission_id);
    await click('[aria-label="Pause task"]');await until('Pause B',()=>!!last('chat:pause'));
    assert.equal(last('chat:pause').session_id,'B');
    emit({type:'chat:pause_state',...route('B'),pause_revision:2,state:'paused',accepted:true});
    await until('B paused',()=>evaluate(`!!document.querySelector('[aria-label="Resume task"]')`));
    await click('[aria-label="Stop response"]'); await until('Stop B', () => !!last('cancel'));
    assert.equal(last('cancel').session_id, 'B'); assert.equal(sessions.get('A').runtime.busy, true);
    complete('B', 'Beta complete'); await until('B stops', () => evaluate(`!!document.querySelector('[aria-label="Send message"]')`));
    complete('A', 'Alpha complete');
    emit({type: 'chat:appended', session_id: 'A', messages: sessions.get('A').messages});
    await pause(120); assert.equal(await selected('B'), true); assert.ok(!(await messages()).includes('Alpha complete'));
    await send('Beta follow-up after Alpha settled'); assert.equal(last('chat').session_id, 'B');
    complete('B', 'Beta follow-up complete');
    checks.push('Steer and Stop carry B run fences; stopping B leaves A active; late A settlement cannot repaint B or misroute its next send.');

    await click('#new-chat');
    await until('creation replay after lost ack', () => commands.filter(command => command.type === 'chat:session:new').length === 2, 12000);
    await until('created C1', () => selected('C1'));
    const newCommands = commands.filter(command => command.type === 'chat:session:new');
    assert.equal(newCommands[0].request_id, newCommands[1].request_id); assert.equal(created.size, 1);
    rejectNew = true; await click('#new-chat');
    await until('creation rejected', () => events.some(event => event.type === 'chat:new:result'));
    await send('C1 remains usable after rejection'); assert.equal(last('chat').session_id, 'C1');
    complete('C1', 'Fixture complete');
    assert.equal(peakSockets, 1); assert.equal(connections, 2);
    assert.equal(await evaluate('document.body.dataset.deckMaxConcurrentSockets'), '1');
    assert.equal(commands.filter(command => command.type === 'clarification:response').length, 1);
    assert.deepEqual(errors, []);
    checks.push('Lost new-chat acknowledgement replays the same request on reconnect; unrelated shared default is ignored; rejection leaves the current chat usable; peak one socket.');
    await screenshot('completed-acceptance'); passed = true;
    console.log('E13/E14 native Deck acceptance passed: ' + checks.length + ' scenarios, ' + commands.length + ' commands, peak ' + peakSockets + ' socket.');
  } catch (error) {
    errors.push(error.stack); console.error(error);
    if (win && !win.isDestroyed()) {
      try {fs.writeFileSync(path.join(output, 'failure.png'), (await win.webContents.capturePage()).toPNG()); fs.writeFileSync(path.join(output, 'failure.txt'), await win.webContents.executeJavaScript('document.body.innerText'));} catch {}
    }
  } finally {
    fs.writeFileSync(path.join(output, 'receipt.json'), JSON.stringify({passed, peer: 'local deterministic admission fixture; no model or backend', peakSockets, connections, checks, errors, commands, events}, null, 2));
    for (const socket of server?.clients || []) socket.terminate(); server?.close(); win?.destroy();
    app.exit(passed ? 0 : 1);
  }
}

(process.versions.electron ? runNative() : launch()).catch(error => {console.error(error); process.exitCode = 1;});
