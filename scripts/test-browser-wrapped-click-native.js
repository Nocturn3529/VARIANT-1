'use strict';
// Disposable native reproduction/acceptance. No provider or existing app.
const fs=require('node:fs'),os=require('node:os'),path=require('node:path'),crypto=require('node:crypto');
const {spawn}=require('node:child_process');
const root=path.resolve(__dirname,'..'), baseline=process.argv.includes('--baseline');
const out=require('./native-test-artifacts')(root,`artifacts/embedded-wrapped-click-2026-09-07/${baseline?'baseline':'native'}`,'wrapped-click');
const temp=fs.mkdtempSync(path.join(os.tmpdir(),'variant1-wrapped-click-'));
(async()=>{
  const deck=path.join(temp,'frontend/main-deck');fs.mkdirSync(deck,{recursive:true});
  await require('esbuild').build({entryPoints:[path.join(__dirname,'test-browser-host-native-entry.tsx')],bundle:true,jsx:'automatic',platform:'browser',format:'esm',outfile:path.join(deck,'renderer.js'),logLevel:'silent'});
  fs.copyFileSync(path.join(root,'frontend/main-deck/dist/platform.css'),path.join(deck,'fixture.css'));
  fs.writeFileSync(path.join(deck,'index.html'),fs.readFileSync(path.join(root,'frontend/main-deck/index.html'),'utf8').replace('./dist/platform.js','./renderer.js').replace('./dist/platform.css','./fixture.css'));
  fs.writeFileSync(path.join(temp,'preload.cjs'),`const {contextBridge}=require('electron');contextBridge.exposeInMainWorld('variant1Deck',{getWorkbenchRoot:async()=>({ok:false})});`);
  const page=`<!doctype html><title>Wrapped anchor native fixture</title><style>
    body{font:16px monospace;margin:24px}p{width:430px;line-height:40px;margin:0}#prefix{display:inline-block;width:110px}
    #blocked-box{position:relative;width:160px;height:40px;margin-top:40px}#blocked,#cover{position:absolute;inset:0;width:160px;height:40px}#cover{background:#ddd;z-index:2}
    </style><p id="paragraph"><span id="prefix">Download</span><a id="zip" href="/fixture.zip">Windows embeddable package (64-<br>bit)</a></p>
    <div id="blocked-box"><button id="blocked">Blocked button</button><div id="cover">Overlay</div></div>
    <script>window.mouseEvents=[];window.mouseUps=0;
    for(const type of ['mousedown','mouseup','click'])document.addEventListener(type,e=>{mouseEvents.push({type:e.type,target:e.target.id,x:e.clientX,y:e.clientY});if(e.type==='mouseup'){mouseUps++;if(window.resolveUp){window.resolveUp();window.resolveUp=null}}});
    window.armMouse=()=>{window.nextUp=new Promise(resolve=>window.resolveUp=resolve)};</script>`;
  const sourceHash=crypto.createHash('sha256').update(fs.readFileSync(path.join(root,'frontend/main-deck/src/workbench/browserBridge.ts'))).digest('hex');
  fs.writeFileSync(path.join(temp,'main.cjs'),`
    const {app,BrowserWindow,webContents,session,protocol}=require('electron');
    const fs=require('node:fs'),http=require('node:http'),assert=require('node:assert/strict');
    const boot=require(${JSON.stringify(path.join(root,'electron-app-boot.js'))});boot.applyGpuFlags(app);boot.registerVariant1Scheme(protocol);app.setPath('userData',${JSON.stringify(path.join(temp,'profile'))});
    const receipt={mode:${JSON.stringify(baseline?'baseline':'acceptance')},bridge_sha256:${JSON.stringify(sourceHash)},commands:[],checks:[],window_intervals:[]};let win,guest,server,downloadCount=0,requestCount=0,finishDownload;
    const downloadDone=new Promise(resolve=>finishDownload=resolve);
    const check=(label,fn)=>{fn();receipt.checks.push(label)};
    app.whenReady().then(async()=>{
      boot.registerVariant1Protocol(protocol,${JSON.stringify(temp)});
      session.fromPartition('persist:variant1-preview').on('will-download',(_event,item)=>{
        downloadCount++;item.setSavePath(${JSON.stringify(path.join(temp,'fixture.download'))});
        item.once('done',(_event,state)=>{receipt.download={state,filename:item.getFilename(),bytes:item.getReceivedBytes()};finishDownload(state)});
      });
      server=http.createServer((req,res)=>{if(req.url==='/fixture.zip'){requestCount++;res.writeHead(200,{'Content-Type':'application/zip','Content-Disposition':'attachment; filename="fixture.zip"'});res.end('WRAPPED-ANCHOR-FIXTURE');}else{res.setHeader('Content-Type','text/html');res.end(${JSON.stringify(page)});}});
      await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
      win=new BrowserWindow({show:false,width:1100,height:740,webPreferences:{preload:${JSON.stringify(path.join(temp,'preload.cjs'))},contextIsolation:true,sandbox:true,nodeIntegration:false,webviewTag:true,backgroundThrottling:false}});
      const evaluate=code=>win.webContents.executeJavaScript(code,true);
      const command=async value=>{const result=await evaluate('window.e01.command('+JSON.stringify(value)+')');receipt.commands.push({command:value,result});return result};
      await win.loadURL('variant1://app/frontend/main-deck/index.html');win.show();win.focus();receipt.window_intervals.push({event:'shown',at:new Date().toISOString()});
      assert.equal((await command({action:'new_page',tab_id:'wrapped',url:'http://127.0.0.1:'+server.address().port})).ok,true);
      guest=webContents.getAllWebContents().find(item=>item.getType()==='webview'&&item.hostWebContents===win.webContents);assert.ok(guest);
      const read=await command({action:'read',tab_id:'wrapped'}), ref=read.elements.find(e=>e.name.includes('Windows embeddable')).ref;
      const geometry=await guest.executeJavaScript('(()=>{const a=document.getElementById("zip"),r=a.getBoundingClientRect(),fragments=Array.from(a.getClientRects()).map(r=>({x:r.x,y:r.y,width:r.width,height:r.height}));return {box:{x:r.x,y:r.y,width:r.width,height:r.height},fragments,centerHit:document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)?.id}})()');receipt.geometry=geometry;
      check('Fixture union center lies outside wrapped anchor',()=>{assert.ok(geometry.fragments.length>1);assert.notEqual(geometry.centerHit,'zip')});
      const click=async extra=>{
        await guest.executeJavaScript('window.armMouse()');
        const result=await command({action:'click',tab_id:'wrapped',target:ref,...extra});assert.equal(result.ok,true,result.error);
        // Wait for the actual native mouseup signal, not an arbitrary delay.
        await guest.executeJavaScript('window.nextUp');
        return guest.executeJavaScript('({events:mouseEvents,ups:mouseUps})');
      };
      if(${baseline}){
        receipt.miss=await click({});
        check('Old default reports success but native mouseup misses anchor',()=>assert.notEqual(receipt.miss.events.find(e=>e.type==='mouseup').target,'zip'));
      }else{
        receipt.miss=await click({position:{x:geometry.box.width/2,y:geometry.box.height/2}});
        check('Explicit union-gap position is not silently retargeted',()=>assert.notEqual(receipt.miss.events.find(e=>e.type==='mouseup').target,'zip'));
      }
      check('Miss did not start a download',()=>{assert.equal(downloadCount,0);assert.equal(requestCount,0)});
      const f=geometry.fragments.find(f=>f.width>0&&f.height>0);
      receipt.hit=await click(${baseline}?{position:{x:f.x+f.width/2-geometry.box.x,y:f.y+f.height/2-geometry.box.y}}:{});
      assert.equal(await downloadDone,'completed');
      check('Visible fragment receives actual native mouseup',()=>assert.equal(receipt.hit.events.filter(e=>e.type==='mouseup').at(-1).target,'zip'));
      check('Exactly one native download and one HTTP download request',()=>{assert.equal(downloadCount,1);assert.equal(requestCount,1)});
      if(!${baseline}){
        // Download navigation invalidates the old element epoch even when the
        // committed document is retained. Reobserve before another action.
        const refreshed=await command({action:'read',tab_id:'wrapped'});
        const blocked=refreshed.elements.find(e=>e.name==='Blocked button').ref;
        const before=await guest.executeJavaScript('mouseEvents.length');
        const result=await command({action:'click',tab_id:'wrapped',target:blocked});
        check('Covered default point is rejected before sending input',()=>{assert.equal(result.ok,false);assert.equal(result.code,'CLICK_TARGET_BLOCKED')});
        assert.equal(await guest.executeJavaScript('mouseEvents.length'),before);
        await guest.executeJavaScript('window.armMouse()');
        const forced=await command({action:'click',tab_id:'wrapped',target:blocked,force:true});
        assert.equal(forced.ok,true,forced.error);await guest.executeJavaScript('window.nextUp');
        receipt.forcedEvents=await guest.executeJavaScript('mouseEvents');
        check('force bypasses hit check without claiming target activation',()=>assert.equal(receipt.forcedEvents.filter(e=>e.type==='mouseup').at(-1).target,'cover'));
        assert.equal(downloadCount,1);
      }
      receipt.passed=true;console.log('E50 '+receipt.mode+': '+receipt.checks.length+' native wrapped-anchor/hit-target/download checks passed');
    }).catch(error=>{receipt.passed=false;receipt.error=String(error.stack||error);console.error(error);process.exitCode=1}).finally(()=>{
      receipt.downloadCount=downloadCount;receipt.requestCount=requestCount;receipt.window_intervals.push({event:'closing',at:new Date().toISOString()});
      fs.writeFileSync(${JSON.stringify(path.join(out,'native-receipt.json'))},JSON.stringify(receipt,null,2));
      if(server)server.close();app.exit(process.exitCode||0);
    });
  `);
  const child=spawn(require('electron'),[path.join(temp,'main.cjs')],{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe']});
  let log='';for(const stream of [child.stdout,child.stderr])stream.on('data',chunk=>{log+=chunk;process.stdout.write(chunk)});
  const deadline=setTimeout(()=>child.kill(),60000);
  const code=await new Promise((resolve,reject)=>{child.once('exit',resolve);child.once('error',reject)});clearTimeout(deadline);
  fs.writeFileSync(path.join(out,'native.log'),log);process.exitCode=code===0?0:1;
})().catch(error=>{console.error(error);process.exitCode=1}).finally(()=>{
  if(path.dirname(temp)===path.resolve(os.tmpdir())&&path.basename(temp).startsWith('variant1-wrapped-click-'))fs.rmSync(temp,{recursive:true,force:true,maxRetries:4,retryDelay:150});
});
