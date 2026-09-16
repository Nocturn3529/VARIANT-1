'use strict';
// Isolated local Electron fixture. Actions use the production browser-host
// command path; direct guest scripting below only reads fixture evidence.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const esbuild = require('esbuild');
const root = path.resolve(__dirname, '..');
const out = require('./native-test-artifacts')(root, 'artifacts/embedded-keyboard-2026-09-07/native', 'keyboard');
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'variant1-keyboard-test-'));

(async () => {
  const deck = path.join(temp, 'frontend/main-deck'); fs.mkdirSync(deck, {recursive:true});
  await esbuild.build({entryPoints:[path.join(__dirname,'test-browser-host-native-entry.tsx')],bundle:true,jsx:'automatic',platform:'browser',format:'esm',outfile:path.join(deck,'renderer.js'),logLevel:'silent'});
  fs.copyFileSync(path.join(root,'frontend/main-deck/dist/platform.css'),path.join(deck,'fixture.css'));
  fs.writeFileSync(path.join(deck,'index.html'),fs.readFileSync(path.join(root,'frontend/main-deck/index.html'),'utf8').replace('./dist/platform.js','./renderer.js').replace('./dist/platform.css','./fixture.css'));
  fs.writeFileSync(path.join(temp,'preload.cjs'),`const {contextBridge}=require('electron');contextBridge.exposeInMainWorld('variant1Deck',{getWorkbenchRoot:async()=>({ok:false})});`);
  const page = `<!doctype html><title>E46 isolated keyboard fixture</title>
    <label>Seek<input aria-label="Seek" id="seek" type="range" min="0" max="634" step="5" value="0"></label>
    <label>Text<input aria-label="Text" id="text" type="text"></label>
    <script>window.keyEvents=[];window.focusEvents=[];
    for(const type of ['keydown','keyup','keypress','input'])document.addEventListener(type,e=>keyEvents.push({type:e.type,key:e.key,code:e.code,ctrl:e.ctrlKey,shift:e.shiftKey,alt:e.altKey,meta:e.metaKey,target:e.target.id,value:e.target.value}));
    document.addEventListener('focusin',e=>focusEvents.push(e.target.id));</script>`;
  fs.writeFileSync(path.join(temp,'main.cjs'),`
    const {app,BrowserWindow,webContents}=require('electron');
    const fs=require('node:fs'),http=require('node:http'),assert=require('node:assert/strict');
    const boot=require(${JSON.stringify(path.join(root,'electron-app-boot.js'))});
    boot.applyGpuFlags(app);boot.registerVariant1Scheme(require('electron').protocol);
    app.setPath('userData',${JSON.stringify(path.join(temp,'profile'))});
    const receipts={started_at:new Date().toISOString(),window_intervals:[],commands:[],checks:[],snapshots:[]};let win,server,guest;
    const check=(label,fn)=>{fn();receipts.checks.push(label);};
    app.whenReady().then(async()=>{
      boot.registerVariant1Protocol(require('electron').protocol,${JSON.stringify(temp)});
      server=http.createServer((_req,res)=>{res.setHeader('Content-Type','text/html');res.end(${JSON.stringify(page)});});
      await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
      win=new BrowserWindow({show:false,width:1100,height:740,title:'E46 isolated keyboard validation',webPreferences:{preload:${JSON.stringify(path.join(temp,'preload.cjs'))},contextIsolation:true,sandbox:true,nodeIntegration:false,webviewTag:true,backgroundThrottling:false}});
      const evaluate=code=>win.webContents.executeJavaScript(code,true);
      const command=async value=>{const result=await evaluate('window.e01.command('+JSON.stringify(value)+')');receipts.commands.push({command:value,result});return result;};
      await win.loadURL('variant1://app/frontend/main-deck/index.html');win.show();win.focus();receipts.window_intervals.push({event:'shown',at:new Date().toISOString()});
      assert.equal((await command({action:'new_page',tab_id:'keyboard',url:'http://127.0.0.1:'+server.address().port})).ok,true);
      guest=webContents.getAllWebContents().find(item=>item.getType()==='webview'&&item.hostWebContents===win.webContents);
      assert.ok(guest);
      const read=await command({action:'read',tab_id:'keyboard'});
      const slider=read.elements.find(item=>item.name==='Seek').ref, text=read.elements.find(item=>item.name==='Text').ref;
      const keys=async (target,value)=>{const result=await command({action:'keys',tab_id:'keyboard',target,keys:value});assert.equal(result.ok,true,result.error);};
      const snapshot=async label=>{const state=await guest.executeJavaScript('({events:window.keyEvents,focus:window.focusEvents,active:document.activeElement.id,range:document.getElementById("seek").value,text:document.getElementById("text").value})');receipts.snapshots.push({label,...state});return state;};
      await keys(slider,'Home');for(let i=0;i<24;i++)await keys(slider,'ArrowRight');
      let state=await snapshot('24 right arrows');
      check('Home plus 24 canonical ArrowRight presses moves local range to 120',()=>assert.equal(state.range,'120'));
      check('Actual ArrowRight native key/code events (24 down and up)',()=>{
        assert.equal(state.events.filter(e=>e.type==='keydown'&&e.key==='ArrowRight'&&e.code==='ArrowRight').length,24);
        assert.equal(state.events.filter(e=>e.type==='keyup'&&e.key==='ArrowRight'&&e.code==='ArrowRight').length,24);
      });
      for(const [key,value] of [['ArrowLeft','115'],['ArrowUp','120'],['ArrowDown','115'],['End','630'],['Home','0']]){
        await keys(slider,key);state=await snapshot(key);check(key+' native range behavior',()=>assert.equal(state.range,value));
      }
      await keys(text,'Control+Shift+ArrowLeft');state=await snapshot('modifier chord');
      check('Control and Shift retained on actual native arrow event',()=>assert.ok(state.events.some(e=>e.type==='keydown'&&e.key==='ArrowLeft'&&e.ctrl&&e.shift)));
      for(const key of ['a','A','!','Shift+B',' ','+'])await keys(text,key);
      state=await snapshot('printable case');
      check('Printable case, shifted character, space and plus inserted',()=>assert.equal(state.text,'aA!B +'));
      check('Shift reaches printable char event',()=>assert.ok(state.events.some(e=>e.type==='keypress'&&e.key==='B'&&e.shift)));
      await keys(text,'Control+a');await keys(text,'z');state=await snapshot('select all');
      check('Control+a selects all without injecting a character',()=>assert.equal(state.text,'z'));
      for(const invalid of ['ImaginaryKey','Bogus+ArrowRight','KeyA','Control+']){
        const before=await snapshot('before invalid '+invalid);
        const result=await command({action:'keys',tab_id:'keyboard',target:slider,keys:invalid});
        const after=await snapshot('after invalid '+invalid);
        check('Reject '+invalid+' before element focus or events',()=>{
          assert.equal(result.ok,false);assert.equal(result.code,'UNSUPPORTED_BROWSER_KEY');
          assert.equal(after.active,'text');assert.deepEqual(after.focus,before.focus);assert.deepEqual(after.events,before.events);
        });
      }
      receipts.passed=true;console.log('E46 native: '+receipts.checks.length+' keyboard/slider/modifier/character/invalid-input checks passed');
    }).catch(error=>{receipts.passed=false;receipts.error=String(error.stack||error);console.error(error);process.exitCode=1;}).finally(()=>{
      receipts.window_intervals.push({event:'closing',at:new Date().toISOString()});
      fs.writeFileSync(${JSON.stringify(path.join(out,'native-receipt.json'))},JSON.stringify(receipts,null,2));
      if(server)server.close();app.exit(process.exitCode||0);
    });
  `);
  const child=spawn(require('electron'),[path.join(temp,'main.cjs')],{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe']});
  let log='';for(const stream of [child.stdout,child.stderr])stream.on('data',chunk=>{log+=chunk;process.stdout.write(chunk);});
  const deadline=setTimeout(()=>child.kill(),60000);
  const code=await new Promise((resolve,reject)=>{child.once('exit',resolve);child.once('error',reject);});clearTimeout(deadline);
  fs.writeFileSync(path.join(out,'native.log'),log);process.exitCode=code===0?0:1;
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(()=>{
  if(path.dirname(temp)===path.resolve(os.tmpdir())&&path.basename(temp).startsWith('variant1-keyboard-test-'))fs.rmSync(temp,{recursive:true,force:true,maxRetries:4,retryDelay:150});
});
