'use strict';
// Model-free diagnostic of the canonical renderer host and actual Electron guests.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const esbuild = require('esbuild');
const root = path.resolve(__dirname, '..');
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'variant1-browser-host-'));
const out = require('./native-test-artifacts')(root, 'artifacts/frontend-browser-controls-2026-09-06/browser-host', 'browser-host');
fs.mkdirSync(out, {recursive: true});
(async () => {
  const deck = path.join(temp, 'frontend/main-deck'); fs.mkdirSync(deck, {recursive:true});
  await esbuild.build({entryPoints:[path.join(__dirname, 'test-browser-host-native-entry.tsx')], bundle:true,
    jsx:'automatic', platform:'browser', format:'esm', outfile:path.join(deck,'renderer.js'), logLevel:'silent'});
  fs.copyFileSync(path.join(root,'frontend/main-deck/dist/platform.css'), path.join(deck,'fixture.css'));
  fs.copyFileSync(path.join(root,'frontend/main-deck/popout.html'), path.join(deck,'popout.html'));
  // Popout CSS has the production relative path.
  fs.mkdirSync(path.join(deck, 'dist'), {recursive:true});
  fs.copyFileSync(path.join(deck,'fixture.css'), path.join(deck,'dist/platform.css'));
  fs.writeFileSync(path.join(deck,'index.html'), fs.readFileSync(path.join(root,'frontend/main-deck/index.html'),'utf8').replace('./dist/platform.js','./renderer.js').replace('./dist/platform.css','./fixture.css'));
  fs.writeFileSync(path.join(temp,'preload.cjs'), `const {contextBridge,ipcRenderer}=require('electron'); contextBridge.exposeInMainWorld('variant1Deck', {
    workbenchBrowser:command=>ipcRenderer.invoke('workbench:browser:view',command),
    onWorkbenchBrowserEvent:cb=>{const listener=(_,event)=>cb(event);ipcRenderer.on('workbench:browser:event',listener);return()=>ipcRenderer.removeListener('workbench:browser:event',listener)},
    supportsNativeWindows:true, captureWorkbenchPreview:id=>ipcRenderer.invoke('workbench:browser:capture',id), controlNativeWindow:(id,action,size)=>ipcRenderer.invoke('workbench:window:control',id,action,size),
    onNativeWindowClosed:cb=>{const fn=(_,id)=>cb(id);ipcRenderer.on('workbench:window:closed',fn);return()=>ipcRenderer.removeListener('workbench:window:closed',fn)},
    readWorkbenchDirectory:async root=>({ok:true,entries:[{name:'README.md',path:root+'/README.md',directory:false},{name:'src',path:root+'/src',directory:true}]}),
    getWorkbenchGitStatus:async()=>({ok:true,files:[]}),
    getWorkbenchRoot:async()=>({ok:false}),log:message=>console.log(message)
  });`);
  fs.writeFileSync(path.join(temp,'main.cjs'), `
    const {app,BrowserWindow,protocol,nativeImage,screen}=require('electron');
    const fs=require('node:fs'), http=require('node:http'), assert=require('node:assert/strict');
    const boot=require(${JSON.stringify(path.join(root,'electron-app-boot.js'))});
    const {createNativePopoutManager}=require(${JSON.stringify(path.join(root,'electron-native-popouts.js'))});
    boot.applyGpuFlags(app); boot.registerVariant1Scheme(protocol);
    app.setPath('userData',${JSON.stringify(path.join(temp,'profile'))});
    const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
    const logs=[]; let win, server;
    app.whenReady().then(async()=>{
      boot.registerVariant1Protocol(protocol,${JSON.stringify(temp)});
      server=http.createServer((req,res)=>{res.setHeader('Content-Type','text/html');res.end('<!doctype html><html><head><title>'+req.url+'</title></head><body style="background:#111;color:white;font:24px monospace"><h1>Native browser '+req.url+'</h1><button id="probe">Local button</button><input aria-label="Local input"><div style="position:fixed;left:680px;top:350px;width:40px;height:40px;background:white"> </div></body></html>');});
      await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
      const base='http://127.0.0.1:'+server.address().port;
      win=new BrowserWindow({show:false,width:1240,height:820,webPreferences:{preload:${JSON.stringify(path.join(temp,'preload.cjs'))},sandbox:true,contextIsolation:true,nodeIntegration:false,webviewTag:true,backgroundThrottling:false}});
      const manager=createNativePopoutManager({appRoot:${JSON.stringify(root)},getDeckWindow:()=>win,isTrustedIpcSender:event=>event.sender===win.webContents,hardenAppWindow:()=>{},log:message=>logs.push(message)});
      manager.attach(win);
      const views=require(${JSON.stringify(path.join(root,'electron-browser-views.js'))}).createBrowserViewManager({getDeckWindow:()=>win,getNativeWindow:manager.getWindow,isTrustedIpcSender:event=>event.sender===win.webContents,hardenGuestContents:()=>{},log:message=>logs.push(message)});
      require(${JSON.stringify(path.join(root,'electron-browser-capture.js'))}).registerBrowserCapture({getDeckWindow:()=>win,isTrustedIpcSender:event=>event.sender===win.webContents,isNativeHost:manager.isHost,getRetainedGuest:views.getGuest,log:message=>logs.push(message)});
      win.webContents.on('console-message',event=>{logs.push(event.message);console.log(event.message)});
      win.webContents.on('render-process-gone',(_,detail)=>console.error('E01_RENDERER_GONE',detail));
      const evaluate=expression=>win.webContents.executeJavaScript(expression,true);
      const command=value=>{console.log('E01_COMMAND',value.action,value.tab_id);return evaluate('window.e01.command('+JSON.stringify(value)+')')};
      const check=(result,label)=>{assert.equal(result.ok,true,label+': '+JSON.stringify(result));return result};
      let frameNumber=0;
      const readable=result=>{
        assert.ok(result.viewport.width>=800&&result.viewport.height>=480,JSON.stringify(result.viewport));
        const png=Buffer.from(result.image,'base64');
        fs.writeFileSync(${JSON.stringify(out)}+'/frame-'+(++frameNumber)+'.png',png);
        assert.equal(png.readUInt32BE(16),result.image_width);assert.equal(png.readUInt32BE(20),result.image_height);
        const image=nativeImage.createFromBuffer(png).resize({width:result.viewport.width,height:result.viewport.height});
        const pixels=image.getBitmap();const at=(370*result.viewport.width+700)*4;
        assert.ok(pixels[at]>240&&pixels[at+1]>240&&pixels[at+2]>240,'the rendered frame includes page content beyond a narrow dock');
      };
      await win.loadURL('variant1://app/frontend/main-deck/index.html');
      win.showInactive(); console.log('E01_LOADED');
      await pause(500);
      check(await command({action:'new_page',tab_id:'native-a',url:base+'/one'}),'new page');
      check(await command({action:'read',tab_id:'native-a'}),'read first page');
      check(await command({action:'evaluate',tab_id:'native-a',expression:'window.cloneProbe = 0'}),'initialize clone diagnostic');
      const cloneFailure=await command({action:'evaluate',tab_id:'native-a',expression:'(window.cloneProbe++, () => document.title)'});
      assert.equal(cloneFailure.ok,false,'an uninvoked function cannot cross the Electron result bridge');
      assert.match(String(cloneFailure.error),/Invoke function expressions/);
      assert.match(String(cloneFailure.error),/effects may already have occurred/);
      const cloneRecovery=check(await command({action:'evaluate',tab_id:'native-a',expression:'(() => ({title:document.title, calls:window.cloneProbe}))()'}),'explicit invocation recovery');
      assert.equal(cloneRecovery.value.title,'/one');
      assert.equal(cloneRecovery.value.calls,1,'a clone failure must not replay JavaScript effects');
      const active=check(await command({action:'screenshot',tab_id:'native-a'}),'active capture');
      readable(active);
      fs.writeFileSync(${JSON.stringify(path.join(out,'native-active.png'))},Buffer.from(active.image,'base64'));
      check(await command({action:'new_page',tab_id:'native-b',url:base+'/two'}),'second page');
      assert.equal(await evaluate('window.e01.group("native-a")'),await evaluate('window.e01.group("native-b")'),'new host tabs share the browser group');
      const inactive=check(await command({action:'screenshot',tab_id:'native-a'}),'inactive tab capture');
      assert.ok(inactive.state.url.endsWith('/one'));
      readable(inactive);
      const viewport=check(await command({action:'set_viewport',tab_id:'native-a',width:1280,height:720}),'explicit viewport');
      assert.equal(viewport.viewport.width,1280);assert.equal(viewport.viewport.height,720);
      readable(check(await command({action:'screenshot',tab_id:'native-a'}),'explicit-size capture'));
      assert.equal((await command({action:'set_viewport',tab_id:'native-a',width:182,height:597})).code,'INVALID_VIEWPORT');
      await evaluate('[...document.querySelectorAll("[data-browser-expand]")].find(button=>button.closest(".workbench-pane-layer.is-active")).click(); true');await pause(150);
      const expanded=check(await command({action:'screenshot',tab_id:'native-a'}),'expanded capture');readable(expanded);
      assert.equal(expanded.diagnostics.guest_generation,viewport.diagnostics.guest_generation,'expanding keeps the existing browser guest');
      await evaluate('[...document.querySelectorAll("[data-browser-expand]")].find(button=>button.getAttribute("aria-pressed")==="true").click(); true');await pause(100);
      await evaluate('window.e01.hide("native-a")'); await pause(80);
      check(await command({action:'screenshot',tab_id:'native-a'}),'hidden pane capture');
      const beforeTransfer=check(await command({action:'evaluate',tab_id:'native-a',expression:'(()=>{window.retainedMarker={value:17};document.querySelector("input").value="unsent text";return window.retainedMarker.value})()'}),'seed retained state');
      const referenceBefore=check(await command({action:'read',tab_id:'native-a'}),'reference before transfer').elements.find(row=>row.name==='Local button').ref;
      const generationBefore=(await command({action:'state',tab_id:'native-a'})).diagnostics.guest_generation;
      const group=await evaluate('window.e01.detach("native-a")'); await pause(600);
      check(await command({action:'read',tab_id:'native-a'}),'detached guest read');
      const retainedState=check(await command({action:'evaluate',tab_id:'native-a',expression:'({marker:window.retainedMarker?.value,draft:document.querySelector("input").value})'}),'retained state');assert.deepEqual(retainedState.value,{marker:17,draft:'unsent text'});assert.equal(retainedState.diagnostics.guest_generation,generationBefore);
      check(await command({action:'click',tab_id:'native-a',target:referenceBefore}),'pre-detach reference still resolves');
      console.log('E09_NATIVE_GEOMETRY',JSON.stringify({windows:BrowserWindow.getAllWindows().map(w=>({bounds:w.getBounds(),content:w.getContentBounds()})),display:screen.getPrimaryDisplay().workArea}));
      console.log('E09_GUEST_GEOMETRY',JSON.stringify(await command({action:'evaluate',tab_id:'native-a',expression:'({width:innerWidth,height:innerHeight,dpr:devicePixelRatio,screenX,screenY})'})));
      const detached=check(await command({action:'screenshot',tab_id:'native-a'}),'detached capture');readable(detached);assert.equal(detached.viewport.width,1280);
      check(await command({action:'set_viewport',tab_id:'native-a',width:1024,height:640}),'resize detached browser');
      const resized=check(await command({action:'screenshot',tab_id:'native-a'}),'resized detached capture');readable(resized);assert.equal(resized.viewport.width,1024);
      check(await command({action:'set_viewport',tab_id:'native-a',mode:'auto'}),'automatic detached viewport');
      BrowserWindow.getAllWindows().find(window=>window!==win && !window.isDestroyed()).setSize(1500,1000);await pause(200);
      const auto=check(await command({action:'screenshot',tab_id:'native-a'}),'automatic viewport follows native window resize');assert.ok(auto.viewport.width>1200);readable(auto);
      check(await command({action:'set_viewport',tab_id:'native-a',width:1280,height:720}),'restore detached viewport');
      await evaluate('window.e01.dock()'); await pause(300);
      const docked=check(await command({action:'screenshot',tab_id:'native-a'}),'docked capture');readable(docked);assert.equal(docked.viewport.width,1280);
      assert.deepEqual((await command({action:'evaluate',tab_id:'native-a',expression:'({marker:window.retainedMarker?.value,draft:document.querySelector("input").value})'})).value,{marker:17,draft:'unsent text'},'docking preserves page runtime/input');
      check(await command({action:'navigate',tab_id:'native-a',url:base+'/changed'}),'navigation');
      const changed=check(await command({action:'read',tab_id:'native-a'}),'read after navigation');
      assert.ok(changed.text.includes('/changed'));
      check(await command({action:'screenshot',tab_id:'native-a'}),'capture after navigation');
      await evaluate('window.e01.stackWithChat("native-a")'); await pause(250);
      const stacked=check(await command({action:'read',tab_id:'native-a'}),'browser stacked with Chat');
      assert.ok(stacked.url.endsWith('/changed'));
      await evaluate('[...document.querySelectorAll("button")].find(button=>button.getAttribute("aria-label")==="Pop out browser").click(); true'); await pause(450);
      check(await command({action:'screenshot',tab_id:'native-a'}),'shared native host from Chat group');
      assert.equal(await evaluate('Boolean(document.querySelector(".chat-workspace"))'),true,'Chat stays in the main window');
      await evaluate('window.e01.dock()'); await pause(250);
      check(await command({action:'close_page',tab_id:'native-b'}),'close page');
      assert.equal((await command({action:'read',tab_id:'native-b'})).code,'TAB_NOT_FOUND');
      assert.ok(!logs.some(line=>/E01_CSP|Uncaught|Unhandled|ResizeObserver loop/.test(line)),logs.join('\\n'));

      await evaluate('window.e01.selectChat("owner-a")');
      check(await command({action:'new_page',owner_chat_id:'owner-a',tab_id:'owned-a',url:base+'/owner-a'}),'owned create A');
      check(await command({action:'evaluate',owner_chat_id:'owner-a',tab_id:'owned-a',expression:'window.__ownershipSentinel = 37'}),'set retained page state');
      await evaluate('window.e01.selectChat("owner-b")');
      check(await command({action:'new_page',owner_chat_id:'owner-b',tab_id:'owned-b',url:base+'/owner-b'}),'owned create B');
      const inventory=check(await command({action:'tabs',owner_chat_id:'owner-a'}),'owned inventory');
      assert.deepEqual(inventory.tabs.map(t=>t.id),['owned-a']);
      assert.equal((await command({action:'read',owner_chat_id:'owner-b',tab_id:'owned-a'})).code,'TAB_NOT_FOUND');
      check(await command({action:'read',owner_chat_id:'owner-a',tab_id:'owned-a'}),'background owned read');
      const retained=check(await command({action:'evaluate',owner_chat_id:'owner-a',tab_id:'owned-a',expression:'window.__ownershipSentinel'}),'retained background state');
      assert.equal(retained.value ?? retained.result,37,'chat switch preserves live page state');
      const selected=await evaluate('document.querySelector(".history-item.active")?.dataset.sessionId');
      assert.equal(selected,'owner-b','background operation cannot switch foreground chat');
      await evaluate('window.e01.selectChat("owner-a")');
      check(await command({action:'read',owner_chat_id:'owner-a',tab_id:'owned-a'}),'restored owned read');
      const closeGroup=await evaluate('window.e01.detach("owned-a")');await pause(450);
      const guestBeforeClose=await evaluate('window.variant1Deck.workbenchBrowser({action:"call",tabId:"owned-a",method:"executeJavaScript",args:["1"]})');
      await evaluate('window.e01.closeWindow('+JSON.stringify(closeGroup)+')');await pause(200);
      assert.equal((await command({action:'read',owner_chat_id:'owner-a',tab_id:'owned-a'})).code,'TAB_NOT_FOUND','window X closes the browser tab rather than docking it');
      assert.equal(views.getGuest(guestBeforeClose.state.guestId),null,'window close destroys the native guest');
      assert.equal(await evaluate('document.querySelectorAll("[data-retained-browser=owned-a]").length'),0);
      await evaluate('window.e01.showProjects()');await pause(150);
      fs.writeFileSync(${JSON.stringify(path.join(out,'chat-projects.png'))},(await win.webContents.capturePage()).toPNG());
      console.log('Chat ownership native: two owners, filtered inventory, foreign-target rejection, background read and unchanged foreground passed');
      console.log('E01 native: ready/active/inactive/hidden/detached/docked/navigated/closed guests passed');
      console.log('E09 native: shared tabs, readable page pixels, viewport requests, in-place expansion, and retained detached/docked sizing passed');
    }).catch(error=>{logs.push(error.stack||String(error));console.error(error);process.exitCode=1;}).finally(async()=>{
      if(win&&!win.isDestroyed()) {try{fs.writeFileSync(${JSON.stringify(path.join(out,'receipts.json'))},JSON.stringify(await win.webContents.executeJavaScript('window.e01.receipts()'),null,2))}catch{}}
      fs.writeFileSync(${JSON.stringify(path.join(out,'native.log'))},logs.join('\\n'));
      if(server) server.close(); app.exit(process.exitCode||0);
    });
  `);
  const child=spawn(require('electron'),[path.join(temp,'main.cjs')],{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe']});
  let output=''; child.stdout.on('data',chunk=>{output+=chunk;process.stdout.write(chunk)}); child.stderr.on('data',chunk=>{output+=chunk;process.stderr.write(chunk)});
  const timer=setTimeout(()=>child.kill(),60000);
  const code=await new Promise(resolve=>child.once('exit',resolve)); clearTimeout(timer);
  fs.writeFileSync(path.join(out,'electron.log'),output);
  process.exitCode=code===0?0:1;
  if(path.dirname(temp)===path.resolve(os.tmpdir())&&path.basename(temp).startsWith('variant1-browser-host-')) fs.rmSync(temp,{recursive:true,force:true,maxRetries:4,retryDelay:150});
})().catch(error=>{console.error(error);process.exitCode=1});
