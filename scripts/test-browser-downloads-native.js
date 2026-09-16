'use strict';
// Real Electron download events, disposable localhost page/profile, no model.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const esbuild = require('esbuild');
const root = path.resolve(__dirname, '..');
const probe = process.argv.includes('--probe');
const out = require('./native-test-artifacts')(root, 'artifacts/frontend-post-rerun-2026-09-06/' + (probe ? 'download-baseline' : 'download-native'), probe ? 'download-baseline' : 'downloads');
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'variant1-download-test-'));
fs.mkdirSync(out, {recursive:true});
(async () => {
  const deck = path.join(temp,'frontend/main-deck');fs.mkdirSync(deck,{recursive:true});
  await esbuild.build({entryPoints:[path.join(__dirname,'test-browser-host-native-entry.tsx')],bundle:true,jsx:'automatic',platform:'browser',format:'esm',outfile:path.join(deck,'renderer.js'),logLevel:'silent'});
  fs.copyFileSync(path.join(root,'frontend/main-deck/dist/platform.css'),path.join(deck,'fixture.css'));
  fs.writeFileSync(path.join(deck,'index.html'),fs.readFileSync(path.join(root,'frontend/main-deck/index.html'),'utf8').replace('./dist/platform.js','./renderer.js').replace('./dist/platform.css','./fixture.css'));
  const downloadApi = probe ? '' : `bindWorkbenchBrowser:(tab,id,operationId)=>ipcRenderer.invoke('workbench:browser:bind',tab,id,operationId),workbenchDownloads:command=>ipcRenderer.invoke('workbench:browser:downloads',command),onWorkbenchDownloads:callback=>{const handler=(_,rows)=>callback(rows);ipcRenderer.on('workbench:browser:downloads',handler);return()=>ipcRenderer.removeListener('workbench:browser:downloads',handler)},`;
  fs.writeFileSync(path.join(temp,'preload.cjs'), `const {contextBridge,ipcRenderer}=require('electron');contextBridge.exposeInMainWorld('variant1Deck',{${downloadApi}getWorkbenchRoot:async()=>({ok:false}),log:message=>console.log(message)});`);
  fs.writeFileSync(path.join(temp,'main.cjs'), `
    const {app,BrowserWindow,protocol,session}=require('electron');
    const fs=require('node:fs'),http=require('node:http'),assert=require('node:assert/strict');
    const boot=require(${JSON.stringify(path.join(root,'electron-app-boot.js'))});
    boot.applyGpuFlags(app);boot.registerVariant1Scheme(protocol);app.setPath('userData',${JSON.stringify(path.join(temp,'profile'))});
    const events=[],results=[];let win,server,downloadDone;
    const nativeDownloads=${probe ? 'null' : `require(${JSON.stringify(path.join(root,'electron-browser-downloads.js'))}).registerBrowserDownloads({getDataDir:()=>${JSON.stringify(temp)},getDeckWindow:()=>win,isTrustedIpcSender:event=>event.sender===win?.webContents,isNativeHost:()=>false,log:message=>console.log(message)})`};
    const downloaded=new Promise(resolve=>{downloadDone=resolve});
    app.on('web-contents-created',(_,guest)=>{
      if(guest.getType()!=='webview')return;
      guest.on('did-start-navigation',(_e,url,inPlace,mainFrame)=>events.push({event:'did-start-navigation',guest:guest.id,url,inPlace,mainFrame}));
      guest.on('did-fail-load',(_e,errorCode,errorDescription,url,mainFrame)=>events.push({event:'did-fail-load',guest:guest.id,errorCode,errorDescription,url,mainFrame}));
      guest.on('did-fail-provisional-load',(_e,errorCode,errorDescription,url,mainFrame)=>events.push({event:'did-fail-provisional-load',guest:guest.id,errorCode,errorDescription,url,mainFrame}));
      guest.on('did-stop-loading',()=>events.push({event:'did-stop-loading',guest:guest.id}));
      guest.on('dom-ready',()=>events.push({event:'dom-ready',guest:guest.id}));
      guest.on('render-process-gone',(_event,details)=>events.push({event:'render-process-gone',guest:guest.id,...details}));
    });
    app.whenReady().then(async()=>{
      boot.registerVariant1Protocol(protocol,${JSON.stringify(temp)});
      session.fromPartition('persist:variant1-preview').on('will-download',(_event,item,contents)=>{
        events.push({event:'will-download',guest:contents?.id,filename:item.getFilename()});
        if(${JSON.stringify(probe)})item.setSavePath(${JSON.stringify(path.join(temp,'download.bin'))});
        else assert.ok(item.getSavePath().startsWith(${JSON.stringify(path.join(temp,'data/browser/download-staging'))}),'the native owner stages the download before a save dialog can open');
        item.on('done',(_event,state)=>{events.push({event:'download-done',state});downloadDone(state)});
      });
      server=http.createServer((req,res)=>{
        if(req.url==='/slow.bin'){res.writeHead(200,{'Content-Type':'application/octet-stream','Content-Disposition':'attachment; filename="slow.bin"','Content-Length':'20'});res.write('1234567890');setTimeout(()=>res.end('abcdefghij'),800);return;}
        if(req.url==='/file.bin'){res.writeHead(200,{'Content-Type':'application/octet-stream','Content-Disposition':'attachment; filename="fixture.bin"'});res.end('VARIANT1-DOWNLOAD-FIXTURE');return;}
        res.setHeader('Content-Type','text/html');res.end('<!doctype html><title>Download fixture</title><h1>Local download fixture</h1><a href="/file.bin">Download</a>');
      });
      await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const base='http://127.0.0.1:'+server.address().port;
      win=new BrowserWindow({show:false,width:1200,height:800,webPreferences:{preload:${JSON.stringify(path.join(temp,'preload.cjs'))},contextIsolation:true,sandbox:true,nodeIntegration:false,webviewTag:true,backgroundThrottling:false}});
      win.webContents.on('console-message',event=>console.log(event.message));
      const evaluate=code=>win.webContents.executeJavaScript(code,true);
      const command=async value=>{const result=await evaluate('window.e01.command('+JSON.stringify(value)+')');results.push({command:value,result});return result};
      await win.loadURL('variant1://app/frontend/main-deck/index.html');win.showInactive();
      assert.equal((await command({action:'new_page',tab_id:'download-a',url:base+'/page'})).ok,true);
      await command({action:'navigate',tab_id:'download-a',url:base+'/file.bin',operation_id:'operation-A-download'});
      assert.equal(await downloaded,'completed');
      await new Promise(resolve=>setTimeout(resolve,100));
      for(const action of ['state','reload','navigate'])await command({action,tab_id:'download-a',url:base+'/recovered'});
      assert.equal((await command({action:'new_page',tab_id:'download-b',url:base+'/fresh'})).ok,true);
      assert.ok(events.some(event=>event.event==='will-download'));
      assert.ok(results.some(row=>row.command.url===base+'/file.bin'&&row.result.ok===false)||!${JSON.stringify(probe)},'record the navigation promise rejection without assuming a specific native error code');
      const recovery=results.filter(row=>['state','reload','navigate'].includes(row.command.action)&&row.command.url===base+'/recovered');
      if(${JSON.stringify(probe)})assert.ok(recovery.every(row=>row.result.ok===false),'the baseline reproduces blocked recovery');
      else assert.ok(recovery.every(row=>row.result.ok===true),'attached guests must allow recovery after a download');
      let downloadedPath=${JSON.stringify(path.join(temp,'download.bin'))};
      if(!${JSON.stringify(probe)}){
        let drained;
        for(let i=0;i<50;i++){drained=await command({action:'drain_downloads',tab_id:'download-a',operation_id:'operation-B-drain'});if(drained.downloads?.length)break;await new Promise(resolve=>setTimeout(resolve,20));}
        assert.equal(drained.downloads.length,1);const row=drained.downloads[0];downloadedPath=row.path;
        assert.equal(row.status,'completed');assert.equal(row.bytes,Buffer.byteLength('VARIANT1-DOWNLOAD-FIXTURE'));assert.equal(row.sha256.length,64);
        assert.equal(row.operation_id,'operation-A-download','a later drain cannot take ownership of the initiating operation');
        assert.equal(fs.readFileSync(downloadedPath,'utf8'),'VARIANT1-DOWNLOAD-FIXTURE');
        assert.equal((await command({action:'drain_downloads',tab_id:'download-b'})).downloads.length,0,'downloads stay with their source tab');
        await command({action:'ack_downloads',tab_id:'download-a',download_ids:[row.download_id]});
        assert.equal(fs.existsSync(downloadedPath),false,'only acknowledgement removes the staged file');
        assert.equal((await command({action:'drain_downloads',tab_id:'download-a'})).downloads.length,0);
        await command({action:'navigate',tab_id:'download-a',url:base+'/slow.bin',operation_id:'operation-slow-start'});
        const progress=await command({action:'downloads',tab_id:'download-a'});
        const pending=progress.downloads.find(item=>item.suggested_filename==='slow.bin');
        assert.ok(pending,'download history exposes native progress before CAS handoff');
        assert.equal(pending.operation_id,'operation-slow-start');
        assert.ok(['in_progress','finalizing','completed'].includes(pending.status));
        let slow;
        for(let i=0;i<100;i++){const result=await command({action:'drain_downloads',tab_id:'download-a'});slow=result.downloads.find(item=>item.suggested_filename==='slow.bin');if(slow)break;await new Promise(resolve=>setTimeout(resolve,20));}
        assert.ok(slow,'the existing download completes without another navigation');
        assert.equal(slow.operation_id,pending.operation_id);assert.equal(slow.bytes,20);
        await command({action:'ack_downloads',tab_id:'download-a',download_ids:[slow.download_id]});
      }else assert.equal(fs.readFileSync(downloadedPath,'utf8'),'VARIANT1-DOWNLOAD-FIXTURE');
      assert.ok(!events.some(event=>event.event==='render-process-gone'),'download recovery must not crash its renderer');
      console.log(${JSON.stringify(probe ? 'E11 native baseline: real download navigation and blocked recovery reproduced' : 'E11 native: staged download, no automatic dialog, scoped handoff, acknowledgement and browser recovery passed')});
    }).catch(error=>{console.error(error);process.exitCode=1}).finally(()=>{
      fs.writeFileSync(${JSON.stringify(path.join(out,'events-and-results.json'))},JSON.stringify({events,results},null,2));
      nativeDownloads?.dispose();if(server)server.close();app.exit(process.exitCode||0);
    });
  `);
  const child=spawn(require('electron'),[path.join(temp,'main.cjs')],{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe']});
  let log='';for(const stream of [child.stdout,child.stderr])stream.on('data',chunk=>{log+=chunk;process.stdout.write(chunk)});
  const deadline=setTimeout(()=>child.kill(),60000);
  const code=await new Promise(resolve=>child.once('exit',resolve));clearTimeout(deadline);
  fs.writeFileSync(path.join(out,'native.log'),log);process.exitCode=code===0?0:1;
  if(path.dirname(temp)===path.resolve(os.tmpdir())&&path.basename(temp).startsWith('variant1-download-test-'))fs.rmSync(temp,{recursive:true,force:true,maxRetries:4,retryDelay:150});
})().catch(error=>{console.error(error);process.exitCode=1});
