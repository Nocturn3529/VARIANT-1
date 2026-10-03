'use strict';
// Real Settings renderer and native export IPC, with isolated deterministic backend receipts.
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),{spawn}=require('node:child_process');
const {buildSync}=require('esbuild');
const root=path.resolve(__dirname,'..'),temp=fs.mkdtempSync(path.join(os.tmpdir(),'variant-context-settings-'));
const out=path.join(root,'artifacts/context-routing-settings-native');fs.mkdirSync(out,{recursive:true});
const entry=`import React from 'react';import {createRoot} from 'react-dom/client';import {SettingsOverlay} from './frontend/main-deck/src/SettingsOverlay';import {useAppState,selectSettingsCategory} from './frontend/main-deck/src/state/appStore';import * as ctx from './frontend/main-deck/src/externalContextSettingsStore';import * as routing from './frontend/main-deck/src/providerRoutingStore';import {setSessionContext,setSessionConnection,ingestSessions} from './frontend/main-deck/src/state/sessionStore';import {ingest as ingestPlatform} from './frontend/main-deck/src/store';ingestPlatform({type:'config',providers:[{name:'openrouter',display_name:'OpenRouter',configured:true,model:'stealth/space-bunny-alpha',reasoning_efforts:['minimal','low','medium','high','xhigh','max']}]});const runtime={send:command=>{window.fixtureTransport.send(command);return true},isOpen:()=>true,notify:()=>{}};ctx.setExternalContextSettingsContext(runtime);routing.setProviderRoutingContext(runtime);setSessionContext(runtime);window.fixtureTransport.onResult(message=>{ctx.ingestExternalContextSettings(message);routing.ingestProviderRouting(message);});ctx.setExternalContextSettingsConnection('connected');routing.setProviderRoutingConnection('connected');setSessionConnection('connected');ingestSessions({type:'chat:sessions',active_id:'fixture-chat',items:[{id:'fixture-chat',title:'Retained analysis session'}]});ctx.chooseContextChat('fixture-chat');selectSettingsCategory('session-context');window.fixturePage=selectSettingsCategory;window.fixtureRoutingDraft=routing.editProviderRouting;function App(){const state=useAppState();return <SettingsOverlay category={state.settingsCategory} onClose={()=>{}}/>}createRoot(document.getElementById('root')).render(<App/>);`;
buildSync({stdin:{contents:entry,resolveDir:root,loader:'tsx'},bundle:true,format:'iife',outfile:path.join(temp,'renderer.js'),jsx:'automatic'});
const css=['design-system','shell','goals','settings','workbench','workspace-overlays','refinement'].map(name=>fs.readFileSync(path.join(root,`frontend/main-deck/src/styles/${name}.css`),'utf8')).join('\n');
fs.writeFileSync(path.join(temp,'index.html'),`<!doctype html><html><head><meta charset="utf-8"><style>${css}\nhtml,body,#root{margin:0;width:100%;height:100%;background:#0b0b0b;}</style></head><body class="app-shell"><div id="root"></div><script src="renderer.js"></script></body></html>`);
fs.writeFileSync(path.join(temp,'preload.cjs'),`const {contextBridge,ipcRenderer}=require('electron');contextBridge.exposeInMainWorld('variant1Deck',{chooseContextExportPath:format=>ipcRenderer.invoke('context-export:choose-path',format)});contextBridge.exposeInMainWorld('fixtureTransport',{send:command=>ipcRenderer.send('fixture:send',command),onResult:callback=>ipcRenderer.on('fixture:response',(_event,value)=>callback(value))});`);
fs.writeFileSync(path.join(temp,'main.cjs'),`
const {app,BrowserWindow,ipcMain,dialog}=require('electron'),fs=require('node:fs'),assert=require('node:assert/strict');
app.setPath('userData',${JSON.stringify(path.join(temp,'profile'))});app.disableHardwareAcceleration();
let win,policy={enabled:false,max_attempts:4,max_wait_seconds:60,fallback_routes:[],auxiliary_routes:{}},revision='r1';
const status={view_id:'frozen-view',counts:{message:1,cell:1,snapshot:0},coverage:{messages:'retained canonical text',cells:'source and result',snapshots:'metadata only'},watermarks:{ledger_upper_sequence:7}};
const pause=ms=>new Promise(r=>setTimeout(r,ms)),run=code=>win.webContents.executeJavaScript(code);
async function wait(code){for(let i=0;i<160;i++){if(await run(code))return;await pause(40);}throw new Error('Timeout '+code);}
const capture=async name=>{await wait('(()=>{let node=document.querySelector(".settings-overlay");while(node){if(Number(getComputedStyle(node).opacity)<0.99)return false;node=node.parentElement;}return true;})()');await pause(100);fs.writeFileSync(${JSON.stringify(out)}+'/'+name,(await win.webContents.capturePage()).toPNG());};
app.whenReady().then(async()=>{
 win=new BrowserWindow({show:false,width:1180,height:900,webPreferences:{preload:${JSON.stringify(path.join(temp,'preload.cjs'))},contextIsolation:true,sandbox:true,offscreen:true,backgroundThrottling:false}});
 dialog.showSaveDialog=async()=>({canceled:false,filePath:${JSON.stringify(path.join(temp,'context.jsonl'))}});
 require(${JSON.stringify(path.join(root,'electron-deck-ipc.js'))}).registerDeckIpc({app,appRoot:${JSON.stringify(root)},getDeckWindow:()=>win,getMonitorWindow:()=>null,isTrustedIpcSender:(event,target)=>!!target && event.sender===target.webContents,readSettings:()=>({}),log:()=>{}});
 ipcMain.on('fixture:send',(event,message)=>{
   if(message.type.startsWith('provider-recovery:')){const operation=message.type.split(':')[1];if(operation==='set'){assert.equal(message.expected_revision,revision);policy=message.config;revision='r2';}event.sender.send('fixture:response',{type:'provider-recovery:result',request_id:message.request_id,operation,ok:true,result:{config:policy,revision,profiles:['internal_json','internal_prose','vision']}});return;}
   if(message.type!=='external-context:request')return;
   const operation=message.operation;let result;
   if(operation==='children')result={items:[{child_id:'fixture-child',name:'Goal analysis worker',status:'completed'}],truncated:false};
   else if(operation==='views')result={items:[{view_id:'frozen-view',created_at:1791028800}],has_more:false,next_cursor:null};
   else if(operation==='status')result=status;
   else if(operation==='read' || operation==='search')result={view_id:'frozen-view',items:[{source_id:'source-message',kind:'message',role:'user',ordinal:0,preview:'Compare the measurements and preserve the result.'},{source_id:'source-cell',kind:'cell',ordinal:1,preview:'summary = {"mean": 17.2}'}],has_more:false,next_cursor:null,coverage:'Message and cell text; snapshots have metadata-only search.'};
   else if(operation==='expand')result={view_id:'frozen-view',source:{source_id:message.source_id,kind:'cell',sequence:7},text:message.part==='result'?'{"mean": 17.2}':'summary = {"mean": 17.2}',offset:0,total_chars:25,has_more:false,next_offset:null};
   else if(operation==='capture' || operation==='refresh')result={view_id:'frozen-view',status};
   else if(operation==='export'){fs.writeFileSync(message.path,'{"type":"fixture-source","text":"Retained evidence"}\\n');result={view_id:'frozen-view',path:message.path,bytes:fs.statSync(message.path).size,source_count:2,omission_count:0,omissions:[]};}
   event.sender.send('fixture:response',{type:'external-context:result',request_id:message.request_id,operation,chat_id:message.chat_id,ok:true,result});
 });
 await win.loadFile(${JSON.stringify(path.join(temp,'index.html'))});
 await wait('[...document.querySelectorAll("select")].find(el=>el.getAttribute("aria-label")==="Saved context view")?.querySelector("option[value=frozen-view]")');
 await run('(()=>{const select=[...document.querySelectorAll("select")].find(el=>el.getAttribute("aria-label")==="Saved context view");select.value="frozen-view";select.dispatchEvent(new Event("change",{bubbles:true}));})()');
 await wait('document.querySelectorAll(".context-sources > button").length===2');
 await run('document.querySelectorAll(".context-sources > button")[1].click()');
 await wait('document.querySelector(".context-detail pre")?.textContent.includes("summary")');await pause(80);await capture('context-browser.png');
 await run('document.querySelector(".context-browser").scrollIntoView({block:"center"})');await capture('context-browser-detail.png');
 await run('[...document.querySelectorAll("button")].find(x=>x.textContent==="Export view").click()');
 await wait('document.body.textContent.includes("Context export saved.")');assert.ok(fs.existsSync(${JSON.stringify(path.join(temp,'context.jsonl'))}));
 await run('window.fixturePage("provider-routing")');await wait('!!document.querySelector(".provider-routing-settings [role=switch]")');
 assert.equal(await run('document.querySelector(".provider-routing-settings [role=switch]").getAttribute("aria-checked")'),"false");
 await run('document.querySelector(".provider-routing-settings [role=switch]").click()');
 await wait('document.querySelector(".provider-routing-settings [role=switch]").getAttribute("aria-checked")==="true"');
 await run('window.fixtureRoutingDraft({enabled:true,max_attempts:4,max_wait_seconds:60,fallback_routes:[{mode:"cloud",provider:"openrouter",model:"stealth/space-bunny-alpha",reasoning_effort:"max"},{mode:"local",provider:"local",model:"local-worker.gguf"}],auxiliary_routes:{internal_prose:[{mode:"cloud",provider:"openrouter",model:"stealth/space-bunny-alpha",reasoning_effort:"minimal"}]}})');
 await wait('document.querySelectorAll(".routing-route").length===3');
 await run('[...document.querySelectorAll("button")].find(x=>x.textContent==="Save routing").click()');await wait('document.body.textContent.includes("Routing settings saved.")');await capture('model-routing.png');
 assert.equal(policy.enabled,true,'enabled draft was durably acknowledged');
 assert.equal(await run('document.querySelector(".provider-routing-settings [role=switch]").getAttribute("aria-checked")'),"true");
 win.setSize(700,820);await pause(100);await capture('model-routing-narrow.png');
 assert.equal(await run('(()=>{const p=document.querySelector(".settings-page-body");return p.scrollWidth<=p.clientWidth;})()'),true,'routing page fits narrow settings');
 await run('window.fixturePage("session-context")');await wait('document.querySelectorAll(".context-sources > button").length===2');await capture('context-browser-narrow.png');
 assert.equal(await run('(()=>{const p=document.querySelector(".settings-page-body");return p.scrollWidth<=p.clientWidth;})()'),true,'context browser fits narrow settings');
 console.log('Native Settings: frozen source detail, native export IPC/receipt, default-off acknowledged routing save and narrow layouts passed');
}).catch(error=>{console.error(error);process.exitCode=1;}).finally(()=>app.exit(process.exitCode||0));
`);
const child=spawn(require('electron'),[path.join(temp,'main.cjs')],{cwd:root,windowsHide:true,stdio:'inherit'});
const timer=setTimeout(()=>child.kill(),45000);
child.once('exit',code=>{clearTimeout(timer);process.exitCode=code===0?0:1;const target=path.resolve(temp);if(path.dirname(target)===path.resolve(os.tmpdir()) && path.basename(target).startsWith('variant-context-settings-'))fs.rmSync(target,{recursive:true,force:true,maxRetries:4,retryDelay:150});});
