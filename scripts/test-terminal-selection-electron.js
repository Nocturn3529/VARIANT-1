'use strict';
const fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {spawn}=require('node:child_process');
const esbuild=require('esbuild');
const root=path.resolve(__dirname,'..'),temp=fs.mkdtempSync(path.join(os.tmpdir(),'variant1-terminal-selection-'));
const before=process.argv.includes('--before');
const resize=process.argv.includes('--resize');
const output=require('./native-test-artifacts')(root,'artifacts/pane-resources-2026-09-05','terminal-selection');
(async()=>{
  await esbuild.build({entryPoints:[path.join(__dirname,resize?'test-terminal-resize-entry.tsx':'test-terminal-selection-entry.tsx')],bundle:true,format:'esm',platform:'browser',jsx:'automatic',outfile:path.join(temp,'renderer.js'),logLevel:'silent'});
  fs.copyFileSync(path.join(root,'frontend/main-deck/dist/platform.css'),path.join(temp,'style.css'));
  fs.writeFileSync(path.join(temp,'index.html'),'<!doctype html><html><head><link rel="stylesheet" href="style.css"></head><body><div id="variant1-react-root"></div><script type="module" src="renderer.js"></script></body></html>');
  fs.writeFileSync(path.join(temp,'popout.html'),'<html><head><link rel="stylesheet" href="style.css"></head><body><div id="popout-ready"></div></body></html>');
  fs.writeFileSync(path.join(temp,'main.cjs'),`
    const {app,BrowserWindow}=require('electron');const fs=require('node:fs');const assert=require('node:assert/strict');
    app.setPath('userData',${JSON.stringify(path.join(temp,'profile'))});
    require(${JSON.stringify(path.join(root,'electron-app-boot.js'))}).applyGpuFlags(app);
    app.whenReady().then(async()=>{
      ${resize ? "app.on('browser-window-created',(_event,child)=>child.webContents.once('did-finish-load',()=>child.showInactive()));" : ""}
      const win=new BrowserWindow({show:false,width:800,height:600,webPreferences:{sandbox:true,contextIsolation:true,nodeIntegration:false,backgroundThrottling:false}});
      win.webContents.setWindowOpenHandler(()=>({action:'allow',overrideBrowserWindowOptions:{show:false,backgroundThrottling:false}}));
      await win.loadFile(${JSON.stringify(path.join(temp,'index.html'))});
${resize ? "win.showInactive();win.webContents.on('did-create-window',child=>child.showInactive());" : ""}
      const result=await win.webContents.executeJavaScript(${JSON.stringify(resize?'window.runTerminalResize()':'window.runTerminalSelection()')},true);
      fs.writeFileSync(${JSON.stringify(path.join(output,resize?'terminal-resize.json':before?'terminal-before.json':'terminal-after.json'))},JSON.stringify(result,null,2));
      console.log(JSON.stringify(result));
      ${resize?"for(const key of ['docked','detached','detachedResized','redocked']){assert.equal(result[key].aligned,true,key+' TUI rows align');assert.equal(result[key].fits,true,key+' fits slot');}assert.equal(result.maxInFlight,1);assert.ok(result.resizeRequests<20);assert.equal(result.storageWrites,0);assert.equal(result.endedRemoved,true);assert.equal(result.livePreserved,true);assert.ok(result.logReads<12);assert.equal(result.openCommands,0);":before?"assert.ok(result.openCommands>0,'reproduce passive terminal creation');":"assert.equal(result.openCommands,0,'Selecting an archived terminal cannot create a shell');assert.equal(result.writesToClosed,0);assert.equal(result.clearPreservedCursor,true);assert.equal(result.clearSignals,0);assert.equal(result.emulators,1,'Only the selected terminal needs an emulator');assert.equal(result.closedArchivedRemoved,true);assert.equal(result.remainingTerminals,0);assert.equal(result.remainingEmulators,0,'closing final terminal disposes TUI emulator');"}
      app.exit(0);
    }).catch(error=>{console.error(error);app.exit(1)});
  `);
  const child=spawn(require('electron'),[path.join(temp,'main.cjs')],{cwd:root,windowsHide:true,stdio:'inherit'});
  const timer=setTimeout(()=>child.kill(),20000);const code=await new Promise(resolve=>child.once('exit',resolve));clearTimeout(timer);process.exitCode=code===0?0:1;
  if(path.dirname(temp)===path.resolve(os.tmpdir())&&path.basename(temp).startsWith('variant1-terminal-selection-'))fs.rmSync(temp,{recursive:true,force:true,maxRetries:4,retryDelay:150});
})().catch(error=>{console.error(error);process.exitCode=1});
