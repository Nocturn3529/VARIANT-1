'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const root = path.resolve(__dirname, '..');
const out = require('./native-test-artifacts')(root, 'artifacts/pr21-workbench-reading', 'reading');

async function launch() {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'variant1-reading-'));
  const deck = path.join(temp, 'frontend/main-deck');
  const esbuild = require('esbuild');
  try {
    await esbuild.build({entryPoints:{platform:path.join(__dirname,'test-workbench-reading-entry.tsx')},
      outdir:path.join(deck,'dist'),bundle:true,jsx:'automatic',format:'esm',platform:'browser',loader:{'.woff2':'file'},logLevel:'silent'});
    for (const file of ['index.html', 'popout.html']) fs.copyFileSync(path.join(root,'frontend/main-deck',file),path.join(deck,file));
    fs.writeFileSync(path.join(temp,'preload.js'), `const {ipcRenderer,contextBridge}=require('electron');
      contextBridge.exposeInMainWorld('variant1Deck',{supportsNativeWindows:true,controlNativeWindow:(...args)=>ipcRenderer.invoke('workbench:window:control',...args)});`);
    const child = require('node:child_process').spawn(require('electron'), [
      '--force-device-scale-factor=1', `--user-data-dir=${path.join(temp,'user')}`, __filename,
    ], {windowsHide:true,stdio:'inherit',env:{...process.env, VARIANT1_READING_FIXTURE:temp}});
    const timer = setTimeout(() => child.kill(), 45000);
    try { assert.equal(await new Promise((resolve,reject) => {child.once('exit',resolve);child.once('error',reject);}), 0); }
    finally { clearTimeout(timer); }
  } finally {
    esbuild.stop();
    if (path.dirname(temp) === path.resolve(os.tmpdir()) && path.basename(temp).startsWith('variant1-reading-')) {
      fs.rmSync(temp, {recursive:true,force:true,maxRetries:8,retryDelay:100});
    }
  }
}

async function check() {
  const {app, BrowserWindow, protocol, net} = require('electron');
  const {createNativePopoutManager} = require('../electron-native-popouts');
  const temp = process.env.VARIANT1_READING_FIXTURE;
  app.setPath('userData',path.join(temp,'user'));
  protocol.registerSchemesAsPrivileged([{scheme:'variant1',privileges:{standard:true,secure:true,supportFetchAPI:true}}]);
  await app.whenReady();
  protocol.handle('variant1', request => {
    const url = new URL(request.url);
    const file = path.resolve(temp,'.'+decodeURIComponent(url.pathname));
    if (url.host !== 'app' || !file.startsWith(path.join(temp,'frontend') + path.sep)) return new Response('',{status:404});
    return net.fetch(pathToFileURL(file).href);
  });
  const win = new BrowserWindow({width:1440,height:900,useContentSize:true,frame:false,show:false,focusable:false,opacity:0,
    webPreferences:{preload:path.join(temp,'preload.js'),contextIsolation:true,sandbox:true,backgroundThrottling:false}});
  const manager = createNativePopoutManager({appRoot:root,getDeckWindow:()=>win,isTrustedIpcSender:(event,owner)=>event.sender===owner.webContents,hardenAppWindow(){},log(){}});
  manager.attach(win);
  win.webContents.on('did-create-window',child=>{child.setFocusable(false);child.setOpacity(0);});
  const run = async (expression, target=win) => {
    try {return await target.webContents.executeJavaScript(expression);}
    catch (error) {throw new Error(expression + '\n' + await target.webContents.executeJavaScript('document.body.innerText.slice(-800)') + '\n' + error);}
  };
  const pause = ms => new Promise(resolve => setTimeout(resolve,ms));
  async function wait(expression, target=win) {
    const deadline=Date.now()+6500;
    while (Date.now()<deadline) { if (await run(expression,target)) return; await pause(35); }
    throw new Error('Timed out: '+expression+'\n'+await run('document.body.innerText.slice(-700)',target));
  }
  const click = (label,target=win) => run(`document.querySelector(${JSON.stringify('[aria-label="'+label+'"]')}).click()`,target);
  const rows = [];
  try {
    await win.loadURL('variant1://app/frontend/main-deck/index.html');
    await wait('window.readingFixture && document.querySelector(".peer-chat__bubble")');
    await run('readingFixture.hideFiles()');
    fs.mkdirSync(out,{recursive:true});
    for (const [width,height] of [[1440,900],[1000,700]]) {
      win.setContentSize(width,height);
      await wait(`innerWidth===${width} && innerHeight===${height} && !!document.querySelector(".history-panel")===${width===1440}`);
      await wait('[".message__content",".message__meta time","#composer-input",".peer-chat__bubble",".chat-turn--live [role=status]"].every(s=>document.querySelector(s))');
      const report = await run(`(() => {
        const style = s => getComputedStyle(document.querySelector(s));
        const rect = s => { const r=document.querySelector(s).getBoundingClientRect(); return {x:r.x,y:r.y,width:r.width,height:r.height}; };
        const status=document.querySelector(".chat-turn--live [role=status], .current-task [role=status], .turn-status [role=status]");
        return {viewport:[innerWidth,innerHeight],transcript:style(".message__content").fontSize,
          peer:style(".peer-chat__bubble").fontSize,composer:style("#composer-input").fontSize,
          timestamp:style(".message__meta time").fontSize,muted:style(".message__meta time").color,
          peerRect:rect(".peer-chat__bubble"),composerRect:rect("#composer-input"),status:status?.textContent || null,
          titlebar:document.querySelector(".titlebar__drag")?.textContent};
      })()`);
      for (const font of [report.transcript,report.peer,report.composer]) assert.equal(font,'15px');
      assert.equal(report.timestamp,'11px'); assert.equal(report.titlebar,'');
      assert.ok(report.status);
      assert.ok(report.peerRect.width>200 && report.composerRect.width>200 && report.composerRect.y>report.peerRect.y);
      rows.push(report);
      fs.writeFileSync(path.join(out,`comfortable-${width}.png`),(await win.webContents.capturePage()).toPNG());
    }
    await click('Show agent sessions');
    await wait('document.querySelector(".workbench-side-overlay .history-panel #history-search-input")');
    await click('Close Agent sessions panel');
    await wait('!document.querySelector(".workbench-side-overlay")');
    await run('readingFixture.compact(true)');
    await wait('document.documentElement.dataset.density==="compact" && !!document.querySelector(".history-panel")');
    assert.equal(await run('getComputedStyle(document.querySelector(".message__content")).fontSize'),'13px');
    await run('readingFixture.compact(false); readingFixture.reveal()');
    await wait('document.querySelector(\'[aria-label="Add output to Observatory / Dashboard rebuild"]:enabled\')');
    await run('readingFixture.detach()');
    let child;
    for (let i=0;i<100&&!child;i++){child=BrowserWindow.getAllWindows().find(other=>other!==win);if(!child)await pause(35);}
    assert.ok(child);
    await wait('document.querySelector(".xterm-screen") && document.querySelector(\'[aria-label="Go to owning chat"]\')',child);
    await click('Keep window on top',child);
    await wait('document.querySelector(\'[aria-label="Keep window on top"]\').getAttribute("aria-pressed")==="true"',child);
    await run('readingFixture.switchToB()');
    await wait('readingFixture.state().current==="test-reader-b"');
    assert.equal(await run('document.title',child),'Terminal · Observatory / Dashboard rebuild — VARIANT-1');
    await click('Add output to Observatory / Dashboard rebuild',child);
    await wait('readingFixture.state().a.includes("full-output-marker") && readingFixture.state().b===""');
    await run(`(() => {const screen=document.querySelector(".xterm-screen"),rect=screen.getBoundingClientRect();
      const where={bubbles:true,cancelable:true,clientX:rect.left+12,clientY:rect.top+7,button:0,detail:2};
      screen.dispatchEvent(new MouseEvent("mousedown",{...where,buttons:1}));document.dispatchEvent(new MouseEvent("mouseup",where));})()`,child);
    await wait('document.querySelector(\'[aria-label="Add selection to Observatory / Dashboard rebuild"]\')',child);
    await click('Add selection to Observatory / Dashboard rebuild',child);
    await run('document.querySelector(".xterm textarea").dispatchEvent(new KeyboardEvent("keydown",{key:"l",ctrlKey:true,bubbles:true,cancelable:true}))',child);
    const state = await run('readingFixture.state()');
    assert.deepEqual(state.a.trim().split(/\r?\n\s*\n/).map(part=>part.trim()),['AlphaMarker full-output-marker','AlphaMarker','AlphaMarker']);
    assert.equal(state.current,'test-reader-b'); assert.equal(state.b,'');
    fs.writeFileSync(path.join(out,'detached-owner.png'),(await child.webContents.capturePage()).toPNG());
    await click('Go to owning chat',child);
    await wait('readingFixture.state().current==="test-reader-a"');
    assert.deepEqual(await run('readingFixture.state().switches'),['test-reader-a']);
    fs.writeFileSync(path.join(out,'results.json'),JSON.stringify({rows,transfer:state,processes:app.getAppMetrics().map(({pid,creationTime})=>({pid,creationTime}))},null,2));
    console.log('Comfortable/Compact transcript and history, detached terminal owner, selection/output transfer and explicit chat navigation passed');
  } finally {
    for (const window of BrowserWindow.getAllWindows()) if (!window.isDestroyed()) window.destroy();
  }
}
if (process.versions.electron) check().then(()=>require('electron').app.exit(0),error=>{console.error(error);require('electron').app.exit(1);});
else launch().catch(error=>{console.error(error);process.exitCode=1;});
