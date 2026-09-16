'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

module.exports = async function testNativePopouts({client, CdpClient, getJson, waitFor, websocketFrames, rendererErrors, root, getOutput}) {
  const childClients = [];
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  async function childFor(id) {
    const deadline = Date.now() + 15000;
    while (Date.now() < deadline) {
      const target = (await getJson('/json/list')).find(item => {
        try { const url = new URL(item.url); return url.pathname.endsWith('/popout.html') && url.searchParams.get('surface') === id; } catch { return false; }
      });
      if (target?.webSocketDebuggerUrl) {
        const child = new CdpClient(target.webSocketDebuggerUrl);
        await child.open(); childClients.push(child);
        child.on('Runtime.exceptionThrown', params => rendererErrors.push(params.exceptionDetails?.exception?.description || params.exceptionDetails?.text));
        await child.call('Runtime.enable');
        await waitFor(child, `document.querySelector('.native-window-chrome')`, `${id} native content`);
        return child;
      }
      await pause(60);
    }
    throw new Error(`Native window ${id} did not open: ${await client.evaluate("document.body.innerText.slice(-1800)")}\n${getOutput().split('\n').filter(line => /native-window|SecurityError|ERR_|popout/.test(line)).slice(-20).join('\n')}`);
  }
  async function click(child, label) {
    await child.evaluate(`(() => { const button = document.querySelector('[aria-label=' + ${JSON.stringify(JSON.stringify(label))} + ']'); if (!button) throw new Error('Missing ${label}'); button.click(); return true; })()`);
  }
  async function dock(child, label, parentSelector) {
    try { await child.evaluate(`setTimeout(() => document.querySelector('[aria-label=' + ${JSON.stringify(JSON.stringify(label))} + ']').click(), 0); true`); }
    catch (error) { if (!/CDP target closed/.test(error.message)) throw error; }
    await waitFor(client, parentSelector, `docked ${label}`);
  }
  try {
    await click(client, 'Python runtime and backend status');
    await waitFor(client, `document.querySelector('[aria-label="Pop out python runtime"]')`, 'runtime popout action');
    await click(client, 'Pop out python runtime');
    const runtime = await childFor('utility:runtime');
    await waitFor(runtime, `document.querySelector('.runtime-summary .kernel-glyph.is-ready canvas')`, 'native procedural kernel canvas');
    assert.ok(await runtime.evaluate(`document.body.innerText.includes('Python kernel')`));
    assert.equal(await client.evaluate(`document.querySelectorAll('.runtime-details').length`), 0, 'runtime is moved, not duplicated');
    assert.equal(await runtime.evaluate(`typeof window.variant1Deck`), 'undefined', 'child does not receive the privileged Deck bridge');
    assert.equal(await runtime.evaluate(`document.querySelectorAll('script').length`), 0, 'child cannot bootstrap another backend socket');
    await click(client, 'Python runtime and backend status');
    assert.equal((await getJson('/json/list')).filter(item => item.url.includes('surface=utility%3Aruntime')).length, 1);
    await click(runtime, 'Keep window on top');
    await waitFor(runtime, `document.querySelector('[aria-label="Keep window on top"]').getAttribute('aria-pressed') === 'true'`, 'native pin state');
    await client.evaluate(`window.variant1Deck.toggleMaximize(); true`);
    await pause(150);
    const owner = await client.evaluate(`({left: screenX, top: screenY, right: screenX + outerWidth, width: outerWidth})`);
    await runtime.evaluate(`window.moveTo(${owner.right - 70}, ${owner.top + 30}); true`);
    await waitFor(runtime, `screenX + outerWidth > ${owner.right} || screenX < ${owner.left}`, 'window outside the Deck bounds');
    const shot = await runtime.call('Page.captureScreenshot', {format: 'png'});
    fs.writeFileSync(path.join(require('./native-test-artifacts')(root,'artifacts','panels'), 'native-runtime-popout.png'), Buffer.from(shot.data, 'base64'));
    await dock(runtime, 'Dock Python runtime panel', `document.querySelector('dialog[open] .runtime-details')`);
    await click(client, 'Close python runtime');
    console.log('native runtime: shared state, one window, pin, outside bounds, and docking passed');

    await click(client, 'Overview');
    await waitFor(client, `document.querySelector('.overview-scroll')`, 'overview content');
    await client.evaluate(`[...document.querySelectorAll('.overview-scroll .utility-tabs button')].find(button => button.textContent === 'Requests').click(); true`);
    await click(client, 'Pop out overview');
    const overview = await childFor('utility:overview');
    assert.equal(await overview.evaluate(`document.querySelector('.utility-tabs [aria-pressed="true"]').textContent`), 'Requests');
    const before = websocketFrames.filter(frame => frame.includes('>') && frame.includes('hardware:telemetry')).length;
    await pause(1350);
    assert.ok(websocketFrames.filter(frame => frame.includes('>') && frame.includes('hardware:telemetry')).length > before,
      'Overview must continue polling while Chat is the main surface');
    await dock(overview, 'Dock Overview panel', `document.querySelector('dialog[open] .overview-scroll')`);
    assert.equal(await client.evaluate(`document.querySelector('.overview-scroll .utility-tabs [aria-pressed="true"]').textContent`), 'Requests');
    await click(client, 'Close overview');
    console.log('native Overview: selected detail and live telemetry survived the round trip');

    await client.evaluate(`document.querySelector('.history-utility-button').click(); true`);
    await waitFor(client, `document.querySelector('[aria-label="Pop out automations"]')`, 'automation popout action');
    await click(client, 'Pop out automations');
    const automations = await childFor('utility:automations');
    await waitFor(automations, `document.querySelector('.automations-create')`, 'native automations');
    await automations.evaluate(`document.querySelector('.automations-create').click(); true`);
    await client.evaluate(`window.variant1Deck.minimize(); true`);
    await automations.evaluate(`document.querySelector('.automation-builder').dispatchEvent(new Event('submit', {bubbles: true, cancelable: true})); true`);
    await waitFor(automations, `document.querySelector('.automation-builder [role="alert"]')?.textContent.includes('Add both')`, 'visible validation in detached form');
    const automationShot = await automations.call('Page.captureScreenshot', {format: 'png'});
    fs.writeFileSync(path.join(require('./native-test-artifacts')(root,'artifacts','panels'), 'frontend-maintainability-automation.png'), Buffer.from(automationShot.data, 'base64'));
    await dock(automations, 'Dock Automations panel', `document.querySelector('dialog[open] .automation-builder [role="alert"]')`);
    await waitFor(client, `document.visibilityState === 'visible'`, 'Dock restores the minimized main window');
    await click(client, 'Close automations');
    console.log('native Automations: visible validation with main minimized, preserved form, and docking restore passed');

    assert.equal(await client.evaluate(`[...document.querySelectorAll('button')].filter(button=>button.getAttribute('aria-label')==='Detach Chats panel').length`),0,'history remains docked');

    const filesId = await client.evaluate(`document.querySelector('.workbench-files').closest('[data-group-id]').dataset.groupId`);
    await click(client, 'Detach Files panel');
    const files = await childFor(`pane:${filesId}`);
    await waitFor(files, `document.querySelectorAll('.workbench-file-row').length > 0`, 'native file tree');
    await files.evaluate(`document.querySelector('.workbench-file-row').dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, clientX: 100, clientY: 100})); true`);
    await waitFor(files, `document.querySelector('[role="menu"]')`, 'native file menu');
    await files.evaluate(`document.querySelector('[role="menu"]').dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})); true`);
    await click(files, 'Keep window on top');
    const placement = await files.evaluate(`({left: screenX, top: screenY, width: outerWidth, height: outerHeight})`);
    await click(client, 'Edit layout');
    await client.evaluate(`[...document.querySelectorAll('.workbench-editbar button')].find(button => button.textContent === 'Save as').click(); true`);
    await waitFor(client, `document.querySelector('[aria-label="Layout name"]')`, 'layout name editor');
    await client.evaluate(`document.querySelector('[aria-label="Layout name"]').focus(); true`);
    await client.call('Input.insertText', {text: 'Native design QA'});
    await client.evaluate(`document.querySelector('.workbench-preset-form').requestSubmit(); true`);
    await waitFor(client, `!document.querySelector('.workbench-preset-form')`, 'native layout saved');
    const savedLayout = await client.evaluate(`Object.entries(JSON.parse(localStorage.getItem('variant1.workbench.presets.v1'))).find(([, preset]) => preset.name === 'Native design QA')`);
    assert.ok(savedLayout?.[1]?.windows?.length > 0, 'layout persists its native placements');
    assert.equal(savedLayout[1].windows.find(window => window.id === `pane:${filesId}`).pinned, true);
    await dock(files, 'Dock Files panel', `document.querySelector('.workbench-files')`);
    await client.evaluate(`(() => { const select = document.querySelector('[aria-label="Layout preset"]'); select.value = 'user-native-design-qa'; select.dispatchEvent(new Event('change', {bubbles: true})); return true; })()`);
    const restoredFiles = await childFor(`pane:${filesId}`);
    await waitFor(restoredFiles, `document.querySelector('[aria-label="Keep window on top"]').getAttribute('aria-pressed') === 'true'`, 'saved pin state');
    const restoredPlacement = await restoredFiles.evaluate(`({left: screenX, top: screenY, width: outerWidth, height: outerHeight})`);
    for (const key of Object.keys(placement)) assert.ok(Math.abs(placement[key] - restoredPlacement[key]) < 4, `restored ${key}: expected ${JSON.stringify(placement)}, got ${JSON.stringify(restoredPlacement)}; saved ${JSON.stringify(savedLayout[1].windows)}`);
    await click(client, 'Panels and windows');
    await waitFor(client, `document.querySelector('.command-palette')`, 'native window switcher');
    await client.evaluate(`[...document.querySelectorAll('.command-palette [role="option"]')].find(button => button.textContent.includes('Dock resource panels')).click(); true`);
    await waitFor(client, `document.querySelector('.workbench-files') && !document.querySelector('.command-palette')`, 'Dock all from window switcher');
    await client.evaluate(`[...document.querySelectorAll('.workbench-editbar button')].find(button => button.textContent === 'Done').click(); true`);
    console.log('native design: procedural canvas, saved position/pin, window switcher, and Dock all passed');

    // An unsaved editor is adopted as the same DOM node; no project file is written.
    await client.evaluate(`[...document.querySelectorAll('.workbench-file-row')].find(row => row.querySelector('.workbench-file-row__name')?.textContent === 'package.json').dispatchEvent(new MouseEvent('dblclick', {bubbles: true})); true`);
    await waitFor(client, `document.querySelector('.workbench-file-preview')`, 'file preview');
    await client.evaluate(`[...document.querySelectorAll('.workbench-file-preview button')].find(button => button.textContent === 'Edit').click(); true`);
    await waitFor(client, `document.querySelector('textarea[aria-label="Edit package.json"]')`, 'file editor');
    await client.evaluate(`window.__popoutEditor = document.querySelector('textarea[aria-label="Edit package.json"]'); window.__popoutEditor.focus(); window.__popoutEditor.select(); true`);
    await client.call('Input.insertText', {text: 'UNSAVED_NATIVE_POPOUT_CHECK'});
    const editorId = await client.evaluate(`window.__popoutEditor.closest('[data-group-id]').dataset.groupId`);
    await client.evaluate(`Promise.all([...document.querySelectorAll('.workbench-browser .workbench-browser__guest')].map((guest, i) => guest.loadURL('about:blank#native-current-' + i)))`);
    const guestIds=await client.evaluate(`[...document.querySelectorAll('.workbench-browser .workbench-browser__guest')].map(guest=>guest.getWebContentsId())`);
    assert.ok(guestIds.length>0,"native browser transfer check needs a real guest");
    await client.evaluate(`Promise.all([...document.querySelectorAll('.workbench-browser .workbench-browser__guest')].map(guest=>guest.executeJavaScript('window.__nativeMoveMarker=41')))`);
    const guestUrls = await client.evaluate(`[...document.querySelectorAll('.workbench-browser .workbench-browser__guest')].map(guest => guest.getURL())`);
    await client.evaluate(`document.querySelector('[data-group-id="${editorId}"] .workbench-tabs__detach').click(); true`);
    const editor = await childFor(`pane:${editorId}`);
    assert.equal(await editor.evaluate(`document.querySelector('textarea').value`), 'UNSAVED_NATIVE_POPOUT_CHECK');
    await waitFor(editor, `(() => { try { return [...document.querySelectorAll('.workbench-browser .workbench-browser__guest')].every(guest => !!guest.getURL()); } catch { return false; } })()`, 'reattached native browser guests');
    const movedGuests = await editor.evaluate(`[...document.querySelectorAll('.workbench-browser .workbench-browser__guest')].map(guest => guest.getURL())`);
    assert.deepEqual(movedGuests, guestUrls, 'browser guests retain their current URLs when the preview group moves');
    assert.deepEqual(await editor.evaluate(`[...document.querySelectorAll('.workbench-browser .workbench-browser__guest')].map(guest=>guest.getWebContentsId())`),guestIds);
    assert.deepEqual(await editor.evaluate(`Promise.all([...document.querySelectorAll('.workbench-browser .workbench-browser__guest')].map(guest=>guest.executeJavaScript('window.__nativeMoveMarker')))`),guestIds.map(()=>41));
    await dock(editor, 'Dock package.json panel', `document.querySelector('textarea[aria-label="Edit package.json"]')`);
    assert.equal(await client.evaluate(`document.querySelector('textarea[aria-label="Edit package.json"]') === window.__popoutEditor`), true);
    assert.equal(await client.evaluate(`window.__popoutEditor.value`), 'UNSAVED_NATIVE_POPOUT_CHECK');
    await waitFor(client, `(() => { try { return [...document.querySelectorAll('.workbench-browser .workbench-browser__guest')].every(guest => guest.getURL().includes('#native-current-')); } catch { return false; } })()`, 'current browser URLs after docking');
    await client.evaluate(`[...document.querySelectorAll('.workbench-file-preview button')].find(button => button.textContent === 'Cancel').click(); true`);
    console.log('native panes: file menu, unsaved editor identity, and current browser URLs passed');

    await client.evaluate(`[...document.querySelectorAll('.workbench-tab')].find(tab => /Browser/.test(tab.textContent))?.querySelector('[role="tab"]')?.click(); true`);
    await waitFor(client, `Array.from(document.querySelectorAll('.workbench-browser .workbench-browser__guest')).some(guest => guest.dataset.browserReady === 'true' && guest.getBoundingClientRect().width > 0)`, 'ready capture guest');
    await client.evaluate(`[...document.querySelectorAll('[aria-label="Capture screenshot"]')].find(button => button.getClientRects().length > 0).click(); true`);
    await waitFor(client, `Array.from(document.querySelectorAll('.workbench-file-preview__media img')).some(image => image.src.startsWith('data:image/png;base64,') && image.naturalWidth > 0)`, 'production native browser capture');
    console.log('native browser: production preload, trusted capture IPC, and decoded PNG passed');

    if (!await client.evaluate(`document.querySelector('.workbench-terminal-rail [data-terminal-id]') !== null`)) {
      await click(client,'New terminal');
      await waitFor(client, `document.querySelector('.workbench-terminal-rail [data-terminal-id][aria-pressed="true"]')`, 'first terminal in the acknowledged new chat', 60000);
    }
    const originalTerminal = await client.evaluate(`document.querySelector('.workbench-terminal-rail [data-terminal-id][aria-pressed="true"]').dataset.terminalId`);
    const beforeNew = await client.evaluate(`document.querySelectorAll('.workbench-terminal-rail [data-terminal-id]').length`);
    await waitFor(client, `!document.querySelector('[aria-label="New terminal"]').disabled`, 'terminal creation enabled');
    await click(client, 'New terminal');
    try { await waitFor(client, `document.querySelectorAll('.workbench-terminal-rail [data-terminal-id]').length === ${beforeNew + 1}`, 'second deliberate terminal', 60000); }
    catch (error) { throw new Error(error.message + '\n' + await client.evaluate(`document.querySelector('.workbench-terminal-pane').innerText`) + '\n' + websocketFrames.filter(frame => /terminal:|execution:/.test(frame)).slice(-12).join('\n')); }
    const secondTerminal = await client.evaluate(`document.querySelector('.workbench-terminal-rail [data-terminal-id][aria-pressed="true"]').dataset.terminalId`);
    const opensBeforeSwitch = websocketFrames.filter(frame => frame.includes('>') && frame.includes('"type":"terminal:open"')).length;
    for (const id of [originalTerminal, secondTerminal, originalTerminal]) {
      await client.evaluate(`document.querySelector('[data-terminal-id="${id}"]').click(); true`);
      await waitFor(client, `document.querySelector('[data-terminal-id="${id}"]').getAttribute('aria-pressed') === 'true' && document.querySelectorAll('.xterm').length === 1`, 'one selected terminal emulator');
      await pause(150);
    }
    await client.evaluate(`document.querySelector('[title="Refresh terminals"]').click(); true`);
    await pause(350);
    const liveTerminals = await client.evaluate(`[...document.querySelectorAll('[data-terminal-id]')].map(button => ({id:button.dataset.terminalId,state:button.dataset.terminalState}))`);
    assert.ok([originalTerminal,secondTerminal].every(id => liveTerminals.some(item => item.id === id && item.state === 'running')), 'switching the emulator preserves both live backend PTYs');
    assert.equal(websocketFrames.filter(frame => frame.includes('>') && frame.includes('"type":"terminal:open"')).length,opensBeforeSwitch,'numbered selection must never create a terminal');
    console.log('native terminal selection: two live PTY identities retained, one emulator, zero implicit opens');

    const terminalCount = await client.evaluate(`document.querySelectorAll('.workbench-terminal-rail [aria-pressed]').length`);
    const terminalId = await client.evaluate(`document.querySelector('.workbench-terminal-pane').closest('[data-group-id]').dataset.groupId`);
    await click(client, 'Detach Terminal panel');
    const terminal = await childFor(`pane:${terminalId}`);
    await waitFor(terminal, `document.querySelector('.xterm-helper-textarea')`, 'native xterm');
    assert.equal(await terminal.evaluate(`document.querySelectorAll('.workbench-terminal-rail [aria-pressed]').length`), terminalCount);
    await terminal.evaluate(`document.querySelector('.xterm-helper-textarea').focus(); true`);
    await terminal.call('Input.insertText', {text: 'echo VARIANT1_NATIVE_WINDOW_CHECK'});
    await terminal.call('Input.dispatchKeyEvent', {type: 'keyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13});
    await terminal.call('Input.dispatchKeyEvent', {type: 'keyUp', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13});
    await pause(500);
    assert.ok(websocketFrames.some(frame => frame.includes('>') && frame.includes('VARIANT1_NATIVE_WINDOW_CHECK')), 'native terminal input must use the existing backend connection');
    await dock(terminal, 'Dock Terminal panel', `document.querySelector('.workbench-terminal-pane')`);
    assert.equal(await client.evaluate(`Number(document.body.dataset.deckLiveSockets)`), 1);
    assert.equal(await client.evaluate(`Number(document.body.dataset.deckMaxConcurrentSockets)`), 1);
    console.log('native terminal: existing PTY and single backend connection passed');

    const mainSize = await client.evaluate(`({width: outerWidth, height: outerHeight})`);
    await client.evaluate(`window.resizeTo(800, 680); true`);
    await waitFor(client, `innerWidth <= 830`, 'compact main window');
    await click(client, 'Show chats');
    await waitFor(client, `document.querySelector('.workbench-side-overlay .history-panel')`, 'compact Chats drawer');
    await client.evaluate(`document.querySelector('.workbench-side-overlay .workbench-tabs__detach').click(); true`);
    const compactChats = await childFor(`pane:${historyId}`);
    assert.ok(await compactChats.evaluate(`!!document.getElementById('history-search-input')`));
    await dock(compactChats, 'Dock Chats panel', `document.querySelector('.workbench-side-overlay .history-panel')`);
    await click(client, 'Close Chats panel');
    await client.evaluate(`window.resizeTo(${mainSize.width}, ${mainSize.height}); true`);
    await waitFor(client, `innerWidth > 830 && document.querySelector('.history-panel')`, 'restored main window');
    console.log('native compact layout: drawer detaches and docks into the compact chat interface');

    await click(client, 'Log monitor');
    let monitorTarget;
    for (let i = 0; i < 100 && !monitorTarget; i++) {
      monitorTarget = (await getJson('/json/list')).find(item => item.url.endsWith('/frontend/monitor.html'));
      if (!monitorTarget) await pause(50);
    }
    assert.ok(monitorTarget, 'Log monitor must open as its own native window');
    const monitor = new CdpClient(monitorTarget.webSocketDebuggerUrl);
    childClients.push(monitor); await monitor.open();
    await waitFor(monitor, `document.readyState === 'complete' && document.querySelector('#mon-pin') && typeof window.variant1Monitor?.controlWindow === 'function'`, 'initialized monitor window controls');
    await click(monitor, 'Keep Log monitor on top');
    await waitFor(monitor, `document.querySelector('#mon-pin').getAttribute('aria-pressed') === 'false'`, 'monitor unpin');
    await click(monitor, 'Maximize or restore Log monitor');
    assert.equal(await monitor.evaluate(`typeof window.variant1Monitor.getLogHistory`), 'function');
    console.log('native Log monitor: independent window, pin, maximize, and existing log bridge passed');
  } finally {
    for (const child of childClients) child.close();
  }
};
