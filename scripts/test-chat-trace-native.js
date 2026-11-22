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
const run=code=>win.webContents.executeJavaScript(code).catch(error=>{throw new Error(code+' — '+error.message);});
async function wait(code){for(let i=0;i<240;i++){if(await run(code))return;await pause(50);}throw new Error('Timed out: '+code);}
async function capture(name){await pause(250);fs.writeFileSync(${JSON.stringify(out)}+'/'+name,(await win.webContents.capturePage()).toPNG());}
app.whenReady().then(async()=>{
 boot.registerVariant1Protocol(protocol,${JSON.stringify(root)});
 server=new WebSocketServer({port:0,host:'127.0.0.1'});await new Promise(r=>server.once('listening',r));
 const info={port:server.address().port,token:'trace-fixture'};
 const peer={message_id:'fixture-peer-request',exchange_id:'fixture-exchange',sender_peer_id:'chat:trace-fixture',
  target_peer_id:'chat:reader',target_display_name:'Reader',message_kind:'request',content:'Please check the file count.',
  state:'queued',revision:1,sender_invocation:{chat_id:'trace-fixture',run_id:'fixture-run',outer_tool_call_id:'fixture-live-cell'}};
 let transcript=${JSON.stringify(messages)};
 const progressRun='fixture-progress-off';
 let annotatedCells;
 server.on('connection',socket=>{sockets.push(socket);const send=value=>socket.send(JSON.stringify(value));
  socket.on('message',raw=>{const m=JSON.parse(String(raw));
   if(m.type==='chat:sessions')send({type:'chat:sessions',active_id:'trace-fixture',items:[{id:'trace-fixture',title:'Inspect the browser'}]});
   if(m.type==='chat:session:get')send({type:'chat:session',session:{id:'trace-fixture',title:'Inspect the browser',messages:transcript,runtime:{busy:false,kernel:{state:'idle',generation:3}}}});
   if(m.type==='peers:inspect' && m.message_id===peer.message_id)send({type:'peers:result',chat_id:m.chat_id,request_id:m.request_id,operation:'inspect',ok:true,result:peer});
   if(m.type==='execution:get')send({type:'execution:snapshot',chat_id:'trace-fixture',terminals:[],processes:[]});
   if(m.type==='chat:session:annotate' && m.run_id===progressRun){
    annotatedCells=m.steps;
    send({type:'chat:session:annotated',id:m.id,run_id:m.run_id,request_id:m.request_id,ok:true});
   }
   if(m.type==='browser:host:register')send({type:'browser:host:registered'});
   if(m.type==='chat')throw new Error('Fixture must not submit chat');
  });
 });
 ipcMain.handle('fixture:backend',()=>info);
 win=new BrowserWindow({show:false,width:1440,height:960,webPreferences:{preload:${JSON.stringify(path.join(temp,"preload.cjs"))},contextIsolation:true,sandbox:true,nodeIntegration:false}});
 await win.loadURL('variant1://app/frontend/main-deck/index.html');
 await wait('!!document.querySelector(".turn-timeline") && !document.querySelector(".startup-cover")');
 win.showInactive();await run('document.fonts.ready.then(()=>true)');
 assert.equal(await run('document.querySelector(".execution-trace__owner")===null'),true);
 assert.equal(await run('document.querySelectorAll(".turn-timeline > .trace-entry--thought").length'),2,'each thought sits in the timeline where it happened');
 await run('document.querySelector(".trace-entry--thought button").click()');
 await capture('chat-traces.png');
 const codeStyle=await run('(()=>{const s=getComputedStyle(document.querySelector(".message__content code"));return {font:s.fontFamily,weight:s.fontWeight};})()');
 assert.match(codeStyle.font,/Geist Mono/);assert.equal(codeStyle.weight,'400');
 win.setSize(600,880);await pause(200);
 assert.equal(await run('(()=>{const t=document.querySelector(".turn-timeline");return t.scrollWidth<=t.clientWidth;})()'),true,'compact traces fit the chat');
 await capture('chat-traces-compact.png');
 win.setSize(1440,960);
 const route={source:'chat',session_id:'trace-fixture',admission_id:'fixture-admission',run_id:'fixture-run'};
 const emit=frame=>sockets.forEach(socket=>socket.send(JSON.stringify({...route,...frame})));
 emit({type:'start'});
 const summary={type:'thinking',summary_id:'summary_0123456789abcdef0123456789abcdef',summary_source:'provider_reasoning',ts:Date.now()};
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
 const motion=await run('(()=>{const node=document.querySelector(".trace-entry.is-running .trace-progress i"),animation=node.getAnimations()[0],playing=animation.playState;animation.pause();animation.currentTime=0;const low=getComputedStyle(node).transform;animation.currentTime=500;const high=getComputedStyle(node).transform;animation.play();return {playing,low,high};})()');
 assert.equal(motion.playing,'running');assert.notEqual(motion.low,motion.high,'the live animation changes the pulse in an occluded native window');
 await run('document.documentElement.dataset.motion="reduced"');
 assert.equal(await run('getComputedStyle(document.querySelector(".trace-entry.is-running .trace-progress i")).animationName'),'none');
 await run('document.documentElement.dataset.motion="system"');
 emit({type:'tool:activity',event:'tool:result',tool:'ipython',call_id:'fixture-live-cell',status:'ok',text:'Page ready',duration_ms:350});
 await wait('!document.querySelector(".trace-entry.is-running") && !!document.querySelector(".execution-trace__waiting")');
 // Expand both explicitly. The DOM remounts when the optimistic turn settles.
 const live='.chat-turn--live';
 await run('document.querySelector("'+live+' .tool-run .trace-entry__disclosure").click()');
 await run('document.querySelector("'+live+' .trace-entry--thought .trace-entry__disclosure").click()');
 await run('document.querySelector("'+live+' .trace-entry--thought .trace-entry__disclosure").click()');
 emit({type:'activity',event:'peer:sent',call_id:'fixture-send',peer_message:peer});
 await wait('document.querySelector("'+live+' .peer-send-trace summary")?.textContent.includes("Queued")');
 assert.equal(await run('document.querySelector("'+live+'").textContent.includes("Waiting for peer reply")'),false,'queue admission does not imply a wait');
 await run('document.querySelector("'+live+' .peer-send-trace summary").click()');
 const call={type:'tool:activity',tool:'ipython',call_id:'fixture-wait-cell'};
 emit({...call,event:'tool:start',status:'running',args_preview:JSON.stringify({code:'request.wait(timeout_s=5)'})});
 const waiting={type:'activity',event:'peer:wait',outer_call_id:call.call_id,call_id:'fixture-nested-wait',message_id:peer.message_id};
 emit({...waiting,status:'running'});
 await wait('document.querySelector("'+live+' .trace-entry.is-running .trace-entry__name")?.textContent==="Waiting for peer reply"');
 assert.equal(await run('document.querySelectorAll("'+live+' .trace-progress").length'),1);
 await capture('peer-wait.png');
 peer.state='replied';peer.revision++;
 emit({type:'peer:changed',chat_id:'trace-fixture',message_id:'fixture-correlated-reply',revision:peer.revision});
 emit({...waiting,status:'done'});
 emit({...call,event:'tool:result',status:'ok',text:'Reply received',duration_ms:120});
 await wait('document.querySelector("'+live+' .peer-send-trace summary")?.textContent.includes("Reply received")');
 assert.equal(await run('document.querySelectorAll("'+live+' .execution-trace__waiting .trace-progress").length'),1);
 emit({type:'done',text:'The page is ready.'});
 await wait('!document.querySelector(".trace-progress")');
 const chosen=s=>'(()=>{const t=[...document.querySelectorAll(".chat-response-segment")].at(-1);return !!t && '+s+'})()';
 const retained=chosen('t.querySelector(".trace-entry--thought button")?.ariaExpanded==="true" && t.querySelector(".tool-run button.trace-entry__disclosure")?.ariaExpanded==="true" && t.querySelector(".peer-send-trace")?.open');
 assert.equal(await run(retained),true,'explicit thought, tool and peer disclosures survive done');
 const saved={role:'assistant',text:'The page is ready.',run_id:route.run_id,peer_sent:[peer],steps:[
   {id:summary.summary_id,kind:'thinking',source:summary.summary_source,status:'done',summary_revision:3,label:'Thought',detail:'Browser connection checked.'},
   {id:call.call_id+'-first',call_id:'fixture-live-cell',kind:'tool',tool:'ipython',label:'Python',status:'ok',args_preview:'{"code":"page.read()"}',result_preview:'Page ready'},
   {id:call.call_id,call_id:call.call_id,kind:'tool',tool:'ipython',label:'Python',status:'ok',args_preview:'{"code":"request.wait()"}',result_preview:'Reply received'}]};
 transcript=[...transcript,{role:'user',text:'Check with Reader.'},saved];
 // Replace the visible transcript, then reopen its saved history through the normal session snapshot.
 const session=messages=>({type:'chat:session',session:{id:'trace-fixture',title:'Inspect the browser',messages,runtime:{busy:false}}});
 emit(session([]));await wait('!document.querySelector(".turn-timeline")');
 emit(session(transcript));await wait(retained);
 assert.equal(await run(chosen('t.querySelectorAll(".peer-send-trace").length===1 && !t.querySelector(".trace-progress")')),true);
 assert.equal(await run(chosen('t.querySelector(".peer-send-trace summary").textContent.includes("Reply received")')),true);
 const widths=await run(chosen('({row:t.querySelector(".peer-send-trace summary").getBoundingClientRect().width,timeline:t.querySelector(".turn-timeline").clientWidth,border:getComputedStyle(t.querySelector(".peer-send-trace")).borderTopWidth})'));
 assert.ok(widths.row<widths.timeline*.85 && widths.border==='0px','peer disclosure stays text-width and borderless');
 await capture('settled-peer-traces.png');
 // Use a short settled turn to check normal reading density and native keyboard disclosure.
 const reviewSteps=[
   {id:'narration',kind:'text',label:'Narration',detail:'I’ll check the architecture guide and the repository status.',status:'done'},
   {id:'read',kind:'tool',label:'Read file',tool:'read_file',args_preview:JSON.stringify({path:'docs/ARCHITECTURE.md'}),result_preview:'The desktop renderer is the Main Deck.',status:'ok'},
   ...['git status','git log -1'].map((command,index)=>({id:'cmd-'+index,kind:'tool',label:'Command',tool:'run_command',args_preview:JSON.stringify({command}),status:'ok'})),
   {id:'brief-thought',kind:'thinking',source:'provider_summary',label:'Thought',detail:'The guide and the checked-out files agree.',status:'done'}];
 emit(session([{role:'user',text:'Where does the desktop renderer start?'},
  {role:'assistant',run_id:'review-fixture',text:'The Main Deck renderer starts in frontend/main-deck/src/main.tsx.',steps:reviewSteps}]));
 await wait('document.querySelector(".execution-trace__summary")?.textContent.includes("Read ARCHITECTURE.md, ran 2 commands")');
 // Exercise Chromium's native button activation without taking foreground focus on Windows.
 const input=win.webContents.debugger;input.attach('1.3');
 await input.sendCommand('Emulation.setFocusEmulationEnabled',{enabled:true});
 await run('document.querySelector(".execution-trace__summary").focus()');
 assert.ok(await run('document.activeElement.matches(".execution-trace__summary")'));
 const enter=async()=>{const key={key:'Enter',code:'Enter',windowsVirtualKeyCode:13};await input.sendCommand('Input.dispatchKeyEvent',{...key,type:'keyDown',text:String.fromCharCode(13)});await input.sendCommand('Input.dispatchKeyEvent',{...key,type:'keyUp'});};
 await enter();await wait('document.querySelector(".execution-trace__summary")?.ariaExpanded==="true"');
 await enter();await wait('document.querySelector(".execution-trace__summary")?.ariaExpanded==="false"');
 input.detach();
 await run('document.querySelector(".execution-trace__summary").blur()');
 await capture('inline-traces.png');
 // Only ordinary assistant tokens accompany these tool calls. A silent call stays silent.
 const off=frame=>emit({...frame,run_id:progressRun,admission_id:progressRun+'-admission'});
 const notes=['I’ll read the process logs.','The lookup failed. I’ll check the process list.'];
 const expected=[notes[0],'Read process logs','Python cell failed',notes[1],'Listed processes'];
 const timeline=root=>'[...document.querySelectorAll("'+root+' .turn-narration, '+root+' .trace-entry__name")].map(n=>n.textContent.trim())';
 const activity=(id,event,fields)=>off({type:'tool:activity',tool:'ipython',call_id:id,event,...fields});
 off({type:'start'});
 const rawFieldMarker='UNRENDERED_PROVIDER_FIXTURE_FIELD';
 off({type:'token',segment:1,token:notes[0],reasoning_content:rawFieldMarker});
 await wait('document.querySelector(".chat-turn--live")?.textContent.includes('+JSON.stringify(notes[0])+')');
 assert.equal(await run('document.querySelectorAll(".chat-turn--live .trace-entry--thought").length'),0,'narration renders without inventing a public Thought');
 assert.equal(await run('document.body.textContent.includes('+JSON.stringify(rawFieldMarker)+')'),false);
 activity('logs','tool:start',{status:'running',args_preview:JSON.stringify({code:'proc = handles[0]; print(proc.read())'})});
 activity('logs','tool:result',{status:'ok',cell_action:'Read process logs',text:'ready'});
 await wait('document.querySelector(".chat-turn--live .trace-entry__name")?.textContent==="Read process logs"');
 activity('lookup','tool:start',{status:'running',args_preview:JSON.stringify({code:'handle = handles[8]'})});
 activity('lookup','tool:result',{status:'error',text:'IndexError'});
 off({type:'token',segment:3,token:notes[1]});
 activity('retry','tool:start',{status:'running',args_preview:JSON.stringify({code:'args = {"mode":"processes"}; list_processes(**args)'})});
 await wait('document.querySelectorAll(".chat-turn--live .turn-narration").length===2 && !!document.querySelector(".chat-turn--live .trace-entry.is-running .trace-progress")');
 await capture('thinking-off-progress-live.png');
 off({type:'clarification:request',chat_id:'trace-fixture',id:'fixture-question',questions:[
   {id:'source',header:'Process logs',question:'Which process should I inspect?',options:[{label:'Server'}]}]});
 await wait('document.querySelector(".runtime-clarification__question")?.textContent==="Which process should I inspect?" && !document.querySelector(".chat-turn--live .trace-progress")');
 await capture('awaiting-answer.png');
 off({type:'clarification:closed',chat_id:'trace-fixture',id:'fixture-question',status:'resolved'});
 await wait('!document.querySelector(".runtime-clarification__question") && !!document.querySelector(".chat-turn--live .trace-progress")');
 activity('retry','tool:result',{status:'ok',cell_action:'Listed processes',text:'one process'});
 await wait('JSON.stringify('+timeline('.chat-turn--live')+')==='+JSON.stringify(JSON.stringify(expected)));
 off({type:'activity',event:'task:done',status:'ok',text:'The check is complete.'});
 off({type:'done',durable:true,text:'The check is complete.'});
 await wait('!document.querySelector(".chat-turn--live")');
 for(let n=0;!annotatedCells && n<50;n++)await pause(50);
 assert.deepEqual(annotatedCells.map(s=>[s.callId,s.cellAction]),[['logs','Read process logs'],['lookup',undefined],['retry','Listed processes']]);
 const cells=annotatedCells.map(s=>({...s,call_id:s.callId,args_preview:s.argsPreview,result_preview:s.resultPreview,cell_action:s.cellAction}));
 const note=(index,segment)=>({id:'text:'+segment,kind:'text',label:'Narration',status:'done',detail:notes[index],segment});
 emit(session([]));await wait('!document.querySelector(".turn-timeline")');
 emit(session([{role:'user',text:'Check the process.'},{role:'assistant',run_id:progressRun,text:'The check is complete.',reasoning_content:rawFieldMarker,
      steps:[note(0,1),cells[0],cells[1],note(1,3),cells[2]]}]));
 await wait('JSON.stringify('+timeline('.turn-timeline')+')==='+JSON.stringify(JSON.stringify(expected)));
 assert.equal(await run('document.querySelectorAll(".trace-entry--thought,.trace-progress").length'),0);
 assert.equal(await run('document.body.textContent.includes('+JSON.stringify(rawFieldMarker)+')'),false,'reopened history renders public transcript fields');
 assert.equal(await run('document.querySelector(".turn-timeline").textContent.includes("The check is complete.")'),false);
 await run('document.querySelector(".tool-run .trace-entry__disclosure").click()');
 assert.ok((await run('document.querySelector(".trace-entry__body").textContent')).includes('proc.read()'));
 await run('document.querySelector(".execution-trace__summary").click()');
 await capture('thinking-off-progress.png');
 await wait('document.querySelector(".message-results summary")?.textContent.includes("3 tool results · 1 failed")');
 await run('document.querySelector(".message-results summary").click()');
 await wait('document.querySelectorAll(".message-results li").length===3');
 assert.deepEqual(await run('[...document.querySelectorAll(".message-results pre")].map(el=>el.textContent)'),['ready','IndexError','one process']);
 await capture('recorded-tool-results.png');
 win.setSize(1440,420);await pause(150);
 await run('document.querySelector(".history-item__menu").click()');
 await wait('!!document.querySelector("#runtime-session-menu")');
 assert.equal(await run('(()=>{const r=document.querySelector("#runtime-session-menu").getBoundingClientRect();return r.top>=8&&r.bottom<=innerHeight-8&&r.left>=8&&r.right<=innerWidth-8;})()'),true,'full session menu fits near the bottom of a short native window');
 await capture('history-menu-bounds.png');
 fs.writeFileSync(${JSON.stringify(path.join(out,"native-checks.json"))},JSON.stringify({anonymousMain:true,thoughtsVisible:true,codeStyle,compactFits:true,correlatedWait:true,replyReceipt:true,disclosuresRetained:true,keyboardToggle:true,peerRowWidths:widths,thinkingOffProgress:expected},null,2));
 console.log('Native chat traces passed streaming, peer wait and reply, saved disclosure choices, keyboard toggles, compact layout and reduced motion.');
}).catch(error=>{console.error(error);process.exitCode=1;}).finally(()=>{for(const socket of sockets)socket.terminate();if(server)server.close();app.exit(process.exitCode||0);});
`);
const child=spawn(require("electron"),[path.join(temp,"main.cjs")],{windowsHide:true,stdio:"inherit",cwd:root});
const timer=setTimeout(()=>child.kill(),45000);
child.once("exit",code=>{clearTimeout(timer);process.exitCode=code===0?0:1;const target=path.resolve(temp);if(path.dirname(target)===path.resolve(os.tmpdir())&&path.basename(target).startsWith("variant-trace-ui-"))fs.rmSync(target,{recursive:true,force:true,maxRetries:4,retryDelay:150});});
