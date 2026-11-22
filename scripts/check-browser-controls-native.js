'use strict';
const assert = require('node:assert/strict');

// The reveal has no focus/click handler. Only a real pointer hover exposes it.
const controlsPage = `<!doctype html><title>Browser controls</title>
<style>body{margin:48px}#reveal{margin:30px 90px}#revealed{display:none}#reveal:hover + #revealed{display:inline-block}</style>
<button id="reveal">Reveal field</button><input id="revealed" aria-label="Revealed field">
<select aria-label="Native choice"><option value="a">Alpha</option><option value="b">Beta</option></select>
<div tabindex="0" role="combobox" aria-label="Custom choice">Custom choice</div>
<aside id="delayed" hidden>Wait target</aside><script>
window.changes=0;document.querySelector('select').onchange=()=>window.changes++;
</script>`;

async function checkBrowserControls({command, evaluate, base}) {
  const owner = 'controls-owner', tab = 'controls-deliverable', run = 'controls-run';
  const send = (action, fields = {}) => command({action, owner_chat_id:owner, tab_id:tab, run_id:run, ...fields});
  const ok = async (action, fields) => {
    const result = await send(action, fields);
    assert.equal(result.ok, true, result.error);
    return result;
  };
  const js = async expression => (await ok('evaluate', {expression})).value;
  const read = async () => (await ok('read')).elements;
  const wait = (selector, state, timeout_ms = 2500) => send('wait', {condition:'element', selector, state, timeout_ms});
  const selectChat = id => evaluate('window.e01.selectChat('+JSON.stringify(id)+')');
  const expectWait = async (selector,state) => {
    const result = await wait(selector,state);
    assert.equal(result.ok,true,result.error);
    assert.deepEqual(result.value,{condition:'element',state});
  };
  await selectChat(owner);
  await ok('new_page', {url:base+'/controls'});
  assert.equal((await ok('activate_page',{visible:true})).shown,true);
  for (const zoom of [1.5,1]) {
    const zoomed=await evaluate('window.variant1Deck.workbenchBrowser('+JSON.stringify({action:'call',tabId:tab,method:'setZoomFactor',args:[zoom]})+')');
    assert.equal(zoomed.ok,true);
    const elements=await read(), reveal=elements.find(row=>row.name==='Reveal field');
    assert.ok(reveal.actions.includes('hover'));
    assert.ok(elements.find(row=>row.name==='Native choice').actions.includes('select'));
    assert.ok(!elements.find(row=>row.name==='Custom choice').actions.includes('select'));
    await ok('hover',{target:reveal.ref});
    await expectWait('#revealed','visible');
    assert.ok((await read()).some(row=>row.name==='Revealed field'));
    assert.equal(await js("document.activeElement.id==='reveal'"),false,'hover does not focus the button');
  }
  const dropdown=(await read()).find(row=>row.name==='Native choice').ref;
  assert.deepEqual((await ok('select',{target:dropdown,values:'b'})).value,['b']);
  assert.equal((await send('select',{target:dropdown,values:['missing']})).code,'INVALID_SELECT_OPTION');
  assert.deepEqual(await js("({value:document.querySelector('select').value,changes:window.changes})"),{value:'b',changes:1});

  await js("setTimeout(()=>{document.querySelector('#delayed').hidden=false},200); true");
  await expectWait('#delayed','visible');
  await js("setTimeout(()=>{document.querySelector('#delayed').hidden=true},200); true");
  await expectWait('#delayed','hidden');
  await expectWait('#delayed','attached');
  await js("setTimeout(()=>{document.querySelector('#delayed').remove()},200); true");
  await expectWait('#delayed','detached');
  assert.equal((await wait('input[','visible')).code,'INVALID_WAIT_SELECTOR');
  assert.equal((await wait('#never','attached',80)).code,'WAIT_TIMEOUT');
  await js("setTimeout(()=>location.assign('/controls?next'),250); true");
  assert.equal((await wait('#never','attached')).code,'DOCUMENT_CHANGED');
  await expectWait('select','visible');

  await js('window.evaluateCalls=0');
  const failure=await send('evaluate',{expression:'(window.evaluateCalls++, () => document.title)'});
  assert.equal(failure.code,'EVALUATION_RESULT_UNSERIALIZABLE');
  assert.equal(failure.phase,'evaluate');
  assert.deepEqual(await js('({calls:window.evaluateCalls,title:document.title})'),{calls:1,title:'Browser controls'});
  await ok('new_page',{tab_id:'controls-handoff',url:base+'/controls'});
  await ok('new_page',{tab_id:'controls-temporary',url:base+'/controls'});
  assert.equal((await ok('mark_page',{mark:'deliverable'})).marked,true);
  assert.equal((await ok('mark_page',{tab_id:'controls-handoff',mark:'handoff'})).marked,true);
  await selectChat('controls-foreground');
  assert.equal((await ok('activate_page',{visible:true})).shown,false);
  assert.equal(await evaluate('window.e01.displayedChat()'),'controls-foreground');
  await selectChat(owner);
  await evaluate('window.e01.toggleRightPanels()');
  assert.equal((await ok('activate_page',{visible:true})).shown,false,'agent respects a hidden right panel');
  await evaluate('window.e01.toggleRightPanels()');
  assert.equal((await ok('activate_page',{visible:true})).shown,true);
  assert.deepEqual((await ok('cleanup_run')).closed,['controls-temporary']);
  const kept=(await ok('tabs')).tabs.map(row=>[row.id,row.kept]).sort();
  assert.deepEqual(kept,[[tab,'deliverable'],['controls-handoff','handoff']]);
  for (const [tab_id] of kept) await ok('close_page',{tab_id});
  console.log('Native browser controls passed: CSS-only hover at two zoom levels, select, document-fenced waits, clone error and recovery, and scoped show/mark/cleanup.');
}

module.exports = {controlsPage, checkBrowserControls};
