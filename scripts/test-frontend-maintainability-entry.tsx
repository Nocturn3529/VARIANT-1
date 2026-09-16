import assert from "node:assert/strict";
import {act} from "react";
import {createRoot} from "react-dom/client";
import {AutomationsDestination} from "../frontend/main-deck/src/AutomationsDestination";
import * as automation from "../frontend/main-deck/src/automationStore";
import {setMemoryConnection, setMemoryContext} from "../frontend/main-deck/src/memoryStore";
import {VoiceSettings} from "../frontend/main-deck/src/VoiceSettings";
import {setGeneralContext, ingestGeneral} from "../frontend/main-deck/src/generalStore";
import {CustomEndpointsPanel,LocalRuntimePanel} from "../frontend/main-deck/src/ProviderCenter";
import {setContext, ingest} from "../frontend/main-deck/src/store";
import {PreviewPane} from "../frontend/main-deck/src/workbench/PreviewPane";
import {openBrowser, getPreviewState, closePreview} from "../frontend/main-deck/src/workbench/previewStore";
import {SurfaceDocumentContext} from "../frontend/main-deck/src/ui/SurfaceDocument";
import {watchPath} from "../frontend/main-deck/src/workbench/watchPath";
import {getToastState, notifyToast} from "../frontend/main-deck/src/state/toastStore";
import type {RuntimeApi} from "../frontend/main-deck/src/types";

const sent: Array<Record<string, unknown>> = [];
const notices: string[] = [];
const roots: ReturnType<typeof createRoot>[] = [];
function mount(host: HTMLElement) { const root = createRoot(host); roots.push(root); return root; }
const context = {send: (command: Record<string, unknown>) => { sent.push(command); return true; },
  notify: (message: string, surface?: string) => { notices.push(message); notifyToast(message, surface); }, isOpen: () => true};
const pause = (ms = 0) => new Promise(resolve => setTimeout(resolve, ms));
async function change(input: HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement, value: string) {
  await act(async () => {
    const prototype = input.tagName === "SELECT" ? HTMLSelectElement.prototype : input.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event(input.tagName === "SELECT" ? "change" : "input", {bubbles: true}));
  });
}
function field(host: Element, label: string): HTMLInputElement {
  const node = [...host.querySelectorAll("label")].find(row => row.querySelector("span")?.textContent === label)?.querySelector("input,textarea,select");
  assert.ok(node, `missing field ${label}`); return node as HTMLInputElement;
}
function button(host: Element, text: string): HTMLButtonElement {
  const node = [...host.querySelectorAll("button")].find(row => row.textContent === text);
  assert.ok(node, `missing button ${text}`); return node;
}

async function testEditorOwnership() {
  // The initiating form lives in another document; the main document has no toast host.
  const child = document.implementation.createHTMLDocument("Detached Automations");
  const host = child.createElement("div"); child.body.appendChild(host);
  const root = mount(host);
  automation.setAutomationContext(context);
  await act(async () => { automation.openAutomationBuilder(); root.render(<AutomationsDestination/>); });
  await change(field(host, "Name"), "A"); await change(field(host, "Instruction"), "First instruction");
  sent.length = 0;
  await act(async () => { button(host, "Create automation").click(); });
  const first = sent.at(-1)!;
  assert.equal(button(host, "Saving…").disabled, true);
  await act(async () => { host.querySelector("form")!.dispatchEvent(new Event("submit", {bubbles: true, cancelable: true})); });
  assert.equal(sent.length, 1, "double submission cannot create two requests");
  await act(async () => { automation.closeAutomationBuilder(); automation.openAutomationBuilder(); });
  await change(field(host, "Name"), "B"); await change(field(host, "Instruction"), "Keep this draft");
  await act(async () => { automation.ingestAutomations({type: "automation:accepted", request_id: first.request_id}); });
  assert.equal(field(host, "Name").value, "B", "A's delayed receipt cannot close B");
  await act(async () => { button(host, "Create automation").click(); });
  const second = sent.at(-1)!;
  await act(async () => { automation.ingestAutomations({type: "automation:error", request_id: first.request_id, error: "Old failure"}); });
  assert.ok(automation.getAutomationState().pendingSave, "a stale error cannot release B's pending save");
  await act(async () => { automation.ingestAutomations({type: "automation:error", request_id: second.request_id, error: "Schedule rejected"}); });
  assert.match(host.querySelector('[role="alert"]')!.textContent!, /Schedule rejected/);
  assert.equal(field(host, "Instruction").value, "Keep this draft");
  assert.equal(document.querySelector('[role="alert"]'), null);
  assert.equal(getToastState().surface, "utility:automations");
  await act(async () => root.unmount());
  automation.closeAutomationBuilder();
  console.log("M08/M46: delayed saves, duplicate submit, and persistent feedback in a detached form passed");
}

async function testSpeechAndEndpoints() {
  const host = document.createElement("div"); document.body.appendChild(host);
  const root = mount(host);
  setGeneralContext(context); setContext(context);
  const providers = ["openai", "elevenlabs"].map(id => ({id, name: id, kind: "cloud", auth: "api_key", available: true}));
  await act(async () => { ingestGeneral({type: "config", voice: {stt: {provider: "openai", providers}, tts: {provider: "openai", providers, voice: "saved-voice"}}}); root.render(<VoiceSettings/>); });
  let card = host.querySelector('.voice-provider-card')!;
  await change(card.querySelector('input[type="password"]')!, "dummy-key-A");
  await act(async () => button(card.querySelector('.voice-provider-credential')!, "Save").click());
  const keyRequest = sent.at(-1)!;
  await change(card.querySelector(".voice-provider-select select")!, "elevenlabs");
  card = host.querySelector('.voice-provider-card')!;
  assert.equal(card.querySelector<HTMLInputElement>('input[type="password"]')!.value, "");
  await change(card.querySelector('input[type="password"]')!, "dummy-key-B");
  const noticesBefore = notices.length;
  await act(async () => ingestGeneral({type: "speech:accepted", request_id: keyRequest.request_id}));
  assert.equal(card.querySelector<HTMLInputElement>('input[type="password"]')!.value, "dummy-key-B");
  assert.equal(notices.length, noticesBefore, "A's receipt cannot be announced as B's success");
  const voice = host.querySelector<HTMLInputElement>('input[list="active-tts-voices"]')!;
  await act(async () => voice.focus()); await change(voice, "cancelled-voice");
  sent.length = 0;
  await act(async () => voice.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape", bubbles: true})));
  assert.equal(voice.value, "saved-voice"); assert.equal(sent.length, 0);
  await act(async () => voice.focus()); await change(voice, "entered-voice");
  await act(async () => voice.dispatchEvent(new KeyboardEvent("keydown", {key: "Enter", bubbles: true})));
  assert.equal(sent.length, 1); assert.equal(sent[0].value, "entered-voice");
  await act(async () => voice.focus()); await change(voice, "blurred-voice"); await act(async () => voice.blur());
  assert.equal(sent.length, 2); assert.equal(sent[1].value, "blurred-voice");

  await act(async () => { ingest({type: "config", custom_endpoints: []}); root.render(<CustomEndpointsPanel/>); });
  const id = field(host, "Provider ID");
  for (const value of ["a", "ax", "axet", "axet-proxy", "revised-proxy"]) {
    await change(id, value); assert.equal(id.disabled, false); assert.match(host.textContent!, /Add Endpoint/);
  }
  await change(field(host, "Name"), "Dummy endpoint"); await change(field(host, "Endpoint URL"), "http://127.0.0.1:9999/v1"); await change(field(host, "Default Model"), "dummy-model");
  await act(async () => button(host, "Save").click());
  const save = sent.at(-1)!;
  await act(async () => ingest({type: "cloud:custom-endpoint:saved", request_id: save.request_id,
    endpoint: {id: "revised-proxy", name: "Dummy endpoint", base_url: "http://127.0.0.1:9999/v1", model: "dummy-model", discover_models: true, is_current: true, models: ["dummy-model"]}}));
  assert.equal(field(host, "Provider ID").disabled, true); assert.match(host.textContent!, /Edit Endpoint/);
  await act(async () => root.unmount()); host.remove();
  console.log("M26/M27/M28: provider identity, endpoint typing, and voice Escape/Enter/blur passed");
}

async function testTransportAndWatchers() {
  automation.setAutomationContext(context); setMemoryContext(context);
  for (const connect of [automation.setAutomationConnection, setMemoryConnection]) {
    connect("offline"); sent.length = 0; connect("connected");
    const count = sent.length; assert.ok(count > 0);
    await pause(220); sent.length = 0;
    connect("offline"); connect("connected"); connect("connected");
    assert.equal(sent.length, count, "a short disconnect refreshes exactly once while the badge remains online");
  }
  let resolve!: (value: {ok: boolean; id: string}) => void;
  let listener!: (event: {id: string}) => void;
  const stopped: string[] = []; let callbacks = 0; let subscriptions = 0;
  const api = {watchWorkbenchPath: () => new Promise(r => { resolve = r; }),
    onWorkbenchPathChanged: callback => { listener = callback; subscriptions++; return () => { subscriptions--; }; },
    stopWorkbenchWatch: async id => { stopped.push(id); return {ok: true}; }} as RuntimeApi;
  const dispose = watchPath(api, "virtual", () => callbacks++, {delay: 1});
  dispose(); dispose(); resolve({ok: true, id: "late"}); await pause();
  listener({id: "late"}); await pause(5);
  assert.deepEqual(stopped, ["late"]); assert.equal(callbacks, 0); assert.equal(subscriptions, 0);
  const active = watchPath(api, "virtual", () => callbacks++, {delay: 1});
  resolve({ok: true, id: "active"}); await pause();
  listener({id: "another"}); await pause(5); assert.equal(callbacks, 0);
  listener({id: "active"}); listener({id: "active"}); await pause(5); assert.equal(callbacks, 1);
  listener({id: "active"}); active(); await pause(5); assert.equal(callbacks, 1);
  assert.deepEqual(stopped, ["late", "active"]);
  console.log("M09/M20: fast reconnect hydration and deferred watcher disposal passed");
}

async function testBrowserNavigation() {
  const host = document.createElement("div"); document.body.appendChild(host);
  const child = document.implementation.createHTMLDocument("native browser host");
  const guests: Array<HTMLElement & {url: string; loads: string[]; loadURL: (url: string) => Promise<void>}> = [];
  for (const owner of [document, child]) {
    const create = owner.createElement.bind(owner);
    owner.createElement = ((tag: string, options?: ElementCreationOptions) => {
      const node = create(tag, options);
      if (tag === "webview") {
        const guest = node as typeof guests[number];
        guest.url = ""; guest.loads = [];
        Object.assign(guest, {getURL: () => guest.url || guest.getAttribute("src"), getTitle: () => "Page", canGoBack: () => false, canGoForward: () => false,
          loadURL: async (url: string) => { guest.loads.push(url); guest.url = url; guest.dispatchEvent(new Event("did-navigate")); }});
        guests.push(guest);
      }
      return node;
    }) as typeof owner.createElement;
  }
  const root = mount(host);
  const id = openBrowser("https://first.test/", {newTab: true});
  await act(async () => root.render(<SurfaceDocumentContext.Provider value={document}><PreviewPane tabId={id} api={null}/></SurfaceDocumentContext.Provider>));
  const guest = guests.at(-1)!; assert.equal(guest.getAttribute("src"), "https://first.test/");
  let attached = false;
  Object.assign(guest, {getWebContentsId: () => { if (!attached) throw new Error("Guest is not attached"); return 1; }});
  await act(async () => { assert.equal(openBrowser("https://second.test/"), id); });
  assert.equal(guest.loads.length, 0, "a request waits for a guest that is still attaching");
  await act(async () => { attached = true; guest.dispatchEvent(new Event("did-attach")); guest.dispatchEvent(new Event("dom-ready")); });
  assert.deepEqual(guest.loads, ["https://second.test/"]); assert.equal(guests.length, 1);
  await act(async () => { guest.url = "https://observed.test/"; guest.dispatchEvent(new Event("did-navigate")); });
  assert.equal(getPreviewState().pages[id].url, "https://observed.test/"); assert.equal(guest.loads.length, 1);
  await act(async () => {
    document.dispatchEvent(new CustomEvent("variant1:surface-will-move", {detail: {mount: host}}));
    child.body.appendChild(host);
    root.render(<SurfaceDocumentContext.Provider value={child}><PreviewPane tabId={id} api={null}/></SurfaceDocumentContext.Provider>);
  });
  assert.equal(guests.at(-1)!.getAttribute("src"), "https://observed.test/", "transfer uses the observed page, not an old navigation request");
  await act(async () => root.unmount()); closePreview(id); host.remove();
  console.log("M19: same-tab intent navigates the existing guest; observed navigation and popout transfer remain independent");
}

export async function run() {
  try {
    await testEditorOwnership();
    await testSpeechAndEndpoints();
    await testTransportAndWatchers();
    await testBrowserNavigation();
    const host=document.createElement('div');document.body.appendChild(host);const root=mount(host);
    const custom={installed:true,managed_installed:false,install_source:'custom',custom_active:true,configured_binary:'C:/custom/llama-server.exe',running_binary:'C:/custom/llama-server.exe',active_binary:'C:/custom/llama-server.exe'};
    await act(async()=>{ingest({type:'inference:platform',targets:[],install_jobs:[],local_runtime:custom});root.render(<LocalRuntimePanel/>);});
    assert.equal(host.querySelector('.platform-badge')?.textContent,'Custom');assert.ok(!host.textContent?.includes('Packaged llama.cpp'));
    assert.ok([...host.querySelectorAll('em')].some(node=>node.textContent==='Running'));
    await act(async()=>ingest({type:'inference:platform',targets:[],install_jobs:[],local_runtime:{...custom,managed_installed:true,pending_restart:true,configured_binary:'C:/managed/llama-server.exe'}}));
    assert.equal(host.querySelector('.platform-badge')?.textContent,'Restart pending');
    assert.match(host.textContent!,/Next binary/);assert.ok(host.textContent?.includes('C:/custom/llama-server.exe') && host.textContent.includes('C:/managed/llama-server.exe'));
    await act(async()=>ingest({type:'inference:platform',targets:[],install_jobs:[],local_runtime:{...custom,running_binary:''}}));
    assert.ok([...host.querySelectorAll('em')].some(node=>node.textContent==='Selected'));
    assert.ok(![...host.querySelectorAll('em')].some(node=>node.textContent==='Running'),'configured executable alone is not labeled running');
    await act(async()=>root.unmount());host.remove();
    console.log("frontend maintainability UI/lifecycle acceptance: all tests passed");
  } finally { await act(async () => roots.forEach(root => root.unmount())); }
}
