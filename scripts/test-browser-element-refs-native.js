'use strict';
// Native document/node identity contract through the existing host bridge.
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {spawn}=require('node:child_process');
const root=path.resolve(__dirname,'..');
const out=require('./native-test-artifacts')(root,'artifacts/embedded-element-identity-2026-09-07/native','element-refs');
const temp=fs.mkdtempSync(path.join(os.tmpdir(),'variant1-element-refs-'));
(async()=>{
  const deck=path.join(temp,'frontend/main-deck');fs.mkdirSync(deck,{recursive:true});
  await require('esbuild').build({entryPoints:[path.join(__dirname,'test-browser-host-native-entry.tsx')],bundle:true,jsx:'automatic',platform:'browser',format:'esm',outfile:path.join(deck,'renderer.js'),logLevel:'silent'});
  fs.copyFileSync(path.join(root,'frontend/main-deck/dist/platform.css'),path.join(deck,'fixture.css'));
  fs.writeFileSync(path.join(deck,'index.html'),fs.readFileSync(path.join(root,'frontend/main-deck/index.html'),'utf8').replace('./dist/platform.js','./renderer.js').replace('./dist/platform.css','./fixture.css'));
  fs.writeFileSync(path.join(temp,'preload.cjs'),`const {contextBridge}=require('electron');contextBridge.exposeInMainWorld('variant1Deck',{getWorkbenchRoot:async()=>({ok:false})});`);
  const html=`<!doctype html><title>Element identity fixture</title><button id="first">First</button><button id="retained">Retained</button><a id="download" href="/fixture.zip">Download fixture</a>
    <script>window.actions=[];document.addEventListener('click',e=>actions.push({target:e.target.id,type:e.type}));</script>`;
  fs.writeFileSync(path.join(temp,'main.cjs'),`
    const {app,BrowserWindow,protocol,webContents,session}=require('electron');
    const fs=require('node:fs'),http=require('node:http'),assert=require('node:assert/strict'),{once}=require('node:events');
    const boot=require(${JSON.stringify(path.join(root,'electron-app-boot.js'))});boot.applyGpuFlags(app);boot.registerVariant1Scheme(protocol);app.setPath('userData',${JSON.stringify(path.join(temp,'profile'))});
    let win,guest,server,downloads=0,resolveDownload;const done=new Promise(resolve=>resolveDownload=resolve);
    const receipt={checks:[],commands:[],snapshots:[],window_intervals:[]};const check=(label,fn)=>{fn();receipt.checks.push(label)};
    app.whenReady().then(async()=>{
      boot.registerVariant1Protocol(protocol,${JSON.stringify(temp)});
      session.fromPartition('persist:variant1-preview').on('will-download',(_event,item)=>{downloads++;item.setSavePath(${JSON.stringify(path.join(temp,'fixture.download'))});item.once('done',(_e,state)=>resolveDownload(state));});
      server=http.createServer((req,res)=>{if(req.url==='/fixture.zip'){res.writeHead(200,{'Content-Disposition':'attachment; filename="fixture.zip"','Content-Type':'application/zip'});res.end('IDENTITY-FIXTURE');}else{res.setHeader('Content-Type','text/html');res.end(${JSON.stringify(html)})}});
      await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const base='http://127.0.0.1:'+server.address().port;
      win=new BrowserWindow({show:false,width:1100,height:740,webPreferences:{preload:${JSON.stringify(path.join(temp,'preload.cjs'))},contextIsolation:true,sandbox:true,nodeIntegration:false,webviewTag:true,backgroundThrottling:false}});
      const command=async value=>{const result=await win.webContents.executeJavaScript('window.e01.command('+JSON.stringify(value)+')',true);receipt.commands.push({command:value,result});return result};
      const findGuest=()=>webContents.getAllWebContents().find(item=>item.getType()==='webview'&&item.hostWebContents===win.webContents);
      const read=async(extra={})=>{const result=await command({action:'read',tab_id:'identity',...extra});assert.equal(result.ok,true,result.error);return result.elements};
      const ref=(rows,name)=>rows.find(row=>row.name===name).ref;
      const actions=()=>guest.executeJavaScript('window.actions');
      const click=async target=>{const result=await command({action:'click',tab_id:'identity',target});assert.equal(result.ok,true,result.error);return actions()};
      const reject=async(target,label)=>{const before=await actions();const result=await command({action:'click',tab_id:'identity',target});check(label,()=>{assert.equal(result.ok,false);assert.match(result.error,/stale|stopped|ready|closed/i)});assert.deepEqual(await actions(),before)};
      // Fixture mutations are setup, not alternate click controls. Every action
      // under test is a normal browser host command with the retained ref.
      const mutate=async expression=>{const result=await command({action:'evaluate',tab_id:'identity',expression});assert.equal(result.ok,true,result.error)};
      await win.loadURL('variant1://app/frontend/main-deck/index.html');win.show();win.focus();receipt.window_intervals.push({event:'shown',at:new Date().toISOString()});
      assert.equal((await command({action:'new_page',tab_id:'identity',url:base})).ok,true);guest=findGuest();assert.ok(guest);
      const firstRead=await read(),old=ref(firstRead,'Retained'),download=ref(firstRead,'Download fixture');
      const again=await read();check('Same document reread preserves original reference',()=>assert.equal(ref(again,'Retained'),old));
      await read({max_elements:0});await read({max_elements:1});
      check('Retained ref clicks actual node after zero/limited reads',()=>{});assert.equal((await click(old)).at(-1).target,'retained');
      await mutate('(()=>{const n=document.createElement("button");n.id="inserted";n.textContent="Inserted";document.body.prepend(n);document.body.prepend(document.getElementById("retained"));return true})()');
      check('Insertion/reorder preserves node identity',()=>{});assert.equal(ref(await read(),'Retained'),old);assert.equal((await click(old)).at(-1).target,'retained');
      await mutate('(()=>{const n=document.getElementById("retained").cloneNode(true);n.id="clone";n.textContent="Clone";document.body.prepend(n);return true})()');
      const cloned=await read();check('Cloned copied marker gets a distinct ref',()=>assert.notEqual(ref(cloned,'Clone'),old));assert.equal((await click(old)).at(-1).target,'retained');
      await mutate('(()=>{window.savedNode=document.getElementById("retained");savedNode.remove();return true})()');
      await reject(old,'Detached retained node fails without native input');
      await mutate('(()=>{document.body.appendChild(savedNode);return true})()');const reattached=ref(await read(),'Retained');
      check('Reattached retired identity gets a fresh ref',()=>assert.notEqual(reattached,old));await reject(old,'Old retired ref never aliases reattached node');
      await mutate('(()=>{const n=document.getElementById("retained"),c=n.cloneNode(true);c.id="replacement";c.textContent="Replacement";n.replaceWith(c);return true})()');
      const replaced=await read();await reject(reattached,'Replacement with copied marker cannot inherit ref');const current=ref(replaced,'Replacement');
      await mutate('(()=>{history.pushState({},"","#state");return true})()');await read();assert.equal((await click(current)).at(-1).target,'replacement');
      const inPage=once(guest,'did-navigate-in-page');await mutate('(()=>{location.hash="hash-change";return true})()');await inPage;
      assert.equal(ref(await read(),'Replacement'),current);assert.equal((await click(current)).at(-1).target,'replacement');check('History/hash navigation retains live node refs',()=>{});
      // A ref minted before all those reads/mutations still reaches its original anchor.
      await click(download);assert.equal(await done,'completed');check('Original download ref survives rereads and starts exactly one download',()=>assert.equal(downloads,1));
      assert.equal((await command({action:'navigate',tab_id:'identity',url:base+'/new-document'})).ok,true);await read();await reject(current,'True navigation rejects prior-document ref');
      const reloadRef=ref(await read(),'Retained');const loaded=once(guest,'did-stop-loading');assert.equal((await command({action:'reload',tab_id:'identity'})).ok,true);await loaded;await read();await reject(reloadRef,'Reload rejects previous ref');
      const guestRef=ref(await read(),'Retained'),oldGuest=guest.id;
      assert.equal((await command({action:'close_page',tab_id:'identity'})).ok,true);
      assert.equal((await command({action:'new_page',tab_id:'identity',url:base+'/new-guest'})).ok,true);guest=findGuest();
      check('Replacement uses a different native guest',()=>assert.notEqual(guest.id,oldGuest));await read();await reject(guestRef,'New guest rejects previous guest reference');
      receipt.snapshots.push({finalActions:await actions(),downloads});receipt.passed=true;
      console.log('E51 native: '+receipt.checks.length+' stable-node/reference-fence checks passed');
    }).catch(error=>{receipt.passed=false;receipt.error=String(error.stack||error);console.error(error);process.exitCode=1}).finally(()=>{
      receipt.window_intervals.push({event:'closing',at:new Date().toISOString()});receipt.downloads=downloads;
      fs.writeFileSync(${JSON.stringify(path.join(out,'native-receipt.json'))},JSON.stringify(receipt,null,2));if(server)server.close();app.exit(process.exitCode||0);
    });
  `);
  const child=spawn(require('electron'),[path.join(temp,'main.cjs')],{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe']});
  let log='';for(const stream of [child.stdout,child.stderr])stream.on('data',chunk=>{log+=chunk;process.stdout.write(chunk)});
  const deadline=setTimeout(()=>child.kill(),60000);const code=await new Promise((resolve,reject)=>{child.once('exit',resolve);child.once('error',reject)});clearTimeout(deadline);
  fs.writeFileSync(path.join(out,'native.log'),log);process.exitCode=code===0?0:1;
})().catch(error=>{console.error(error);process.exitCode=1}).finally(()=>{
  if(path.dirname(temp)===path.resolve(os.tmpdir())&&path.basename(temp).startsWith('variant1-element-refs-'))fs.rmSync(temp,{recursive:true,force:true,maxRetries:4,retryDelay:150});
});
