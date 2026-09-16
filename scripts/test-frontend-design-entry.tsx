import assert from "node:assert/strict";
import {act} from "react";
import {createRoot} from "react-dom/client";
import {TurnActivity} from "../frontend/main-deck/src/chat/TurnActivity";
import {traceRows, traceSummary} from "../frontend/main-deck/src/chat/traceModel";
import {kernelSamples, kernelSeed, MUTATION_INK} from "../frontend/main-deck/src/motion/kernelField";
import {AppearanceBindings, getAppearance, setAppearance} from "../frontend/main-deck/src/state/appearanceStore";
import {AppearanceSettings} from "../frontend/main-deck/src/ui/AppearanceSettings";
import {ActionPalette, filterPaletteActions} from "../frontend/main-deck/src/ui/ActionPalette";
import {openPalette, closePalette} from "../frontend/main-deck/src/state/paletteStore";
import {__resetWorkbenchForTests, getWorkbenchState, hidePane, PANE} from "../frontend/main-deck/src/workbench/workbenchStore";
import {parseNativePlacements} from "../frontend/main-deck/src/workbench/nativeWindowStore";
import {ChatComposer} from "../frontend/main-deck/src/chat/ChatComposer";
import {initialChatState, setChatContext, setChatState, getChatState} from "../frontend/main-deck/src/chat/stateCore";
import {__resetTurnStoreForTests, turnController} from "../frontend/main-deck/src/state/turnStore";
import {__resetSessionStoreForTests, ingestSessions, noteDisplayedSession, setSessionContext} from "../frontend/main-deck/src/state/sessionStore";
import type {ChatTurnStep} from "../frontend/main-deck/src/chat/types";
import {parseTurnSteps} from "../frontend/main-deck/src/chat/messages";
import {pushTurnStep, settleRunningThinking} from "../frontend/main-deck/src/chat/turn";
import {activityPresentation} from "../frontend/main-deck/src/chat/activityPresentation";
import {traceActionLabel} from "../frontend/main-deck/src/chat/traceLabels";
import {HistoryRail} from "../frontend/main-deck/src/shell/HistoryRail";

const pause = (ms = 0) => new Promise(resolve => setTimeout(resolve, ms));
function key(node: Element, value: string) { node.dispatchEvent(new KeyboardEvent("keydown", {key: value, bubbles: true, cancelable: true})); }
async function change(input: HTMLInputElement, value: string) {
  await act(async () => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value); input.dispatchEvent(new Event("input", {bubbles: true})); });
}

export async function run() {
  setChatState({...initialChatState(), connected: true});
  const host = document.createElement("div"); document.body.appendChild(host);
  const root = createRoot(host);
  const cells: ChatTurnStep[] = [1, 2].map((generation, index) => ({id: `design-cell-${index}`, callId: `design-call-${index}`, kind: "tool", tool: "ipython", label: "Python cell", status: "ok", ts: 100,
    argsPreview: JSON.stringify({code: index ? "summary = sum(samples)" : "# Load measurements\nsamples = [12, 18]"}),
    resultPreview: JSON.stringify({execution_count: 41 + index, kernel_generation: generation})}));
  try {
    assert.deepEqual(traceRows(cells).map(row => row.step.id), cells.map(cell => cell.id));
    assert.deepEqual(traceRows(cells).map(row => row.boundary), ["Kernel 01", "Kernel 01 → 02"]);
    assert.equal(traceSummary(cells).cells, 2);
    await act(async () => root.render(<TurnActivity steps={cells} live={false} streamText="" turnStartedAt={100}/>));
    const summary = host.querySelector<HTMLButtonElement>('[aria-label="Execution trace"]')!;
    assert.equal(summary.getAttribute("aria-expanded"), "false");
    assert.equal(host.querySelectorAll(".trace-entry").length, 0);
    await act(async () => summary.click());
    assert.deepEqual([...host.querySelectorAll<HTMLElement>("[data-trace-id]")].map(row => row.dataset.traceId), cells.map(cell => cell.id));
    await act(async () => host.querySelector<HTMLButtonElement>('[aria-label="Ran Python, Python cell 41"]')!.click());
    assert.match(host.textContent!, /Load measurements/);
    await act(async () => summary.click());
    await act(async () => root.render(<TurnActivity steps={[cells[0], {...cells[1], status: "error", resultPreview: "NameError: samples"}]} live={false} streamText="" turnStartedAt={100}/>));
    assert.equal(summary.getAttribute("aria-expanded"), "true", "a newly failed cell reopens a collapsed run");
    assert.match(host.querySelector('.trace-entry.is-error')!.textContent!, /NameError/);

    assert.equal(host.querySelector('.execution-trace__owner'), null, 'main agent has no name or empty badge');
    const thought: ChatTurnStep = {id: 'visible-thought', kind: 'thinking', label: 'Thinking', detail: 'Check the browser connection before opening the page.', status: 'running', ts: Date.now()};
    await act(async () => root.render(<TurnActivity key="thought-test" steps={[thought]} live={true} streamText="" turnStartedAt={Date.now()}/>));
    assert.match(host.querySelector('.execution-trace__counts')!.textContent!, /1 thought/);
    assert.equal(host.querySelector('.trace-entry--thought button')!.getAttribute('aria-expanded'), 'true', 'live supplied thought is visible');
    await act(async () => host.querySelector<HTMLButtonElement>('.trace-entry--thought button')!.click());
    assert.match(host.querySelector('.trace-entry__headline')!.textContent!, /Check the browser/, 'collapsed thought still has a readable preview');
    await act(async () => root.render(<TurnActivity key="markdown-thought" steps={[{...thought,id:"markdown-thought",detail:"**Inspecting files**\n\nChecking `settings.json`."}]} live={true} streamText="" turnStartedAt={Date.now()}/>));
    assert.equal(host.querySelector('.trace-thought-content strong')?.textContent,"Inspecting files","thought headings render Markdown instead of literal asterisks");
    assert.equal(host.querySelector('.trace-thought-content code')?.textContent,"settings.json");
    const thoughtBody=host.querySelector('.trace-thought-content');
    await act(async () => root.render(<TurnActivity key="markdown-thought" steps={[{...thought,id:"markdown-thought",detail:"**Inspecting files**\n\nChecking `settings.json` and imports."}]} live={true} streamText="" turnStartedAt={Date.now()}/>));
    await act(async () => {await pause(30);});
    assert.match(host.querySelector('.trace-thought-content')!.textContent!,/and imports/);
    assert.equal(host.querySelector('.trace-thought-content'),thoughtBody,"live updates preserve the preview DOM");
    await act(async () => root.render(<TurnActivity key="markdown-thought" steps={[{...thought,id:"markdown-thought",status:"done",detail:"**Inspecting files**\n\nChecked imports."}]} live={true} streamText="" turnStartedAt={Date.now()}/>));
    assert.equal(host.querySelector('.trace-thought-content'),thoughtBody,"completion keeps the preview in place");
    await act(async () => host.querySelector<HTMLButtonElement>('.trace-entry__disclosure')!.click());
    assert.equal(host.querySelector('.trace-entry__headline')?.textContent,"Inspecting files","folded preview strips formatting markers");
    await act(async () => root.render(<TurnActivity key="child-test" steps={cells} live={false} streamText="" turnStartedAt={100} ownerLabel="Check measurements"/>));
    assert.equal(host.querySelector('.execution-trace__owner')!.textContent, 'Check measurements', 'subagent attribution remains');
    const savedThought = 'Supplied thought summary. '.repeat(40);
    assert.equal(parseTurnSteps([{...thought, detail: savedThought}])![0].detail, savedThought, 'reload preserves thought text beyond 400 characters');
    assert.equal(activityPresentation({...cells[0], argsPreview: '{"code":"print(\\"hello\\")\\nnext'}).input, 'print("hello")\nnext');
    assert.equal(activityPresentation({...cells[0], argsPreview: '{"code":"value\\u12'}).input, 'value', 'incomplete escape is not fabricated');
    assert.equal(activityPresentation({...cells[0], tool: 'search', argsPreview: '{"code":"raw'}).input, '{"code":"raw', 'only Python inputs unwrap code previews');
    const browserCell = {...cells[0], id: 'live-browser-cell', callId: 'live-browser-call', status: 'running' as const, argsPreview: JSON.stringify({code: "page = browser.navigate('https://example.com')"})};
    assert.equal(traceActionLabel(browserCell, true), 'Opening browser page');
    assert.equal(traceActionLabel({...browserCell, status: 'ok'}, false), 'Opened browser page');
    assert.equal(traceActionLabel({...browserCell, status: 'error'}, false), 'Browser navigation failed');
    assert.equal(traceActionLabel({...browserCell, argsPreview: JSON.stringify({code: 'print("browser.navigate()")'})}, true), 'Running Python');
    await act(async () => root.render(<TurnActivity key="live-cell-test" steps={[browserCell]} live={true} streamText="" turnStartedAt={Date.now()}/>));
    assert.match(host.querySelector('.trace-entry__name')!.textContent!, /Opening browser page/);
    assert.ok(host.querySelector('.trace-entry.is-running .trace-progress'), 'only running row shows the progress animation');
    assert.equal(host.querySelector('.trace-entry__body'), null, 'code starts collapsed');
    await act(async () => host.querySelector<HTMLButtonElement>('.trace-entry__disclosure')!.click());
    assert.match(host.querySelector('.trace-entry__body code')!.textContent!, /browser.navigate/);
    await act(async () => setChatState({...getChatState(), pause: {state: 'paused', admissionId: 'test', runId: 'test', revision: 1, synced: true}}));
    assert.equal(host.querySelector('.trace-progress'), null, 'paused run stops animated activity');
    assert.match(host.querySelector('.execution-trace__summary')!.textContent!, /Paused/);
    await act(async () => setChatState({...getChatState(), pause: null, connected: false}));
    assert.equal(host.querySelector('.trace-progress'), null, 'disconnected run does not pretend to be live');
    assert.match(host.querySelector('.execution-trace__summary')!.textContent!, /Reconnecting/);
    await act(async () => setChatState({...getChatState(), connected: true}));
    await act(async () => root.render(<TurnActivity key="live-cell-test" steps={[{...browserCell, status: 'ok'}]} live={true} streamText="" turnStartedAt={Date.now()}/>));
    assert.equal(host.querySelector('.trace-entry .trace-progress'), null, 'completed row stops animating');
    assert.ok(host.querySelector('.execution-trace__waiting'), 'between calls remains visibly working');
    await act(async () => root.render(<TurnActivity key="live-cell-test" steps={[{...browserCell, status: 'ok'}]} live={false} streamText="" turnStartedAt={Date.now()}/>));
    assert.equal(host.querySelector('.trace-progress'), null, 'settled run has no stale animation');

    await act(async () => root.render(<><AppearanceBindings/><AppearanceSettings/></>));
    await act(async () => [...host.querySelectorAll('button')].find(button => button.textContent === 'Compact')!.click());
    assert.equal(getAppearance().density, "compact"); assert.equal(document.documentElement.dataset.density, "compact");
    await act(async () => setAppearance({density: "comfortable", motion: "reduced"}));
    assert.equal(document.documentElement.dataset.motion, "reduced");
    assert.equal(JSON.parse(window.localStorage.getItem("variant1.appearance.v1")!).motion, "reduced");

    __resetWorkbenchForTests(); __resetSessionStoreForTests(); __resetTurnStoreForTests(); hidePane(PANE.files);
    const commands: Array<Record<string, unknown>> = [];
    const context = {send: (command: Record<string, unknown>) => { commands.push(command); return true; }, notify() {}, isOpen: () => true};
    setSessionContext(context); setChatContext(context);
    setChatState({...initialChatState(), sessionId: "design-chat", connected: true}); noteDisplayedSession("design-chat");
    ingestSessions({type: "chat:sessions", items: [{id: "notebook", title: "Notebook calculations"}], active_id: "design-chat"});
    await act(async () => { root.render(<ActionPalette/>); openPalette(); });
    await change(host.querySelector<HTMLInputElement>('[role="combobox"]')!, "Focus Files");
    assert.equal(host.querySelectorAll('[role="option"]').length, 1);
    await act(async () => { key(host.querySelector('[role="combobox"]')!, "Enter"); await pause(20); });
    assert.equal(getWorkbenchState().hidden["owned:files:design-chat"], false);
    await act(async () => openPalette());
    await change(host.querySelector<HTMLInputElement>('[role="combobox"]')!, "Notebook");
    await act(async () => { key(host.querySelector('[role="combobox"]')!, "Enter"); await pause(20); });
    assert.ok(commands.some(command => command.type === "chat:session:switch" && command.id === "notebook"));
    assert.equal(filterPaletteActions([], "anything").length, 0);
    await act(async () => { openPalette("windows"); });
    assert.match(host.textContent!, /No detached windows/);
    await act(async () => closePalette());

    __resetSessionStoreForTests(); __resetTurnStoreForTests(); commands.length = 0;
    setChatState({...initialChatState(), sessionId: "design-chat", connected: true, draft: "Do this next", turnActive: true});
    pushTurnStep({kind: 'thinking', label: 'Thinking', key: 'thinking', detail: 'First ', appendDetail: true, status: 'running'});
    pushTurnStep({kind: 'thinking', label: 'Thinking', key: 'thinking', detail: 'summary.', appendDetail: true, status: 'running'});
    settleRunningThinking();
    pushTurnStep({kind: 'tool', label: 'Python', key: 'cell-between', status: 'done'});
    pushTurnStep({kind: 'thinking', label: 'Thinking', key: 'thinking', detail: 'Next summary.', appendDetail: true, status: 'running'});
    assert.deepEqual(getChatState().turnSteps.map(step => step.kind), ['thinking', 'tool', 'thinking'], 'thoughts stay beside their own cells');
    assert.equal(getChatState().turnSteps[0].detail, 'First summary.');
    pushTurnStep({id: 'summary-test', kind: 'thinking', label: 'Thought', key: 'summary-test', detail: 'Public summary.', status: 'done'});
    pushTurnStep({id: 'summary-test', kind: 'thinking', label: 'Thought', key: 'summary-test', detail: 'Public summary.', status: 'done'});
    assert.equal(getChatState().turnSteps.filter(step => step.id === 'summary-test').length, 1, 'completed summary snapshots are idempotent');
    setChatContext(context); turnController.begin({clientId: getChatState().clientId, sessionId: "design-chat", source: "chat"});
    await act(async () => root.render(<ChatComposer/>));
    await act(async () => [...host.querySelectorAll('button')].find(button => button.textContent === 'Queue next message')!.click());
    await act(async () => key(host.querySelector('#composer-input')!, "Enter"));
    assert.ok(commands.some(command => command.type === "chat" && command.delivery === "follow_up" && command.text === "Do this next"));
    assert.match(host.querySelector('.composer-input-queue')!.textContent!, /Do this next/);

    const points = kernelSamples(kernelSeed("chat:3"), 0);
    assert.deepEqual(points, kernelSamples(kernelSeed("chat:3"), 0));
    assert.notDeepEqual(points, kernelSamples(kernelSeed("chat:4"), 0));
    assert.ok(points.every(point => Number.isFinite(point.x) && Math.abs(point.x) < 1 && Math.abs(point.y) < 1));
    assert.ok(points.filter(point => point.accent).length / points.length < .03);
    assert.equal(MUTATION_INK, "#39ff14");
    const placement = {id: "pane:files", title: "Files", left: -1200, top: 40, width: 400, height: 600, pinned: true};
    assert.deepEqual(parseNativePlacements([placement, placement, {...placement, id: "utility:settings"}, {...placement, width: NaN}]), [placement]);
    await act(async()=>{__resetSessionStoreForTests();ingestSessions({type:"chat:sessions",items:[{id:"menu-session",title:"Menu boundary test"}],active_id:"menu-session"});});
    const bounds=HTMLElement.prototype.getBoundingClientRect;
    const originalHeight=window.innerHeight,originalWidth=window.innerWidth;
    try {
      Object.defineProperty(window,"innerWidth",{configurable:true,value:1024});Object.defineProperty(window,"innerHeight",{configurable:true,value:768});
      HTMLElement.prototype.getBoundingClientRect=function(){
        if(this.id==="runtime-session-menu")return {left:0,top:0,right:180,bottom:304,width:180,height:304,x:0,y:0,toJSON(){}};
        if(this.classList.contains("history-item__menu"))return {left:980,top:720,right:1010,bottom:744,width:30,height:24,x:980,y:720,toJSON(){}};
        return bounds.call(this);
      };
      await act(async()=>root.render(<HistoryRail/>));
      const trigger=host.querySelector<HTMLButtonElement>('[aria-label="Actions for Menu boundary test"]')!;
      await act(async()=>trigger.click());
      const menu=document.querySelector<HTMLElement>('#runtime-session-menu')!;
      assert.equal(menu.style.top,"456px","menu clamps using its full measured 304px height, not the old 132px guess");
      assert.equal(menu.style.left,"830px");
      Object.defineProperty(window,"innerHeight",{configurable:true,value:400});Object.defineProperty(window,"innerWidth",{configurable:true,value:300});
      await act(async()=>window.dispatchEvent(new Event("resize")));
      assert.equal(menu.style.top,"88px");assert.equal(menu.style.left,"112px","open menu follows viewport resize");
      await act(async()=>{key(menu,"Escape");await pause(10);});
      assert.equal(document.querySelector('#runtime-session-menu'),null);assert.equal(document.activeElement,trigger,"Escape restores the session trigger");
    } finally {
      HTMLElement.prototype.getBoundingClientRect=bounds;
      Object.defineProperty(window,"innerHeight",{configurable:true,value:originalHeight});Object.defineProperty(window,"innerWidth",{configurable:true,value:originalWidth});
    }
    console.log("design behavior: ordered/folded traces, failure disclosure, density/motion, palette navigation, queued delivery, seeded geometry, and placement validation passed");
  } finally {
    await act(async () => { root.unmount(); closePalette(); __resetTurnStoreForTests(); setAppearance({density: "comfortable", motion: "system"}); });
    host.remove();
  }
}
