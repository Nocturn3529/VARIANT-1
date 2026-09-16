'use strict';
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..'),output=require('./native-test-artifacts')(root,'artifacts/composer-2026-09-06','composer');
async function launch(){
  const {spawn}=require('node:child_process'),temporary=fs.mkdtempSync(path.join(os.tmpdir(),'variant1-composer-'));
  fs.mkdirSync(output,{recursive:true});
  await require('esbuild').build({entryPoints:[path.join(__dirname,'test-composer-native-entry.tsx')],bundle:true,format:'esm',platform:'browser',jsx:'automatic',outfile:path.join(temporary,'renderer.js'),logLevel:'silent'});
  fs.writeFileSync(path.join(temporary,'index.html'),'<!doctype html><html><head><link rel="stylesheet" href="variant1://app/frontend/main-deck/dist/platform.css"></head><body><div id="variant1-react-root"></div><script type="module" src="renderer.js"></script></body></html>');
  const child=spawn(require('electron'),[__filename,...process.argv.slice(2)],{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe'],env:{...process.env,VARIANT1_COMPOSER_TEST_DIR:temporary}});
  let log='';for(const stream of [child.stdout,child.stderr])stream.on('data',chunk=>{log+=chunk;process.stdout.write(chunk)});
  const timer=setTimeout(()=>child.kill(),60000);
  try{
    const code=await new Promise((resolve,reject)=>{child.once('error',reject);child.once('exit',resolve)});process.exitCode=code===0?0:1;
    if(code===0&&process.argv.includes('--record-motion'))require('node:child_process').execFileSync('ffmpeg',['-y','-hide_banner','-loglevel','error','-framerate','20','-i',path.join(temporary,'frames','frame-%04d.png'),'-vf','pad=ceil(iw/2)*2:ceil(ih/2)*2','-c:v','libx264','-crf','18','-pix_fmt','yuv420p','-movflags','+faststart',path.join(output,'control-motion.mp4')],{windowsHide:true,stdio:'inherit'});
  }
  finally{clearTimeout(timer);fs.writeFileSync(path.join(output,'native.log'),log);
    if(path.dirname(temporary)===path.resolve(os.tmpdir())&&path.basename(temporary).startsWith('variant1-composer-'))fs.rmSync(temporary,{recursive:true,force:true,maxRetries:5,retryDelay:150});}
}
async function native(){
  const {app,BrowserWindow,protocol}=require('electron'),boot=require('../electron-app-boot');
  const temporary=path.resolve(process.env.VARIANT1_COMPOSER_TEST_DIR||'');assert.equal(path.dirname(temporary),path.resolve(os.tmpdir()));assert.ok(path.basename(temporary).startsWith('variant1-composer-'));
  app.setPath('userData',path.join(temporary,'profile'));boot.applyGpuFlags(app);boot.registerVariant1Scheme(protocol);
  let win,passed=false;const checks=[],errors=[];
  const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  try{
    await app.whenReady();boot.registerVariant1Protocol(protocol,root);
    win=new BrowserWindow({show:false,width:1440,height:900,webPreferences:{sandbox:true,contextIsolation:true,nodeIntegration:false,backgroundThrottling:false}});
    win.webContents.on('console-message',event=>{if(event.level==='error')errors.push(event.message)});
    win.webContents.on('render-process-gone',(_,details)=>errors.push(JSON.stringify(details)));
    const run=source=>win.webContents.executeJavaScript(source,true);
    async function until(source,label){for(let i=0;i<100;i++){if(await run(source))return;await delay(30);}throw new Error('Timeout: '+label)}
    const click=selector=>run(`document.querySelector(${JSON.stringify(selector)}).click()`);
    const check=async(source,label)=>{assert.equal(await run(source),true,label);checks.push(label)};
    const capture=async(name,crop=false)=>{
      await run('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))');await delay(60);
      let rect;
      if(crop)rect=await run(`(()=>{const r=document.querySelector('.composer-zone').getBoundingClientRect();return {x:Math.floor(r.x),y:Math.floor(r.y),width:Math.ceil(r.width),height:Math.ceil(r.height)}})()`);
      fs.writeFileSync(path.join(output,name+'.png'),(await win.webContents.capturePage(rect)).toPNG());
    };
    const within=`(()=>{const c=document.querySelector('#composer').getBoundingClientRect();return ['#attach-button','#composer-voice','#model-button','.composer-mutation-control','.composer__send-group'].every(selector=>{const e=document.querySelector(selector),r=e.getBoundingClientRect();return r.width>0&&r.left>=c.left&&r.right<=c.right+1&&r.bottom<=c.bottom+1})})()`;
    await win.loadFile(path.join(temporary,'index.html'));win.show();win.focus();await until(`!!document.querySelector('#model-button')&&document.hasFocus()`,'mounted and focused');await delay(150);
    await check(within,'all existing controls fit the wide composer');await capture('composer-wide');await capture('composer-detail',true);
    await check(`!document.querySelector('.composer__footer,.composer-hint,.composer-disclaimer')&&!document.querySelector('#composer-input').hasAttribute('aria-describedby')`,'marked helper text and disclaimer are removed without a dangling description');
    await check(`!!document.querySelector('.composer__send-group #context-meter-button')`,'context meter is in the toolbar beside Send');
    await check(`!document.querySelector('.composer-mutation-control.is-energizing')`,'loading an already-enabled chat does not replay the activation flourish');
    await run('window.composerFixture.setMutation(false)');await delay(60);await click('.composer-mutation-control');
    await check(`document.querySelector('.composer-mutation-control').getAttribute('aria-checked')==='false'&&!document.querySelector('.is-energizing')`,'pending mutation does not visually claim a successful activation');
    await run('window.composerFixture.acknowledgeMutation()');await until(`!!document.querySelector('.composer-mutation-control.is-energizing')`,'confirmed activation');
    await capture('mutation-activation',true);await delay(1150);
    await check(`!document.querySelector('.is-energizing')&&document.querySelector('.composer-mutation-control').getAnimations({subtree:true}).every(a=>a.playState!=='running')`,'activation settles with no permanent animation loop');
    await click('#model-button');await until(`!!document.querySelector('[data-model-option]')`,'models loaded');
    await check(`document.activeElement?.getAttribute('aria-label')==='Search models'`,'model search receives keyboard focus');
    await click('.model-picker__options-trigger');
    await check(`!!document.querySelector('.model-picker__effort-menu')`,'reasoning options open by explicit action');await capture('model-picker');
    await run(`document.querySelector('#model-menu').dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}))`);
    await until(`!document.querySelector('#model-menu')`,'escape dismissal');
    await check(`document.activeElement?.id==='model-button'`,'Escape restores model trigger focus');
    await click('#model-button');await until(`document.querySelectorAll('[data-model-option]').length>1`,'models');
    await run(`document.querySelectorAll('[data-model-option]')[1].click()`);
    await until(`document.querySelector('#model-button').getAttribute('aria-busy')==='true'`,'setting pending');
    await check(`document.querySelector('[aria-label="Send message"]').disabled`,'Send waits for actual model acknowledgement');
    await check(`document.querySelector('.composer-mutation-control').disabled&&document.querySelector('.composer-mutation-control').title.includes('model change')`,'Mutation is disabled with an accurate reason while model settings apply');
    await run('window.composerFixture.acknowledge()');await until(`!document.querySelector('[aria-label="Send message"]').disabled`,'applied setting');
    await check(`document.querySelector('#model-button').textContent.includes('reasoning fixture')`,'confirmed route is shown');
    await run('void window.composerFixture.prepare()');await until(`!!document.querySelector('.composer-preparation')`,'attachment preparation');
    await check(`document.querySelector('[aria-label="Send message"]').disabled`,'Send cannot omit a file still being prepared');
    await until(`!!document.querySelector('.context-chip')&&!document.querySelector('.composer-preparation')`,'file ready');
    await capture('attachment-ready',true);
    await click('[aria-label="Send message"]');await until(`window.composerFixture.commands.some(c=>c.type==='chat')`,'send');
    await check(`window.composerFixture.commands.find(c=>c.type==='chat').attachments[0].text==='Native attachment content'`,'prepared file reaches the actual composer send');
    await run('window.composerFixture.seed(true)');await delay(100);
    await check(within,'active delivery controls fit with Mutation and model controls');await capture('composer-working',true);
    win.setSize(850,680);await delay(180);await check(within,'all controls remain available in a narrow chat panel');await capture('composer-narrow');
    await click('#context-meter-button');await until(`!!document.querySelector('#context-meter-menu')`,'context panel');
    await check(`(()=>{const r=document.querySelector('#context-meter-menu').getBoundingClientRect();return r.top>=7&&r.left>=7&&r.right<=innerWidth-7&&r.bottom<=innerHeight-7})()`,'context panel is bounded to the viewport');
    await capture('context-narrow');
    await run(`document.querySelector('#context-meter-menu').dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}))`);
    await until(`!document.querySelector('#context-meter-menu')`,'context closed');
    await check(`!!document.querySelector('[aria-label="Pause task"]')&&!document.querySelector('[aria-label="Stop response"]')`,'running exposes Pause, not Stop');
    await click('[aria-label="Pause task"]');
    await until(`document.querySelector('#composer-status')?.textContent.includes('Pausing after current step')`,'pause request accepted');
    await check(`!document.querySelector('[aria-label="Stop response"]')`,'pausing does not expose Stop before the boundary');
    await run('window.composerFixture.pauseAtBoundary()');
    await until(`!!document.querySelector('[aria-label="Resume task"]')&&!!document.querySelector('[aria-label="Stop response"]')`,'paused controls');
    await click('[aria-label="Stop response"]');await click('[aria-label="Stop response"]');
    await check(`window.composerFixture.commands.filter(c=>c.type==='cancel').length===1`,'repeated Stop clicks emit one cancellation request');
    await check(`document.querySelector('[aria-label="Stop response"]').disabled`,'Stop shows pending until task settlement');
    win.focus();await until('document.hasFocus()','motion study focus');
    await run('window.composerFixture.showMotionStudy()');await until(`!!document.querySelector('#motion-study .mutation-decoration')`,'motion study');
    win.focus();await until(`document.querySelector('#motion-study .composer-mic-glyph').dataset.motion==='on'`,'motion study active');await delay(100);
    await check(`document.querySelector('#motion-study .mic-glyph__wave').getAnimations({subtree:true}).some(a=>a.playState==='running')`,'recording state animates its decorative waveform');
    await run('window.composerFixture.reduceMotion(true)');await delay(100);
    await check(`document.querySelector('#motion-study').getAnimations({subtree:true}).every(a=>a.playState!=='running')`,'reduced motion stops mic and mutation animation');
    await run('window.composerFixture.reduceMotion(false)');await delay(100);
    win.hide();await until(`document.querySelector('#motion-study .composer-mic-glyph').dataset.motion==='off'`,'hidden motion suspended');
    await check(`document.querySelector('#motion-study').getAnimations({subtree:true}).every(a=>a.playState!=='running')`,'hidden windows do not keep the micro animations running');
    win.show();win.focus();await until(`document.querySelector('#motion-study .composer-mic-glyph').dataset.motion==='on'`,'visible motion resumed');
    if(process.argv.includes('--record-motion')){
      const frames=path.join(temporary,'frames');fs.mkdirSync(frames);
      const rect=await run(`(()=>{const r=document.querySelector('#motion-study').getBoundingClientRect();return {x:Math.floor(r.x),y:Math.floor(r.y),width:Math.ceil(r.width),height:Math.ceil(r.height)}})()`);
      const recordingStarted=Date.now();
      for(let frame=0;frame<40;frame++){
        if(frame===5)await click('#motion-study .composer-mutation-control');
        fs.writeFileSync(path.join(frames,'frame-'+String(frame).padStart(4,'0')+'.png'),(await win.webContents.capturePage(rect)).toPNG());await delay(Math.max(0,recordingStarted+(frame+1)*50-Date.now()));
      }
    }
    assert.deepEqual(errors,[]);passed=true;console.log('Composer native acceptance passed: '+checks.length+' interaction/layout checks; no model, backend or microphone.');
  }catch(error){errors.push(error.stack);console.error(error);if(win&&!win.isDestroyed())fs.writeFileSync(path.join(output,'failure.png'),(await win.webContents.capturePage()).toPNG());}
  finally{fs.writeFileSync(path.join(output,'native-receipt.json'),JSON.stringify({passed,checks,errors,fixture:'actual Deck components with deterministic local protocol callbacks; no model/backend/microphone'},null,2));win?.destroy();app.exit(passed?0:1);}
}
(process.versions.electron?native():launch()).catch(error=>{console.error(error);process.exitCode=1});
