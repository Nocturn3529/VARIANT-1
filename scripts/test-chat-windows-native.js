'use strict';
// Isolated desktop UI + protocol fixture; no Python backend or model requests.
const fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {spawn}=require('node:child_process');
const root=path.resolve(__dirname,'..'),temp=fs.mkdtempSync(path.join(os.tmpdir(),'variant1-chat-windows-'));
const out=process.env.VARIANT1_FRONTEND_TEST_ARTIFACTS || path.join(root,'artifacts/frontend-layout-2026-09-13');fs.mkdirSync(out,{recursive:true});
fs.writeFileSync(path.join(temp,'preload.cjs'),`const {contextBridge,ipcRenderer}=require('electron');contextBridge.exposeInMainWorld('variant1Deck',{
 listChatWindows:()=>ipcRenderer.invoke('chat-window:list'),manageChatWindow:(id,action)=>ipcRenderer.invoke('chat-window:manage',id,action),
 onChatWindowsChanged:cb=>{const listener=(_e,value)=>cb(value);ipcRenderer.on('chat-window:changed',listener);return()=>ipcRenderer.removeListener('chat-window:changed',listener);},
 getBackendInfo:()=>ipcRenderer.invoke('test:backend'),openChatWindow:(id,title)=>ipcRenderer.invoke('chat-window:open',id,title)
});`);
fs.writeFileSync(path.join(temp,'main.cjs'),`
const {app,BrowserWindow,ipcMain,protocol}=require('electron');
const {WebSocketServer}=require(${JSON.stringify(require.resolve('ws'))});
const fs=require('node:fs'),assert=require('node:assert/strict');
const boot=require(${JSON.stringify(path.join(root,'electron-app-boot.js'))});
boot.applyGpuFlags(app);boot.registerVariant1Scheme(protocol);app.setPath('userData',${JSON.stringify(path.join(temp,'profile'))});
const pause=ms=>new Promise(r=>setTimeout(r,ms));let main,manager,server;const frames=[],sockets=[];
async function wait(win,expression){for(let i=0;i<400;i++){if(await win.webContents.executeJavaScript(expression))return;await pause(50);}throw new Error('Timed out: '+expression);}
app.whenReady().then(async()=>{
 app.on("browser-window-created",(_event,win)=>win.webContents.on("console-message",event=>console.log("CHAT_TEST_CONSOLE",event.message)));
 boot.registerVariant1Protocol(protocol,${JSON.stringify(root)});
 server=new WebSocketServer({port:0,host:'127.0.0.1'});await new Promise(r=>server.once('listening',r));
 const info={port:server.address().port,token:'isolated-test'};
 server.on('connection',(socket,request)=>{
  const url=new URL(request.url,'http://localhost'),role=url.searchParams.get('view_role'),pinned=url.searchParams.get('view_chat_id');
  sockets.push({socket,role,pinned});let sid=pinned||'main-a';
  const send=value=>socket.send(JSON.stringify(value));
  const session=()=>({id:sid,title:sid==='main-a'?'Main chat':'Detached chat',messages:[],runtime:{busy:false,kernel:{state:'idle',generation:1}}});
  socket.on('message',raw=>{const m=JSON.parse(String(raw));frames.push({role,...m});
   if(m.type==='chat:sessions')send({type:'chat:sessions',active_id:'main-a',items:[{id:'main-a',title:'Main chat',project:{root:'C:/Example/Project',name:'Project'}},{id:'chat-b',title:'Detached chat'},{id:'chat-c',title:'Third chat'}]});
   if(m.type==='chat:session:get')send({type:'chat:session',session:session()});
   if(m.type==='chat:session:switch'){sid=m.id;send({type:'chat:session',session:session(),navigation:{request_id:m.request_id,requested_id:sid,effective_id:sid,status:'switched'}});}
   if(m.type==='browser:host:register')send({type:'browser:host:registered'});
   if(m.type==='execution:get')send({type:'execution:snapshot',chat_id:sid,terminals:[],processes:[]});
  });
 });
 main=new BrowserWindow({show:false,width:1240,height:820,webPreferences:{preload:${JSON.stringify(path.join(temp,'preload.cjs'))},sandbox:true,contextIsolation:true,nodeIntegration:false}});
 let firstInfo=true;ipcMain.handle('test:backend',async()=>{if(firstInfo){firstInfo=false;await pause(14000);}return info;});
 manager=require(${JSON.stringify(path.join(root,'electron-chat-windows.js'))}).createChatWindows({appRoot:${JSON.stringify(root)},getDeckWindow:()=>main,getBackendInfo:()=>info,isTrustedIpcSender:e=>e.sender===main.webContents,hardenAppWindow:()=>{},show:false});
 await main.loadURL('variant1://app/frontend/main-deck/index.html');
 main.showInactive();
 await wait(main,'!!document.querySelector(".startup-cover")');await pause(200);main.webContents.invalidate();await pause(80);
 fs.writeFileSync(${JSON.stringify(path.join(out,'startup.png'))},(await main.webContents.capturePage()).toPNG());
 const firstCells=await main.webContents.executeJavaScript('document.querySelector(".startup-cells").toDataURL()');
 await pause(12600);
 const fullCells=await main.webContents.executeJavaScript('document.querySelector(".startup-cells").toDataURL()');assert.notEqual(firstCells,fullCells,'colony grows during loading');
 fs.writeFileSync(${JSON.stringify(path.join(out,'startup-colony-full.png'))},(await main.webContents.capturePage()).toPNG());
 await wait(main,'!document.querySelector(".startup-cover")');
 console.log("CHAT_TEST_STARTUP_DONE");
 assert.equal(await main.webContents.executeJavaScript(${JSON.stringify("[...document.querySelectorAll('button')].filter(b=>['Flip panes','Detach Chats panel'].includes(b.getAttribute('aria-label'))||b.id==='collapse-history'||b.classList.contains('section-footer__button--panel')).length")}),0);
 main.showInactive();await pause(150);
 const shapes=[];
 await main.webContents.executeJavaScript(${JSON.stringify("[...document.querySelectorAll('button')].find(b=>b.getAttribute('aria-label')==='Edit layout').click()")});
 for(const preset of ['focus','default','terminal-deck','quad']){
  await main.webContents.executeJavaScript('(()=>{const select=document.querySelector(".workbench-editbar select");select.value='+JSON.stringify(preset)+';select.dispatchEvent(new Event("change",{bubbles:true}));})()');await pause(80);
  shapes.push(await main.webContents.executeJavaScript('[...document.querySelectorAll(".workbench-split")].map(e=>e.className).join("|")'));
 }
 assert.equal(new Set(shapes).size,4,'each built-in preset changes the scoped layout');
 await main.webContents.executeJavaScript('(()=>{const select=document.querySelector(".workbench-editbar select");select.value="default";select.dispatchEvent(new Event("change",{bubbles:true}));})()');await pause(100);
 const widthBefore=await main.webContents.executeJavaScript('document.querySelector(".workbench-review").getBoundingClientRect().width');
 await main.webContents.executeJavaScript('(()=>{const x=document.querySelector(".workbench-review").getBoundingClientRect().left;const sash=[...document.querySelectorAll(".workbench-sash--row")].filter(e=>e.getBoundingClientRect().left<x).sort((a,b)=>b.getBoundingClientRect().left-a.getBoundingClientRect().left)[0];sash.dispatchEvent(new KeyboardEvent("keydown",{key:"ArrowLeft",bubbles:true}));})()');await pause(100);
 const widthAfter=await main.webContents.executeJavaScript('document.querySelector(".workbench-review").getBoundingClientRect().width');
 console.log('REVIEW_WIDTHS',widthBefore,widthAfter,await main.webContents.executeJavaScript('[...document.querySelectorAll(".workbench-sash--row")].map(e=>({x:e.getBoundingClientRect().left,width:e.parentElement.getBoundingClientRect().width,value:e.getAttribute("aria-valuenow")}))'));
 assert.ok(widthAfter>widthBefore,'docked Review expands through its resize separator');
 await main.webContents.executeJavaScript(${JSON.stringify("[...document.querySelectorAll('button')].find(b=>b.getAttribute('aria-label')==='Hide right panel').click()")});await pause(80);
 assert.equal(await main.webContents.executeJavaScript('[...document.querySelectorAll(".workbench-review,.workbench-files,.workbench-terminal-pane")].filter(e=>e.getClientRects().length&&getComputedStyle(e).visibility!=="hidden").length'),0);
 await main.webContents.executeJavaScript(${JSON.stringify("[...document.querySelectorAll('button')].find(b=>b.getAttribute('aria-label')==='Show right panel').click()")});await pause(80);
 console.log('Native layouts: four scoped presets, docked Review growth, and complete auxiliary panel hiding passed');
 console.log("CHAT_TEST_READY");
 const response=await main.webContents.executeJavaScript('window.variant1Deck.openChatWindow("chat-b","Detached chat")');assert.equal(response.ok,true);

 await wait(main,'document.querySelector(".window-count")?.textContent==="1"');
 await main.webContents.executeJavaScript(${JSON.stringify("[...document.querySelectorAll('button')].find(b=>b.getAttribute('aria-label')==='Panels and windows').click()")});await pause(80);
 assert.match(await main.webContents.executeJavaScript('document.querySelector(".command-palette").textContent'),/Focus Detached chat/);
 fs.writeFileSync(${JSON.stringify(path.join(out,'windows-inventory.png'))},(await main.webContents.capturePage()).toPNG());
 await main.webContents.executeJavaScript(${JSON.stringify("[...document.querySelectorAll('button')].find(b=>b.getAttribute('aria-label')==='Close action palette').click()")});
 const child=manager.list()[0];await wait(child,'!!document.querySelector(".compact-chat #composer-input") && !document.querySelector(".startup-cover")');
 assert.equal(await child.webContents.executeJavaScript('document.querySelectorAll(".history-panel,.workbench,.titlebar-workbench-tools,.section-footer").length'),0);
 assert.equal(frames.filter(f=>f.role==='detached_chat'&&f.type==='browser:host:register').length,0);
 assert.ok(sockets.some(s=>s.role==='detached_chat'&&s.pinned==='chat-b'));
 const peer=sockets.find(s=>s.role==='detached_chat');peer.socket.send(JSON.stringify({type:'chat:session',session:{id:'foreign',title:'Wrong chat',messages:[]}}));await pause(100);
 assert.match(await child.webContents.executeJavaScript('document.querySelector(".titlebar__name").textContent'),/Detached chat/);
 child.showInactive();child.webContents.invalidate();await pause(250);
 fs.writeFileSync(${JSON.stringify(path.join(out,'detached-chat.png'))},(await child.webContents.capturePage()).toPNG());

 main.setSize(1680,1000);
 await main.webContents.executeJavaScript(${JSON.stringify("[...document.querySelectorAll('button')].find(b=>b.getAttribute('aria-label')==='Hide right panel')?.click();[...document.querySelectorAll('.workbench-editbar button')].find(b=>b.textContent==='Done')?.click()")});await pause(100);
 // Open two different chats through their real history menus.
 for(const [id,label] of [['chat-b','Open beside'],['chat-c','Open below']]) {
  await main.webContents.executeJavaScript('document.querySelector('+JSON.stringify('[data-session-id="'+id+'"] .history-item__menu')+').click()');await pause(30);
  await main.webContents.executeJavaScript('[...document.querySelectorAll("[role=menuitem]")].find(b=>b.textContent==='+JSON.stringify(label)+').click()');
 }
 await wait(main,'document.querySelectorAll("iframe.workbench-chat-frame").length===2');
 await wait(main,'[...document.querySelectorAll("iframe.workbench-chat-frame")].every(f=>f.contentDocument.querySelector("#composer-input")&&!f.contentDocument.querySelector(".startup-cover"))');
 assert.equal(sockets.filter(s=>s.role==='detached_chat').length,3,'one attachment per detached/embedded chat view');
 assert.equal(frames.filter(f=>f.role==='detached_chat'&&f.type==='browser:host:register').length,0);
 const frameIds=await main.webContents.executeJavaScript('[...document.querySelectorAll("iframe.workbench-chat-frame")].map(f=>new URL(f.src).searchParams.get("detached_chat"))');
 assert.deepEqual(frameIds.sort(),['chat-b','chat-c']);
 main.webContents.invalidate();await pause(150);
 fs.writeFileSync(${JSON.stringify(path.join(out,'three-chat-panes.png'))},(await main.webContents.capturePage()).toPNG());

 await main.webContents.executeJavaScript('(()=>{const views=[...document.querySelectorAll("iframe.workbench-chat-frame")];window.__preservedChatFrame=views.find(f=>new URL(f.src).searchParams.get("detached_chat")==="chat-b").contentWindow;for(const frame of views){const win=frame.contentWindow,input=win.document.getElementById("composer-input");Object.getOwnPropertyDescriptor(win.HTMLTextAreaElement.prototype,"value").set.call(input,"Draft for "+new URL(frame.src).searchParams.get("detached_chat"));input.dispatchEvent(new win.Event("input",{bubbles:true}));}})()');await pause(100);
 assert.equal(await main.webContents.executeJavaScript('document.getElementById("composer-input").value'),'','pane typing cannot change main draft');
 await main.webContents.executeJavaScript('(()=>{const transfer=new DataTransfer();transfer.setData("application/x-variant1-pane","chatview:chat-b");document.querySelector('+JSON.stringify('[data-pane-tab="chatview:chat-c"]')+').dispatchEvent(new DragEvent("drop",{bubbles:true,dataTransfer:transfer}));})()');await pause(100);
 assert.equal(await main.webContents.executeJavaScript('window.__preservedChatFrame.document.getElementById("composer-input").value'),'Draft for chat-b');
 assert.equal(await main.webContents.executeJavaScript('window.__preservedChatFrame === [...document.querySelectorAll("iframe.workbench-chat-frame")].find(f=>new URL(f.src).searchParams.get("detached_chat")==="chat-b").contentWindow'),true,'tab move keeps browsing context alive');
 main.setSize(1680,1000);
 await main.webContents.executeJavaScript(${JSON.stringify("[...document.querySelectorAll('button')].find(b=>b.getAttribute('aria-label')==='Hide right panel')?.click();[...document.querySelectorAll('.workbench-editbar button')].find(b=>b.textContent==='Done')?.click()")});
 main.webContents.invalidate();await pause(200);

 fs.writeFileSync(${JSON.stringify(path.join(out,'multiple-chats.png'))},(await main.webContents.capturePage()).toPNG());

 for(const id of ['chat-b','chat-c']) {
  await main.webContents.executeJavaScript('(()=>{const frame=[...document.querySelectorAll("iframe.workbench-chat-frame")].find(f=>new URL(f.src).searchParams.get("detached_chat")==='+JSON.stringify(id)+');const win=frame.contentWindow;win.document.getElementById("composer-input").dispatchEvent(new win.KeyboardEvent("keydown",{key:"Enter",bubbles:true}));})()');
 }
 await pause(150);
 assert.deepEqual(frames.filter(f=>f.type==='chat').map(f=>f.session_id).sort(),['chat-b','chat-c'],'each composer dispatches to its own canonical chat');
 console.log('Chat layout: native window inventory and two simultaneous pinned chat panes passed');
 child.hide();
 await main.webContents.executeJavaScript('window.variant1Deck.openChatWindow("chat-b","Detached chat")');assert.equal(manager.list().length,1);
 await child.webContents.executeJavaScript('window.variant1Deck.close()');await pause(150);assert.equal(manager.list().length,0);assert.equal(main.isDestroyed(),false);
 assert.equal(frames.filter(f=>['chat:session:new','chat:stop','cancel'].includes(f.type)).length,0,'opening/closing a view cannot submit or cancel work');
 fs.writeFileSync(${JSON.stringify(path.join(out,'chat-window-frames.json'))},JSON.stringify(frames,null,2));
 console.log('Native chat windows: startup cover, compact pinned view, no host takeover, foreign snapshot rejection, reuse and view-only close passed');
}).catch(e=>{console.error(e);process.exitCode=1;}).finally(()=>{if(manager)manager.closeAll();for(const {socket} of sockets)socket.terminate();if(server)server.close();app.exit(process.exitCode||0);});
`);
const child=spawn(require('electron'),[path.join(temp,'main.cjs')],{windowsHide:true,stdio:'inherit',cwd:root});
const timer=setTimeout(()=>child.kill(),45000);
child.once('exit',code=>{clearTimeout(timer);process.exitCode=code===0?0:1;const absolute=path.resolve(temp);if(path.dirname(absolute)===path.resolve(os.tmpdir())&&path.basename(absolute).startsWith('variant1-chat-windows-'))fs.rmSync(absolute,{recursive:true,force:true,maxRetries:4,retryDelay:150});});
