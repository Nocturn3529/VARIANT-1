'use strict';

// Real Chromium unload decisions with the Deck's document store and quit handler.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const root = path.resolve(__dirname, '..');

async function launch() {
  const temp = await fs.mkdtemp(path.join(os.tmpdir(), 'variant-editor-close-'));
  try {
    await require('esbuild').build({stdin: {resolveDir: root, contents: `
      import {guardEditorUnload,openFilePreview} from './frontend/main-deck/src/workbench/previewStore';
      import {beginFileEdit,getFileDocument,loadFileDocument,saveFileDocument,updateFileDraft} from './frontend/main-deck/src/workbench/fileDocumentStore';
      guardEditorUnload(window, window.nativeEditor.confirm);
      let id, release, saving;
      window.fixture = {
        async edit() {
          id = openFilePreview('example.txt','example.txt');
          await loadFileDocument(id,'example.txt',async()=>({ok:true,text:'original',mtimeMs:1}));
          beginFileEdit(id);updateFileDraft(id,'draft');
        },
        draft() {return getFileDocument(id).text;},
        save() {saving=saveFileDocument(id,'example.txt',()=>new Promise(resolve=>{release=resolve}));},
        async finish() {release({ok:true,mtimeMs:2});await saving;},
      };
    `}, bundle: true, platform: 'browser', outfile: path.join(temp,'fixture.js')});
    await fs.writeFile(path.join(temp,'page.html'), '<!doctype html><main>Editor fixture</main><script src="fixture.js"></script>');
    await fs.writeFile(path.join(temp,'preload.cjs'), `
      const {contextBridge,ipcRenderer}=require('electron');
      contextBridge.exposeInMainWorld('nativeEditor',{confirm:message=>ipcRenderer.sendSync('fixture:discard',message)});
    `);
    const proc = spawn(require('electron'), [__filename], {windowsHide:true, stdio:'inherit',
      env:{...process.env, VARIANT1_EDITOR_CLOSE_FIXTURE:temp}});
    const timer = setTimeout(()=>proc.kill(), 25000);
    try { const [code] = await once(proc,'exit'); assert.equal(code,0); }
    finally {clearTimeout(timer);}
  } finally { await fs.rm(temp,{recursive:true,force:true,maxRetries:4,retryDelay:100}); }
}

async function native() {
  const {app,BrowserWindow,ipcMain} = require('electron');
  const {closeDeckBeforeQuit,createBeforeQuitHandler} = require('../electron-app-boot');
  const temp=path.resolve(process.env.VARIANT1_EDITOR_CLOSE_FIXTURE);
  assert.equal(path.dirname(temp),path.resolve(os.tmpdir()));
  assert.ok(path.basename(temp).startsWith('variant-editor-close-'));
  app.setPath('userData',path.join(temp,'profile'));
  let win, allow=false, confirms=0, stopped=0, quit=0, quitting=false;
  try {
    await app.whenReady();
    app.on('window-all-closed',()=>{});
    win=new BrowserWindow({show:false,webPreferences:{preload:path.join(temp,'preload.cjs'),contextIsolation:true,sandbox:true}});
    ipcMain.on('fixture:discard',(event,message)=>{assert.match(message,/example\.txt/);confirms++;event.returnValue=allow;});
    const run=js=>win.webContents.executeJavaScript(js,true);
    await win.loadFile(path.join(temp,'page.html'));
    await run('fixture.edit()');
    let loads=1;
    win.webContents.on('did-finish-load',()=>{loads++;});
    assert.equal(await closeDeckBeforeQuit(win),false);
    console.log('Native editor kept its draft after close');
    await new Promise(resolve=>setTimeout(resolve,100));
    const beforeReload=confirms;
    win.webContents.reload();
    for(let n=0;n<80 && confirms===beforeReload;n++)await new Promise(resolve=>setTimeout(resolve,25));
    assert.ok(confirms>beforeReload,`reload consults the saved document guard (loads=${loads})`);
    assert.equal(await run('fixture.draft()'),'draft');
    console.log('Native editor kept its draft after reload');
    const beforeQuit=createBeforeQuitHandler({closeDeck:()=>closeDeckBeforeQuit(win),setQuitting:value=>{quitting=value;},
      unregisterShortcuts:()=>{},stopBackend:()=>{stopped++;},quit:()=>{quit++;},log:()=>{}});
    const prevented=once(win.webContents,'will-prevent-unload');
    beforeQuit({preventDefault(){}});await prevented;await new Promise(setImmediate);
    assert.equal(quitting,false);assert.equal(stopped,0);assert.equal(quit,0);
    allow=true;
    await run('fixture.save()');
    const prior=confirms;
    assert.equal(await closeDeckBeforeQuit(win),false);
    assert.equal(confirms,prior,'a pending save keeps its window alive');
    await run('fixture.finish()');await run('fixture.edit()');
    const closed=once(win,'closed');
    beforeQuit({preventDefault(){}});await closed;await new Promise(setImmediate);
    assert.equal(quitting,true);assert.equal(stopped,1);assert.equal(quit,1);
    console.log('Native Deck editor: cancelled close, reload, quit and pending save preserved the document; explicit discard completed quit.');
  } finally {if(win && !win.isDestroyed())win.destroy();}
}

if (process.versions.electron) {
  native().then(()=>require('electron').app.exit(0),error=>{console.error(error);require('electron').app.exit(1);});
} else {
  launch().catch(error=>{console.error(error);process.exitCode=1;});
}
