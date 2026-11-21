'use strict';

// Opt-in native diagnostic: node scripts/test-chat-ownership-native.js --measure-peers
// VARIANT1_REPLAY_APP_ROOT may point at another pinned, built Deck. The local
// admission fixture, workload and timing driver remain identical for both builds.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {performance} = require('node:perf_hooks');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const ids = ['A', 'B', 'C'];

function prepare(sessions) {
  const ts = Date.now() / 1000 - 120;
  for (const id of ids) {
    const messages = [];
    for (let i = 0; i < 32; i++) {
      messages.push({role:'user', text:id+' history question '+i, ts:ts+i*2});
      messages.push({role:'assistant', text:id+' history reply '+i, ts:ts+i*2+1, run_id:id+'-history-'+i,
        steps:[{id:'step-'+i, kind:'tool', label:'Read file', status:'ok', detail:'Bounded fixture read', ts:(ts+i*2)*1000}]});
    }
    sessions.set(id, {id, title:'Peer '+id, messages, runtime:{busy:false}});
  }
}

async function measure({win, app, output, evaluate, until, select, send, emit, token, complete, route, sessions}) {
  const evidence = {method:'Local WebSocket replay; main-process dispatch to the second renderer animation frame after the observed DOM change.',
    initial_rows_per_chat:64, token_period_ms:100, stages:[], resources:[]};
  const frame = () => evaluate('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>resolve(true))))');
  async function timed(samples, operation, action) {
    const started = performance.now();
    await action();
    await frame();
    samples.push({operation, at_ms:Date.now(), elapsed_ms:Math.round((performance.now()-started)*10)/10});
  }
  let active = [];
  let ticks = 0;
  let stage = 'warmup';
  const resourceSample = () => evidence.resources.push({at_ms:Date.now(), stage,
    processes:app.getAppMetrics().map(({pid, creationTime, type, memory, cpu})=>({pid, creationTime, type,
      cpu_percent:cpu.percentCPUUsage, working_set_kib:memory.workingSetSize, private_kib:memory.privateBytes}))});
  const record = () => fs.writeFileSync(path.join(output,'latency.json'), JSON.stringify(evidence,null,2));
  const clock = setInterval(resourceSample,1000);
  const stream = setInterval(()=>{
    ticks++;
    for (const id of active) {
      token(id,' word');
      if (ticks%5 === 0) emit({type:'activity', ...route(id), event:'tool:result', tool:'read_file',
        call_id:id+'-read-'+ticks, status:'ok', args_preview:'{"path":"fixture.txt"}', text:'Read fixture'});
      if (active.length > 1 && ticks%10 === 0) {
        const other = active[(active.indexOf(id)+1)%active.length], call = id+'-peer-'+ticks, owner = route(id);
        emit({type:'activity', ...owner, event:'peer:sent', call_id:call, peer_message:{
          message_id:call, sender_peer_id:'chat:'+id, target_peer_id:'chat:'+other,
          target_display_name:'Peer '+other, content:'Fixture progress '+ticks, state:'queued',
          sender_invocation:{chat_id:id, run_id:owner.run_id, nested_call_id:call},
        }});
      }
    }
  },evidence.token_period_ms);
  try {
    // Visit each session through the sidebar so background streams have an owner.
    for (const id of ids) await select(id);
    for (const count of [0,1,2,3,0]) {
      if (count > active.length) {
        const id = ids[count-1];
        await select(id);
        await send('Replay work for '+id);
        active.push(id);
      } else if (!count && active.length) {
        for (const id of active) complete(id,'Settled '+id);
        active=[];
      }
      stage = count ? 'active-'+count : evidence.stages.length ? 'recovery' : 'idle';
      const samples=[];
      const phase={stage, started_ms:Date.now(), active:[...active], samples};
      evidence.stages.push(phase);
      await pause(400);
      for (let i=0;i<12;i++) {
        const id=ids[i%ids.length];
        await timed(samples,'switch',async()=>{
          await evaluate('document.querySelector(\'[data-session-id="'+id+'"] .history-item__select\').click()');
          await evaluate('new Promise((resolve,reject)=>{const start=performance.now();const check=()=>{if(document.querySelector(\'[data-session-id="'+id+'"] [aria-current="true"]\') && document.querySelector("#message-column")?.textContent.includes("'+id+' history"))resolve(true);else if(performance.now()-start>5000)reject(Error("switch snapshot timeout"));else requestAnimationFrame(check)};check()})');
        });
        await evaluate('(()=>{const input=document.querySelector("#composer-input");input.focus();input.select()})()');
        await timed(samples,'type',async()=>{
          const value='draft '+id+' '+stage+' '+i;
          await win.webContents.insertText(value);
          assert.equal(await evaluate('document.querySelector("#composer-input").value'),value);
        });
        const scroll=await evaluate('(()=>{const s=document.querySelector("#messages-scroll"),r=s.getBoundingClientRect();return {top:s.scrollTop,x:Math.round(r.left+r.width/2),y:Math.round(r.top+r.height/2)}})()');
        await timed(samples,'scroll',async()=>{
          win.webContents.sendInputEvent({type:'mouseWheel',x:scroll.x,y:scroll.y,deltaY:scroll.top>200?140:-140,deltaX:0,canScroll:true});
          await evaluate('new Promise((resolve,reject)=>{const start=performance.now();const check=()=>{if(document.querySelector("#messages-scroll").scrollTop!=='+scroll.top+')resolve(true);else if(performance.now()-start>3000)reject(Error("wheel did not scroll"));else requestAnimationFrame(check)};check()})');
        });
        await evaluate('(()=>{const s=document.querySelector("#messages-scroll");s.scrollTo({top:s.scrollHeight,behavior:"instant"})})()');
        await frame();
        let traceKind;
        await timed(samples,'trace',async()=>{
          traceKind=await evaluate('(()=>{const live=document.querySelector(".chat-turn--live .execution-trace__summary, .chat-turn--live button.trace-entry__disclosure");const b=live||[...document.querySelectorAll(".execution-trace__summary,button.trace-entry__disclosure")].at(-1);if(!b)throw Error("missing trace control");const prev=b.getAttribute("aria-expanded");b.click();return new Promise(resolve=>requestAnimationFrame(()=>resolve(b.getAttribute("aria-expanded")!==prev?(live?"live":"history"):"unchanged")))})()');
          assert.notEqual(traceKind,'unchanged');
        });
        samples.at(-1).trace=traceKind;
        const content=await evaluate('document.querySelector("#message-column").textContent');
        for (const other of ids.filter(other=>other!==id)) assert.ok(!content.includes(other+' history'), 'foreign transcript visible');
        await pause(80);
      }
      resourceSample();
      phase.finished_ms=Date.now();
      record();
    }
    for (const id of ids) {
      await select(id);
      await until('settled transcript for '+id,()=>evaluate('!document.querySelector(".chat-turn--live") && document.querySelector("#message-column").textContent.includes("Settled '+id+'")'));
      assert.equal(sessions.get(id).runtime.busy,false);
    }
    console.log('Native peer replay measured five stages; latency.json includes individual timings and Electron process samples.');
  } finally { clearInterval(stream); clearInterval(clock); record(); }
}

module.exports={prepare,measure};
