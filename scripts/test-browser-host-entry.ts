import assert from "node:assert/strict";
import {registerWorkbenchBrowser, runWorkbenchBrowserCommand, waitForWorkbenchBrowser, type WorkbenchWebview} from "../frontend/main-deck/src/workbench/browserBridge";
import {browserDeadline} from "../frontend/main-deck/src/workbench/browserLifecycle";
import {ingestBrowserHost, setBrowserHostConnection, setBrowserHostContext} from "../frontend/main-deck/src/workbench/browserHostBridge";
import {adoptBrowserTab, getPreviewState} from "../frontend/main-deck/src/workbench/previewStore";
import {applyBrowserViewport, browserViewportSize} from "../frontend/main-deck/src/workbench/browserViewport";
import {findGroupOfPane} from "../frontend/main-deck/src/workbench/layoutModel";
import {getWorkbenchState} from "../frontend/main-deck/src/workbench/workbenchStore";
import {browserKeyInput} from "../frontend/main-deck/src/workbench/browserKeyboard";

const pause = (ms = 0) => new Promise(resolve => setTimeout(resolve, ms));
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(yes => resolve = yes); return {promise, resolve}; }

export async function run() {
  const guest = document.createElement("webview") as WorkbenchWebview;
  document.body.appendChild(guest);
  guest.getBoundingClientRect = () => ({width: 640, height: 400, top: 0, left: 0, right: 640, bottom: 400, x: 0, y: 0, toJSON() {}});
  const page = () => ({title: "Local page", url: "about:blank", loading: false, canGoBack: false, canGoForward: false});
  const unregister = registerWorkbenchBrowser("e01-a", guest, page);
  adoptBrowserTab("e01-a", "about:blank");
  assert.equal(await waitForWorkbenchBrowser("e01-a", 5), false, "registration is not dom-ready");
  guest.dispatchEvent(new Event("dom-ready"));
  assert.equal(await waitForWorkbenchBrowser("e01-a"), true);
  await assert.rejects(runWorkbenchBrowserCommand({action: "state", tab_id: "missing"}), /Requested browser tab/);

  const navigate = () => { guest.dispatchEvent(Object.assign(new Event("did-start-navigation"), {isMainFrame: true, isInPlace: false})); guest.dispatchEvent(new Event("dom-ready")); };
  let read = deferred<unknown>(); guest.executeJavaScript = () => read.promise;
  const pendingRead = runWorkbenchBrowserCommand({action: "read", tab_id: "e01-a"}); await pause(); navigate(); read.resolve({text: "old document"});
  await assert.rejects(pendingRead, /document changed/, "old text cannot be paired with a new document");

  let epoch = 0;
  guest.executeJavaScript = async code => { epoch = Number(/const epoch = (\d+)/.exec(code)?.[1]); return {elements: [{ref: `b${epoch}-1`}]}; };
  await runWorkbenchBrowserCommand({action: "read", tab_id: "e01-a"});
  const point = deferred<unknown>(); guest.executeJavaScript = () => point.promise;
  const inputs: unknown[] = []; guest.sendInputEvent = event => { inputs.push(event); };
  const click = runWorkbenchBrowserCommand({action: "click", tab_id: "e01-a", target: `b${epoch}-1`});
  await pause(); navigate(); point.resolve({x: 20, y: 20});
  await assert.rejects(click, /document changed/); assert.equal(inputs.length, 0, "stale coordinates never become native input");
  await assert.rejects(runWorkbenchBrowserCommand({action: "click", tab_id: "e01-a", target: `b${epoch}-1`}), /stale/);

  guest.getWebContentsId = () => 4;
  window.variant1Deck = {captureWorkbenchPreview: async () => ({ok:false, error:"UnknownVizError"})};
  await assert.rejects(runWorkbenchBrowserCommand({action: "screenshot", tab_id: "e01-a"}), error => (error as {code?: string}).code === "CAPTURE_FAILED");
  const slow = deferred<number>(); await assert.rejects(browserDeadline(slow.promise, 5, "capture"), /capture timed out/); slow.resolve(1);

  const first: unknown[] = [], second: Array<any> = [], logs: string[] = [];
  const ctx = (sent: unknown[]) => ({send: (value: unknown) => { sent.push(value); return true; }, notify() {}, api: {log: (value: string) => logs.push(value)}});
  setBrowserHostContext(ctx(first)); setBrowserHostConnection("connected");
  const evaluation = deferred<unknown>(); guest.executeJavaScript = () => evaluation.promise;
  const old = ingestBrowserHost({type: "browser:host:command", id: "old", command: {action: "evaluate", tab_id: "e01-a", expression: "1"}});
  await pause(); setBrowserHostConnection("offline"); setBrowserHostContext(ctx(second)); setBrowserHostConnection("connected");
  evaluation.resolve(1); await old;
  assert.equal([...first, ...second].filter((value: any) => value.type === "browser:host:result").length, 0, "old completions never enter a new socket");
  await ingestBrowserHost({type: "browser:host:command", id: "new", command: {action: "state", tab_id: "e01-a"}});
  assert.equal(second.find(value => value.id === "new").result.ok, true);
  assert.ok(logs.some(line => line.includes('"delivered":false')));
  await ingestBrowserHost({type: "browser:host:command", id: "missing", command: {action: "read", tab_id: "missing"}});
  assert.equal(second.find(value => value.id === "missing").result.code, "TAB_NOT_FOUND");
  assert.equal(getPreviewState().tabs.some(tab => tab.id === "missing"), false, "a closed explicit target cannot silently resurrect");
  const beforeInvalidKeys = getPreviewState();
  await ingestBrowserHost({type:"browser:host:command",id:"invalid-keys",command:{action:"keys",keys:"ImaginaryKey"}});
  assert.equal(second.find(value=>value.id==="invalid-keys").result.code,"UNSUPPORTED_BROWSER_KEY");
  assert.equal(getPreviewState(),beforeInvalidKeys,"unsupported keys cannot create/select/reveal another tab");

  const surface = document.createElement("div"), host = document.createElement("div");
  surface.appendChild(host); document.body.appendChild(surface); host.appendChild(guest);
  Object.defineProperties(surface, {clientWidth: {value: 280}, clientHeight: {value: 597}});
  const rect = (width: number, height: number) => ({width, height, top: 0, left: 0, right: width, bottom: height, x: 0, y: 0, toJSON() {}});
  surface.getBoundingClientRect = () => rect(280, 597);
  guest.getBoundingClientRect = () => rect(host.style.width.startsWith("clamp") ? 800 : Number.parseFloat(host.style.width), host.style.height.startsWith("clamp") ? 597 : Number.parseFloat(host.style.height));
  applyBrowserViewport(guest);
  assert.deepEqual(browserViewportSize({width: 182, height: 597}), {width: 800, height: 597}, "a narrow dock keeps a readable rendered page");
  const hiddenSurface = document.createElement("div"), hiddenHost = document.createElement("div"), retainedGuest = document.createElement("webview");
  hiddenSurface.appendChild(hiddenHost); hiddenHost.appendChild(retainedGuest);
  hiddenHost.style.width = "1000px"; hiddenHost.style.height = "700px";
  applyBrowserViewport(retainedGuest);
  assert.equal(hiddenHost.style.width, "1000px"); assert.equal(hiddenHost.style.height, "700px", "hidden wrappers cannot reset a retained automatic viewport");
  const invalid = () => runWorkbenchBrowserCommand({action: "set_viewport", tab_id: "e01-a", width: 182, height: 597});
  await assert.rejects(invalid, error => (error as {code: string}).code === "INVALID_VIEWPORT");
  assert.equal(getPreviewState().tabs.find(tab => tab.id === "e01-a")?.viewport, undefined);
  const sized = await runWorkbenchBrowserCommand({action: "set_bounds", tab_id: "e01-a", width: 1280, height: 720});
  assert.deepEqual(sized.viewport, {width: 1280, height: 720, visible_width: 280, visible_height: 597, device_scale_factor: 1, mode: "fixed"});
  assert.deepEqual(getPreviewState().tabs.find(tab => tab.id === "e01-a")?.viewport, {width: 1280, height: 720});
  window.variant1Deck = {captureWorkbenchPreview: async () => ({ok: true, image: "png", image_width: 2560, image_height: 1440})};
  const screenshot = await runWorkbenchBrowserCommand({action: "screenshot", tab_id: "e01-a"});
  assert.equal(screenshot.image_width, 2560, "PNG dimensions remain distinct from CSS viewport pixels");
  assert.equal((screenshot.viewport as {width: number}).width, 1280);
  const capturePending = deferred<{ok: boolean; image: string}>();
  const captureStarted = deferred<void>();
  window.variant1Deck = {captureWorkbenchPreview: () => { captureStarted.resolve(); return capturePending.promise; }};
  const changingCapture = runWorkbenchBrowserCommand({action: "screenshot", tab_id: "e01-a"});
  await captureStarted.promise; host.style.width = "900px"; capturePending.resolve({ok: true, image: "old-size"});
  await assert.rejects(changingCapture, error => (error as {code: string}).code === "VIEWPORT_CHANGED", "resizing cannot pair old pixels with new viewport metadata");
  await runWorkbenchBrowserCommand({action: "set_viewport", tab_id: "e01-a", mode: "auto"});
  assert.equal(host.style.width, "clamp(800px, 100%, 3840px)","automatic size follows CSS instead of waiting on JS pixel updates");

  const pageDocument = document.implementation.createHTMLDocument("Page");
  const pageButton = pageDocument.createElement("button"); pageButton.textContent = "Download";
  pageButton.getBoundingClientRect = () => rect(80, 24); pageDocument.body.appendChild(pageButton);
  const password = pageDocument.createElement("input"); password.type = "password"; password.value = "test-only-password";
  password.getBoundingClientRect = () => rect(120, 24); pageDocument.body.appendChild(password);
  guest.executeJavaScript = async source => new Function("document", "location", "getComputedStyle", `return ${source}`)(pageDocument, {href: "https://page.test/"}, window.getComputedStyle.bind(window));
  const withoutElements = await runWorkbenchBrowserCommand({action: "read", tab_id: "e01-a", max_elements: 0});
  assert.deepEqual(withoutElements.elements, [], "explicit zero requests no element collection");
  const withDefaults = await runWorkbenchBrowserCommand({action: "read", tab_id: "e01-a", max_elements: null});
  assert.equal((withDefaults.elements as unknown[]).length, 2, "null retains the existing element-collection default");
  assert.equal((withDefaults.elements as Array<{input_type: string}>)[1].input_type, "password");
  assert.equal(JSON.stringify(withDefaults).includes("test-only-password"), false, "login readiness never needs the password in a value or fallback name");

  const secondGuest = document.createElement("webview") as WorkbenchWebview;
  document.body.appendChild(secondGuest); secondGuest.getBoundingClientRect = () => rect(800, 480);
  const unregisterSecond = registerWorkbenchBrowser("e09-b", secondGuest, page); secondGuest.dispatchEvent(new Event("dom-ready"));
  await ingestBrowserHost({type: "browser:host:command", id: "new-tab", command: {action: "new_page", tab_id: "e09-b", url: "about:blank"}});
  const layout = getWorkbenchState().layout;
  assert.equal(findGroupOfPane(layout, "preview:e01-a")?.id, findGroupOfPane(layout, "preview:e09-b")?.id,
    "host-created browsers stack before React effects, rather than consuming another column");
  assert.equal(second.find(value => value.id === "new-tab").result.ok, true);
  unregisterSecond(); secondGuest.remove(); surface.remove();
  unregister(); guest.remove(); setBrowserHostConnection("offline");
  const recoveryGuest = document.createElement("webview") as WorkbenchWebview;
  document.body.appendChild(recoveryGuest); recoveryGuest.getBoundingClientRect = () => rect(800, 480);
  const unregisterRecovery = registerWorkbenchBrowser("e11-recovery", recoveryGuest, () => ({...page(), url:"https://retained.test/"}));
  recoveryGuest.dispatchEvent(new Event("dom-ready"));
  const startNavigation = () => recoveryGuest.dispatchEvent(Object.assign(new Event("did-start-navigation"), {isMainFrame:true, url:"https://retained.test/download"}));
  let retained = deferred<unknown>(); recoveryGuest.executeJavaScript = () => retained.promise;
  startNavigation(); recoveryGuest.dispatchEvent(new Event("did-stop-loading"));
  retained.resolve({url:"https://retained.test/",ready:"complete"}); await pause();
  assert.equal(recoveryGuest.dataset.browserReady,"true","a retained committed document recovers after download settlement");
  retained = deferred<unknown>(); startNavigation(); recoveryGuest.dispatchEvent(new Event("did-stop-loading"));
  startNavigation(); retained.resolve({url:"https://retained.test/",ready:"complete"}); await pause();
  assert.equal(recoveryGuest.dataset.browserReady,"false","a delayed probe cannot mark the next navigation ready");
  assert.equal(await waitForWorkbenchBrowser("e11-recovery",10,undefined,false,false),true,"recovery requires attachment, not DOM readiness");
  const recovering = await runWorkbenchBrowserCommand({action:"state",tab_id:"e11-recovery"});
  assert.equal((recovering.state as {document_ready:boolean}).document_ready,false);
  await assert.rejects(runWorkbenchBrowserCommand({action:"read",tab_id:"e11-recovery"}), error => (error as {code:string}).code === "GUEST_NOT_READY");
  let reloads=0; recoveryGuest.reload=()=>{reloads++;};
  await runWorkbenchBrowserCommand({action:"reload",tab_id:"e11-recovery"}); assert.equal(reloads,1);
  recoveryGuest.loadURL=async()=>{recoveryGuest.dispatchEvent(new Event("dom-ready"));};
  assert.equal((await runWorkbenchBrowserCommand({action:"navigate",tab_id:"e11-recovery",url:"https://recovered.test/"})).ok,true);
  retained=deferred<unknown>(); startNavigation(); recoveryGuest.dispatchEvent(new Event("did-stop-loading"));
  unregisterRecovery(); recoveryGuest.remove();retained.resolve({url:"https://retained.test/",ready:"complete"});await pause();
  assert.equal(recoveryGuest.dataset.browserReady,"false","a disposed guest cannot be revived by its old readiness probe");
  console.log("E01: guest readiness, missing targets, navigation fences, stale input prevention, capture errors/deadlines, and connection ownership passed");
  console.log("E09: host tab grouping, automatic minimum viewport, explicit sizing validation, and CSS/PNG dimensions passed");
  console.log("E11: retained-document recovery, attached recovery controls, unreadable-page fences and stale/disposed probes passed");
  await testClickPositions();
  await testKeyboardInput();
  await testElementIdentity();
}

async function testKeyboardInput() {
  for(const direction of ["Right","Left","Up","Down"]) {
    assert.equal(browserKeyInput(`Arrow${direction}`).keyCode,direction);
    assert.equal(browserKeyInput(`ARROW${direction.toUpperCase()}`).keyCode,direction);
  }
  for(const key of ["Home","End","Tab","Escape","F1","F24","num0","numadd","MediaPlayPause","VolumeMute","PrintScreen","AltGr","Super"]) {
    assert.equal(browserKeyInput(key).keyCode.toLowerCase(),key.toLowerCase());
  }
  for(const key of ["a","A","!","+"," "]) assert.equal(browserKeyInput(key).keyCode,key);
  assert.deepEqual(browserKeyInput("Control++"),{keyCode:"+",modifiers:["control"]});
  assert.deepEqual(browserKeyInput("Ctrl+Shift+ArrowLeft"),{keyCode:"Left",modifiers:["control","shift"]});
  assert.deepEqual(browserKeyInput("Cmd+ArrowUp"),{keyCode:"Up",modifiers:["meta"]});
  const guest=document.createElement("webview") as WorkbenchWebview;document.body.appendChild(guest);
  let focusCalls=0,bindCalls=0;
  const inputs:Array<Record<string,unknown>>=[];
  guest.getBoundingClientRect=()=>({width:800,height:600,left:0,top:0,right:800,bottom:600,x:0,y:0,toJSON(){}});
  guest.getWebContentsId=()=>123;
  guest.executeJavaScript=async()=>{focusCalls++;return true;};
  guest.sendInputEvent=event=>inputs.push(event);
  const previousApi=window.variant1Deck;
  window.variant1Deck={bindWorkbenchBrowser:async()=>{bindCalls++;return {ok:true};}};
  const unregister=registerWorkbenchBrowser("keyboard-test",guest,()=>({title:"keys",url:"about:blank",loading:false,canGoBack:false,canGoForward:false}));
  guest.dispatchEvent(new Event("dom-ready"));
  const command=(keys:unknown)=>runWorkbenchBrowserCommand({action:"keys",tab_id:"keyboard-test",keys});
  try {
    for(const keys of ["",null,"ImaginaryKey","KeyA","Digit1","é","Control+","Unknown+ArrowRight","constructor+A","Control++x","++ArrowRight"]) {
      const before={focusCalls,bindCalls,count:inputs.length};
      await assert.rejects(runWorkbenchBrowserCommand({action:"keys",tab_id:"keyboard-test",target:"b1-1",keys}),error=>(error as {code:string}).code==="UNSUPPORTED_BROWSER_KEY");
      assert.deepEqual({focusCalls,bindCalls,count:inputs.length},before,"reject before binding or target focus/input");
    }
    await command("ArrowRight");
    assert.deepEqual(inputs.map(event=>event.keyCode),["Right","Right"]);
    inputs.length=0;await command("Shift+A");
    assert.deepEqual(inputs,[{type:"keyDown",keyCode:"A",modifiers:["shift"]},{type:"char",keyCode:"A",modifiers:["shift"]},{type:"keyUp",keyCode:"A",modifiers:["shift"]}]);
    inputs.length=0;await command("Control+A");
    assert.deepEqual(inputs.map(event=>event.type),["keyDown","keyUp"],"shortcuts never inject a character");
    console.log("Embedded keyboard: canonical arrows, accelerator compatibility, printable case/plus/space, modifiers and pre-focus/bind/input rejection passed");
  } finally {unregister();guest.remove();window.variant1Deck=previousApi;}
}

async function testClickPositions() {
  const guest = document.createElement("webview") as WorkbenchWebview;
  const target = document.createElement("button");
  document.body.append(guest, target);
  const box = (left: number, top: number, width: number, height: number) => ({left, top, width, height, right:left+width, bottom:top+height, x:left, y:top, toJSON() {}});
  guest.getBoundingClientRect = () => box(400, 100, 800, 600);
  let targetBox = box(100, 50, 200, 40), zoom = 1, evaluations = 0;
  target.getBoundingClientRect = () => targetBox;
  let fragments: DOMRect[] | null = null;
  target.getClientRects = () => (fragments || [targetBox]) as unknown as DOMRectList;
  const priorHitTest = document.elementFromPoint;
  let hitTest: (x:number,y:number)=>Element|null = () => target;
  document.elementFromPoint = (x,y) => hitTest(x,y);
  target.scrollIntoView = () => {};
  target.style.borderLeft = "3px solid black"; target.style.borderTop = "5px solid black";
  guest.getZoomFactor = () => zoom;
  const events: Array<Record<string, unknown>> = [];
  guest.sendInputEvent = event => events.push(event);
  const unregister = registerWorkbenchBrowser("position-test", guest, () => ({title:"seek", url:"about:blank", loading:false, canGoBack:false, canGoForward:false}));
  guest.dispatchEvent(new Event("dom-ready"));
  target.setAttribute("aria-label","position target");
  guest.executeJavaScript = async code => {
    evaluations++;
    // Run the production guest script, using CSS viewport geometry independent
    // of the host pane's screen location and device pixel ratio.
    return new Function("document","getComputedStyle","innerWidth","innerHeight","devicePixelRatio","location",`return ${code}`)(document,window.getComputedStyle.bind(window),800/zoom,600/zoom,2,{href:"about:blank"});
  };
  const observed=await runWorkbenchBrowserCommand({action:"read",tab_id:"position-test"});
  const ref=(observed.elements as Array<{ref:string;name:string}>).find(row=>row.name==="position target")!.ref;
  const click = (extra: Record<string, unknown> = {}) => runWorkbenchBrowserCommand({action:"click",tab_id:"position-test",target:ref,...extra});
  const verify = async (extra: Record<string, unknown>, x: number, y: number) => {
    events.length=0;await click(extra);
    assert.deepEqual(events.map(event=>({type:event.type,x:event.x,y:event.y})),["mouseMove","mouseDown","mouseUp"].map(type=>({type,x,y})));
  };
  try {
    await verify({},200,70);
    await verify({position:null},200,70);
    await verify({position:{x:0,y:0}},103,55);
    await verify({position:{x:37.5,y:8.25},button:"right",click_count:2},141,63);
    assert.equal(events[1].button,"right");assert.equal(events[1].clickCount,2);
    fragments=[box(180,50,120,16),box(100,80,30,16)] as DOMRect[];
    await verify({},240,58);
    hitTest=(x)=>x>200 ? document.body : target;
    await verify({},115,88);
    const child=document.createElement("span");target.appendChild(child);hitTest=()=>child;
    await verify({},240,58);child.remove();
    hitTest=()=>document.body;events.length=0;
    await assert.rejects(click(),error=>(error as {code:string}).code==="CLICK_TARGET_BLOCKED");assert.equal(events.length,0);
    await verify({force:true},240,58);
    await verify({position:{x:0,y:0}},103,55); // explicit coordinates are not retargeted or newly hit-gated
    let checks=0;hitTest=()=>{checks++;return document.body;};
    fragments=Array.from({length:65},()=>box(100,50,20,20)) as DOMRect[];
    await assert.rejects(click(),error=>(error as {code:string}).code==="CLICK_TARGET_BLOCKED");assert.equal(checks,64);
    fragments=[box(-10,20,30,20)] as DOMRect[];hitTest=()=>target;
    await verify({},10,30);
    fragments=null;
    // Offsets are pixels, including negative offsets; they are not normalized
    // percentages or forcibly clipped to the element rectangle.
    await verify({position:{x:-2,y:0}},101,55);
    target.scrollIntoView=()=>{targetBox=box(20,10,200,40);};
    await verify({position:{x:37.5,y:8.25}},61,23);
    target.scrollIntoView=()=>{};
    zoom=1.5;
    await verify({position:{x:37.5,y:8.25}},91,35);
    zoom=1;
    for(const position of [[],"x",true,{},{x:1},{x:"2",y:3},{x:NaN,y:1},{x:1,y:Infinity}]) {
      events.length=0;const before=evaluations;
      await assert.rejects(click({position}),error=>(error as {code:string}).code==="INVALID_CLICK_POSITION");
      assert.equal(evaluations,before);assert.equal(events.length,0);
    }
    for(const position of [{x:-100,y:0},{x:800,y:0},{x:0,y:600},{x:776.9,y:0}]) {
      events.length=0;
      await assert.rejects(click({position}),error=>(error as {code:string}).code==="CLICK_OUTSIDE_VIEWPORT");
      assert.equal(events.length,0,"offscreen or rounded-outside points never become input");
    }
    targetBox=box(0,0,0,0);events.length=0;
    await assert.rejects(click(),error=>(error as {code:string}).code==="INVALID_CLICK_GEOMETRY");assert.equal(events.length,0);
    targetBox=box(20,10,200,40);
    const execute=guest.executeJavaScript;
    guest.executeJavaScript=async code=>{const result=await execute(code);zoom=2;return result;};
    await assert.rejects(click({position:{x:2,y:3}}),error=>(error as {code:string}).code==="VIEWPORT_CHANGED");assert.equal(events.length,0);
    console.log("Embedded click positions: actual guest script, center/null defaults, padding borders, CSS offsets, post-scroll geometry, zoom/DPR separation, button/count, invalid/offscreen/zero-size/zoom-change guards passed");
  } finally {unregister();guest.remove();target.remove();document.elementFromPoint=priorHitTest;}
}

async function testElementIdentity() {
  const guest=document.createElement("webview") as WorkbenchWebview;document.body.appendChild(guest);
  const pageDocument=document.implementation.createHTMLDocument("identity fixture");
  const geometry=()=>({width:100,height:30,left:0,top:0,right:100,bottom:30,x:0,y:0,toJSON(){}});
  guest.getBoundingClientRect=geometry;
  let focused="",inputs=0;
  const button=(id:string)=>{const node=pageDocument.createElement("button");node.id=id;node.setAttribute("aria-label",id);node.getBoundingClientRect=geometry;node.focus=()=>{focused=id;};return node;};
  const first=button("first"),second=button("second");pageDocument.body.append(first,second);
  guest.executeJavaScript=async code=>new Function("document","location","getComputedStyle",`return ${code}`)(pageDocument,{href:"https://identity.test/"},window.getComputedStyle.bind(window));
  guest.sendInputEvent=()=>{inputs++;};
  const page=()=>({title:"identity",url:"https://identity.test/",loading:false,canGoBack:false,canGoForward:false});
  let unregister=registerWorkbenchBrowser("identity-test",guest,page);guest.dispatchEvent(new Event("dom-ready"));
  const read=async(extra:Record<string,unknown>={})=>(await runWorkbenchBrowserCommand({action:"read",tab_id:"identity-test",...extra})).elements as Array<{ref:string;name:string}>;
  const keys=(ref:string)=>runWorkbenchBrowserCommand({action:"keys",tab_id:"identity-test",target:ref,keys:"Enter"});
  const stale=async(ref:string)=>{const before=inputs;await assert.rejects(keys(ref),/stale/);assert.equal(inputs,before);};
  try {
    const original=await read(),ref=original[1].ref;
    assert.equal((await read())[1].ref,ref);await keys(ref);assert.equal(focused,"second");
    await read({max_elements:0});await read({max_elements:1});await keys(ref);assert.equal(focused,"second");
    const inserted=button("inserted");pageDocument.body.prepend(inserted);pageDocument.body.prepend(second);
    const reordered=await read();assert.equal(reordered[0].ref,ref);assert.notEqual(reordered.find(row=>row.name==="inserted")!.ref,ref);
    second.setAttribute("data-variant1-browser-ref",original[0].ref);await keys(ref);assert.equal(focused,"second","marker edits cannot redirect registry identity");
    const clone=second.cloneNode(true) as HTMLButtonElement;clone.id="clone";clone.setAttribute("aria-label","clone");clone.getBoundingClientRect=geometry;clone.focus=()=>{focused="clone";};pageDocument.body.prepend(clone);
    const cloned=await read();assert.notEqual(cloned.find(row=>row.name==="clone")!.ref,ref);await keys(ref);assert.equal(focused,"second");
    second.remove();await stale(ref);pageDocument.body.append(second);const reattached=(await read()).find(row=>row.name==="second")!.ref;
    assert.notEqual(reattached,ref);await stale(ref);await keys(reattached);assert.equal(focused,"second");
    const replacement=button("replacement");replacement.setAttribute("data-variant1-browser-ref",reattached);second.replaceWith(replacement);
    await read();await stale(reattached);const fresh=(await read()).find(row=>row.name==="replacement")!.ref;
    guest.dispatchEvent(Object.assign(new Event("did-start-navigation"),{isMainFrame:true,isInPlace:true}));await read();await keys(fresh);
    guest.dispatchEvent(Object.assign(new Event("did-start-navigation"),{isMainFrame:false,isInPlace:false}));await keys(fresh);
    const execute=guest.executeJavaScript!,pending=deferred<unknown>();guest.executeJavaScript=()=>pending.promise;
    const late=keys(fresh);await pause();
    guest.dispatchEvent(Object.assign(new Event("did-start-navigation"),{isMainFrame:true,isInPlace:false}));guest.dispatchEvent(new Event("dom-ready"));
    const before=inputs;pending.resolve(true);await assert.rejects(late,/document changed/);assert.equal(inputs,before);guest.executeJavaScript=execute;
    await read();await stale(fresh);
    const crashRef=(await read())[0].ref;guest.dispatchEvent(new Event("render-process-gone"));guest.dispatchEvent(new Event("dom-ready"));await read();await stale(crashRef);
    const oldGuestRef=(await read())[0].ref;unregister();unregister=registerWorkbenchBrowser("identity-test",guest,page);guest.dispatchEvent(new Event("dom-ready"));await read();await stale(oldGuestRef);
    console.log("Embedded element identity: rereads/limits/zero reads, insertion/reorder, marker tampering, clone/replacement/detach, same-document vs real navigation, crash and guest replacement fences passed");
  } finally {unregister();guest.remove();}
}
