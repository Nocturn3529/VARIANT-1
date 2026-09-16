"use strict";
// Isolated app renderer and deterministic transcript; no model or external browser calls.
const fs = require("node:fs"), path = require("node:path"), os = require("node:os"), {spawn} = require("node:child_process");
const root = path.resolve(__dirname, ".."), temp = fs.mkdtempSync(path.join(os.tmpdir(), "variant-trace-ui-"));
const out = process.env.VARIANT1_TRACE_TEST_ARTIFACTS || path.join(root, "artifacts/frontend-chat-traces-2026-09-15");
fs.mkdirSync(out, {recursive: true});
const steps = [
  {id: "thought-fixture-1", kind: "thinking", label: "Thinking", detail: "I’ll check which browser is connected, then open the page in this chat’s browser.", status: "done", duration_ms: 1800},
  {id: "cell-fixture-1", kind: "tool", tool: "ipython", label: "Python", status: "ok", args_preview: JSON.stringify({code: "page = browser.session.new_page('https://example.com')\npage.read()"}), result_preview: JSON.stringify({execution_count: 12, kernel_generation: 3, title: "Example Domain"}), duration_ms: 320},
  {id: "thought-fixture-2", kind: "thinking", label: "Thinking", detail: "The page is available. I’ll inspect its contents before continuing.", status: "done", duration_ms: 900},
  {id: "cell-fixture-2", kind: "tool", tool: "ipython", label: "Python", status: "error", args_preview: '{"code":"page.read(include_screenshot=True)', result_preview: "TAB_NOT_FOUND — Requested browser tab is not open", duration_ms: 254},
];
const messages = [{role: "user", text: "Open the page in the built-in browser and check what’s there."}, {role: "assistant", text: "The first page opened successfully. The next read returned `TAB_NOT_FOUND` because that tab was no longer available.\n\nThe Python workspace still contains `page`.", steps}];
fs.writeFileSync(path.join(temp, "preload.cjs"), "const {contextBridge,ipcRenderer}=require('electron');contextBridge.exposeInMainWorld('variant1Deck',{getBackendInfo:()=>ipcRenderer.invoke('fixture:backend')});");
fs.writeFileSync(path.join(temp, "main.cjs"), `
const {app,BrowserWindow,ipcMain,protocol}=require('electron');
const {WebSocketServer}=require(${JSON.stringify(require.resolve("ws"))});
const fs=require('node:fs'),assert=require('node:assert/strict');
const boot=require(${JSON.stringify(path.join(root,"electron-app-boot.js"))});
boot.applyGpuFlags(app);boot.registerVariant1Scheme(protocol);app.setPath('userData',${JSON.stringify(path.join(temp,"profile"))});
const pause=ms=>new Promise(r=>setTimeout(r,ms));let win,server;const sockets=[];
const run=code=>win.webContents.executeJavaScript(code);
async function wait(code){for(let i=0;i<240;i++){if(await run(code))return;await pause(50);}throw new Error('Timed out: '+code);}
async function capture(name){await pause(250);fs.writeFileSync(${JSON.stringify(out)}+'/'+name,(await win.webContents.capturePage()).toPNG());}
app.whenReady().then(async()=>{
 boot.registerVariant1Protocol(protocol,${JSON.stringify(root)});
 server=new WebSocketServer({port:0,host:'127.0.0.1'});await new Promise(r=>server.once('listening',r));
 const info={port:server.address().port,token:'trace-fixture'};
 server.on('connection',socket=>{sockets.push(socket);const send=value=>socket.send(JSON.stringify(value));
  socket.on('message',raw=>{const m=JSON.parse(String(raw));
   if(m.type==='chat:sessions')send({type:'chat:sessions',active_id:'trace-fixture',items:[{id:'trace-fixture',title:'Inspect the browser'}]});
   if(m.type==='chat:session:get')send({type:'chat:session',session:{id:'trace-fixture',title:'Inspect the browser',messages:${JSON.stringify(messages)},runtime:{busy:false,kernel:{state:'idle',generation:3}}}});
   if(m.type==='execution:get')send({type:'execution:snapshot',chat_id:'trace-fixture',terminals:[],processes:[]});
   if(m.type==='browser:host:register')send({type:'browser:host:registered'});
   if(m.type==='chat')throw new Error('Fixture must not submit chat');
  });
 });
 ipcMain.handle('fixture:backend',()=>info);
 win=new BrowserWindow({show:false,width:1180,height:960,webPreferences:{preload:${JSON.stringify(path.join(temp,"preload.cjs"))},contextIsolation:true,sandbox:true,nodeIntegration:false}});
 await win.loadURL('variant1://app/frontend/main-deck/index.html');
 await wait('!!document.querySelector(".execution-trace") && !document.querySelector(".startup-cover")');
 win.showInactive();await run('document.fonts.ready.then(()=>true)');
 assert.equal(await run('document.querySelector(".execution-trace__owner")===null'),true);
 assert.match(await run('document.querySelector(".execution-trace__counts").textContent'),/2 thoughts/);
 await run('document.querySelector(".trace-entry--thought button").click()');
 await capture('chat-traces.png');
 const codeStyle=await run('(()=>{const s=getComputedStyle(document.querySelector(".message__content code"));return {font:s.fontFamily,weight:s.fontWeight};})()');
 assert.match(codeStyle.font,/Geist Mono/);assert.equal(codeStyle.weight,'400');
 win.setSize(600,880);await pause(200);
 assert.equal(await run('(()=>{const t=document.querySelector(".execution-trace");return t.scrollWidth<=t.clientWidth;})()'),true,'compact traces fit the chat');
 await capture('chat-traces-compact.png');
 win.setSize(1180,960);
 const route={source:'chat',session_id:'trace-fixture',admission_id:'fixture-admission',run_id:'fixture-run'};
 const emit=frame=>sockets.forEach(socket=>socket.send(JSON.stringify({...route,...frame})));
 emit({type:'start'});
 const summary={type:'thinking',summary_id:'summary_0123456789abcdef0123456789abcdef',summary_source:'provider_summary',ts:Date.now()};
 emit({...summary,status:'running',summary_revision:1,text:'**Inspecting the browser**'});
 await wait('!!document.querySelector(".trace-entry.is-running .trace-thought-content strong")');
 await run('window.liveThought=document.querySelector(".trace-entry.is-running .trace-thought-content")');
 emit({...summary,status:'running',summary_revision:2,text:${JSON.stringify("**Inspecting the browser**\n\nI’ll check the connection, then open a fresh page in this chat.")}});
 await wait('window.liveThought.textContent.includes("fresh page")');
 assert.equal(await run('window.liveThought===document.querySelector(".trace-entry.is-running .trace-thought-content")'),true,'streaming updates preserve the same thought body');
 await run(${JSON.stringify('document.querySelector(\'[aria-label="Show right panel"]\')?.click()')});
 await pause(150);await run('document.querySelector(".trace-entry.is-running").scrollIntoView({block:"center"})');
 await capture('live-thoughts-seamless.png');
 const shell=await run('(()=>{const style=s=>getComputedStyle(document.querySelector(s));return {title:style(".titlebar").borderBottomWidth,footer:style(".section-footer").borderTopWidth,history:style(".history-panel").borderRightWidth,sash:style(".workbench-sash").backgroundColor};})()');
 assert.deepEqual(shell,{title:'0px',footer:'0px',history:'0px',sash:'rgba(0, 0, 0, 0)'},'resting shell has no drawn pane dividers');
 emit({...summary,status:'done',summary_revision:3,text:${JSON.stringify("**Browser connection checked**\n\nI’ll open a fresh browser page in this chat.")}});
 emit({type:'tool:activity',event:'tool:start',tool:'ipython',call_id:'fixture-live-cell',status:'running',args_preview:JSON.stringify({code:"page = browser.navigate('https://example.com')"})});
 await wait('!!document.querySelector(".trace-entry.is-running .trace-progress")');
 assert.equal(await run('window.liveThought.isConnected'),true,'completed thought retains its preview');
 assert.equal(await run('document.querySelectorAll("[data-trace-id=summary_0123456789abcdef0123456789abcdef]").length'),1,'routed provider summary appears once');
 await run('document.querySelector(".trace-entry.is-running").scrollIntoView({block:"center"})');
 await capture('chat-traces-working.png');
 const pulse=()=>run('getComputedStyle(document.querySelector(".trace-entry.is-running .trace-progress i")).transform');
 const before=await pulse();await pause(170);assert.notEqual(await pulse(),before,'active step animates');
 await run('document.documentElement.dataset.motion="reduced"');
 assert.equal(await run('getComputedStyle(document.querySelector(".trace-entry.is-running .trace-progress i")).animationName'),'none');
 await run('document.documentElement.dataset.motion="system"');
 emit({type:'tool:activity',event:'tool:result',tool:'ipython',call_id:'fixture-live-cell',status:'ok',text:'Page ready',duration_ms:350});
 await wait('!document.querySelector(".trace-entry.is-running") && !!document.querySelector(".execution-trace__waiting")');
 emit({type:'done',text:'The page is ready.'});
 await wait('!document.querySelector(".trace-progress")');
 win.setSize(1180,420);await pause(150);
 await run('document.querySelector(".history-item__menu").click()');
 await wait('!!document.querySelector("#runtime-session-menu")');
 assert.equal(await run('(()=>{const r=document.querySelector("#runtime-session-menu").getBoundingClientRect();return r.top>=8&&r.bottom<=innerHeight-8&&r.left>=8&&r.right<=innerWidth-8;})()'),true,'full session menu fits near the bottom of a short native window');
 await capture('history-menu-bounds.png');
 fs.writeFileSync(${JSON.stringify(path.join(out,"native-checks.json"))},JSON.stringify({anonymousMain:true,thoughtsVisible:true,codeStyle,compactFits:true},null,2));
 console.log('Native chat traces: unnamed main, routed summaries, code typography, compact fit, active animation, reduced motion and settled state passed');
}).catch(error=>{console.error(error);process.exitCode=1;}).finally(()=>{for(const socket of sockets)socket.terminate();if(server)server.close();app.exit(process.exitCode||0);});
`);
const child=spawn(require("electron"),[path.join(temp,"main.cjs")],{windowsHide:true,stdio:"inherit",cwd:root});
const timer=setTimeout(()=>child.kill(),45000);
child.once("exit",code=>{clearTimeout(timer);process.exitCode=code===0?0:1;const target=path.resolve(temp);if(path.dirname(target)===path.resolve(os.tmpdir())&&path.basename(target).startsWith("variant-trace-ui-"))fs.rmSync(target,{recursive:true,force:true,maxRetries:4,retryDelay:150});});
