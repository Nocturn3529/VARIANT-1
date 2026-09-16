import assert from "node:assert/strict";
import {act} from "react";
import {createRoot} from "react-dom/client";
import {BrowserSettings} from "../frontend/main-deck/src/BrowserSettings";
import {BrowserReadinessActions} from "../frontend/main-deck/src/chat/ChatBrowserStatus";
import * as browser from "../frontend/main-deck/src/browserSettingsStore";
import {ingestChat, __resetChatStoreForTests} from "../frontend/main-deck/src/chatStore";

const sent: Array<Record<string, unknown>> = [];
const profiles = Array.from({length: 8}, (_, i) => ({id: `opaque-profile-${i}`, label: `Profile ${i}`, directory_name: `Directory ${i}`}));
const catalog = {default: {selection: {mode: "embedded"}, revision: 4}, browsers: [{id: "chrome", label: "Google Chrome", profiles}]};
const last = (type: string) => [...sent].reverse().find(row => row.type === type)!;
const receipt = (command: Record<string, unknown>, extra: Record<string, unknown>) => browser.ingestBrowserSettings({request_id: command.request_id, ...extra});
const snapshot = (chatId: string, extra: Record<string, unknown> = {}) => browser.ingestBrowserSettings({type: "browser:state", chat_id: chatId, state: "idle", message: "Not started", selection: {mode: "embedded"}, selection_source: "default", revision: 1, actions: [], ...extra});
async function change(select: HTMLSelectElement, value: string) {
  await act(async () => { Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!.call(select, value); select.dispatchEvent(new Event("change", {bubbles: true})); });
}

export async function run() {
  browser.__resetBrowserSettingsForTests();
  browser.setBrowserSettingsContext({send: value => {sent.push(value); return true;}, notify() {}});
  browser.setBrowserSettingsConnection("connected");
  assert.equal(sent.length, 0, "connecting does not discover or launch a personal browser");
  browser.refreshBrowserSettings(); browser.refreshBrowserSettings();
  assert.equal(sent.length, 1, "discovery is coalesced");
  receipt(last("browser:settings:get"), {type: "browser:settings", error: {code: "unavailable", message: "Profile discovery is unavailable"}});
  assert.equal(browser.getBrowserSettings().errors.catalog, "Profile discovery is unavailable", "read errors retain actionable backend explanations");
  browser.refreshBrowserSettings();
  receipt(last("browser:settings:get"), {type: "browser:settings", ...catalog});
  snapshot("chat-a"); snapshot("chat-b");
  assert.equal(browser.saveBrowserSelection({mode: "personal", browser_id: "chrome"}, 4), false, "a profile must be selected explicitly");
  assert.equal(browser.saveBrowserSelection({mode: "personal", browser_id: "chrome", profile_id: "missing"}, 4), false);

  const host = document.createElement("div"); document.body.appendChild(host); const root = createRoot(host);
  await act(async () => root.render(<BrowserSettings/>));
  await act(async () => receipt(last("browser:settings:get"), {type: "browser:settings", ...catalog}));
  await act(async () => host.querySelector<HTMLInputElement>('input[value="personal"]')!.click());
  let selects = host.querySelectorAll<HTMLSelectElement>("select");
  assert.equal(selects[0].value, ""); assert.equal(selects[1].value, "", "discovery never chooses the last-used profile");
  await change(selects[0], "chrome");
  selects = host.querySelectorAll<HTMLSelectElement>("select");
  assert.equal(selects[1].options.length, 9, "all eight profiles and the explicit-choice placeholder are shown");
  assert.equal(selects[1].value, "");
  await change(selects[1], "opaque-profile-7");
  const submit = () => host.querySelector("form")!.dispatchEvent(new Event("submit", {bubbles: true, cancelable: true}));
  const count = sent.length;
  await act(async () => {submit(); submit();});
  assert.equal(sent.length, count + 1);
  const saving = last("browser:selection:set");
  assert.deepEqual(saving.selection, {mode: "personal", browser_id: "chrome", profile_id: "opaque-profile-7"});
  assert.equal(saving.expected_revision, 4);
  await act(async () => receipt(saving, {type: "browser:selection:result", ok: true, scope: "chat", chat_id: "chat-a", selection: saving.selection, revision: 5}));
  assert.ok(browser.getBrowserSettings().pending["selection:default"], "a differently scoped reply cannot acknowledge this edit");
  await act(async () => receipt(saving, {type: "browser:selection:result", ok: false, scope: "default", error: {code: "PROFILE_LOCKED", message: "Profile is locked"}}));
  assert.equal(host.querySelectorAll<HTMLSelectElement>("select")[1].value, "opaque-profile-7", "a failed save keeps the draft");
  await act(async () => submit());
  const retry = last("browser:selection:set");
  await act(async () => receipt(saving, {type: "browser:selection:result", ok: true, scope: "default", selection: saving.selection, revision: 5}));
  assert.equal(browser.getBrowserSettings().pending["selection:default"].id, retry.request_id, "stale acknowledgements do not unlock a newer save");
  await act(async () => receipt(retry, {type: "browser:selection:result", ok: true, scope: "default", selection: retry.selection, revision: 5}));
  assert.equal(browser.getBrowserSettings().chats["chat-a"].selection.mode, "embedded", "a new default cannot rebind an active chat");
  assert.ok(host.textContent?.includes("Browser choice saved"));

  await act(async () => {
    browser.refreshBrowserSettings();
    receipt(last("browser:settings:get"), {type: "browser:settings", ...catalog, default: {selection: retry.selection, revision: 5}, browsers: [{...catalog.browsers[0], profiles: profiles.slice(0, 7)}]});
  });
  assert.ok(host.textContent?.includes("saved browser or profile is unavailable"));
  assert.equal(host.querySelector<HTMLButtonElement>('button[type="submit"]')!.disabled, true, "a missing saved identity is never substituted silently");
  await act(async () => ingestChat({type: "chat:session", session: {id: "chat-a", messages: []}}));
  const chatForm = host.querySelector<HTMLFormElement>('form[aria-label="Browser choice for this chat"]')!;
  assert.ok(chatForm);
  await act(async () => chatForm.querySelector<HTMLInputElement>('input[value="managed"]')!.click());
  await act(async () => snapshot("chat-a", {state: "connecting", revision: 2}));
  assert.equal(chatForm.querySelector<HTMLButtonElement>('button[type="submit"]')!.disabled, true, "a connecting chat cannot start another profile change");
  await act(async () => snapshot("chat-a", {state: "profile_locked", revision: 3}));
  assert.equal(chatForm.querySelector<HTMLInputElement>('input[value="managed"]')!.checked, true, "readiness changes keep the edited choice");
  await act(async () => chatForm.dispatchEvent(new Event("submit", {bubbles: true, cancelable: true})));
  const chatSave = last("browser:selection:set");
  assert.equal(chatSave.expected_revision, 3, "readiness-only revisions can advance without discarding the draft");
  await act(async () => receipt(chatSave, {type: "browser:selection:result", ok: true, scope: "chat", chat_id: "chat-a", selection: {mode: "managed"}, revision: 4}));
  await act(async () => chatForm.querySelector<HTMLInputElement>('input[value="embedded"]')!.click());
  await act(async () => snapshot("chat-a", {selection: {mode: "personal", browser_id: "chrome", profile_id: "opaque-profile-1"}, revision: 5}));
  assert.equal(chatForm.querySelector<HTMLButtonElement>('button[type="submit"]')!.disabled, true, "a changed saved choice requires review before overwrite");
  await act(async () => [...chatForm.querySelectorAll("button")].find(button => button.textContent === "Keep my choice")!.click());
  assert.equal(chatForm.querySelector<HTMLButtonElement>('button[type="submit"]')!.disabled, false);
  await act(async () => root.unmount()); host.remove();
  __resetChatStoreForTests();

  snapshot("chat-a", {state: "profile_locked", revision: 6, pending_operation_id: "operation-a", actions: ["retry", "cancel"], message: "Close the selected source browser and retry."});
  snapshot("chat-b", {state: "selection_required", pending_operation_id: "operation-b", actions: ["select_profile", "cancel"]});
  assert.equal(browser.resolveBrowserOperation("chat-b", "retry"), false, "recovery is limited to advertised actions");
  assert.equal(browser.resolveBrowserOperation("chat-a", "retry"), true);
  const resolving = last("browser:resolve");
  assert.equal(resolving.operation_id, "operation-a"); assert.equal(resolving.chat_id, "chat-a");
  receipt(resolving, {type: "browser:resolve:result", ok: true, chat_id: "chat-b", operation_id: "operation-b"});
  assert.ok(browser.getBrowserSettings().pending["resolve:chat-a"], "other chats cannot acknowledge recovery");
  receipt(resolving, {type: "browser:resolve:result", ok: true, chat_id: "chat-a", operation_id: "operation-a"});
  assert.equal(last("browser:state:get").chat_id, "chat-a");
  assert.equal(sent.some(row => row.type === "browser:host:command" || row.action === "navigate"), false, "recovery never replays browser navigation in the frontend");
  browser.setBrowserSettingsConnection("offline");
  assert.equal(Object.keys(browser.getBrowserSettings().pending).length, 0);
  const beforeConnect = sent.length; browser.setBrowserSettingsConnection("connected");
  assert.equal(sent.length, beforeConnect, "reconnection cannot resend a pending mutation");
  snapshot("chat-a", {state: "ready", revision: 8}); snapshot("chat-a", {state: "profile_locked", revision: 7});
  assert.equal(browser.getBrowserSettings().chats["chat-a"].state, "ready", "stale state cannot restore an old wait");
  const actionsHost = document.createElement("div"); document.body.appendChild(actionsHost); const actionsRoot = createRoot(actionsHost);
  await act(async () => actionsRoot.render(<BrowserReadinessActions chatId="chat-b"/>));
  assert.ok(actionsHost.textContent?.includes("Browser settings"));
  assert.ok(actionsHost.textContent?.includes("Cancel browser request"));
  assert.ok(!actionsHost.textContent?.includes("Retry connection"));
  await act(async () => actionsRoot.unmount()); actionsHost.remove();
  browser.__resetBrowserSettingsForTests();
  browser.setBrowserSettingsContext({send: value => {sent.push(value); return true;}, notify() {}});
  browser.setBrowserSettingsConnection("connected"); browser.refreshBrowserSettings();
  const extended = {...catalog, default: {selection: {mode: "embedded"}, revision: 10}, catalog: {
    modes: ["embedded", "managed", "personal", "cdp", "cloud"], defaults: {headed: true, evaluate_enabled: true, command_timeout_s: 10}, fields: [
      {key: "headed", type: "boolean", modes: ["managed", "personal"]},
      {key: "evaluate_enabled", type: "boolean", modes: ["embedded", "managed", "personal", "cdp", "cloud"]},
      {key: "command_timeout_s", type: "number", min: 1, max: 300, modes: ["managed", "personal", "cdp", "cloud"]},
      {key: "cloud_provider", type: "select", options: ["browserbase", "firecrawl"], modes: ["cloud"]},
      {key: "project_id", type: "string", modes: ["cloud"]},
      {key: "cdp_url", type: "string", modes: ["cdp"]},
      {key: "record_sessions", type: "boolean", modes: ["managed", "personal"]},
    ]}};
  receipt(last("browser:settings:get"), {type: "browser:settings", ...extended});
  const optionsHost = document.createElement("div"); document.body.appendChild(optionsHost); const optionsRoot = createRoot(optionsHost);
  await act(async () => optionsRoot.render(<BrowserSettings/>));
  const optionInput = (title: string) => [...optionsHost.querySelectorAll(".browser-options-fields label")].find(label => label.textContent?.includes(title))?.querySelector<HTMLInputElement>("input");
  assert.ok(optionInput("Allow page JavaScript"));
  assert.equal(optionInput("Command timeout"), undefined, "embedded mode does not expose inert external-browser timeout controls");
  await act(async () => optionsHost.querySelector<HTMLInputElement>('input[value="cloud"]')!.click());
  await change(optionsHost.querySelector<HTMLSelectElement>(".browser-options-fields select")!, "browserbase");
  assert.equal(optionsHost.querySelector<HTMLButtonElement>('button[type="submit"]')!.disabled, true, "Browserbase needs an explicit project");
  await act(async () => {const input = optionInput("Browserbase project ID")!; Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value")!.set!.call(input,"project-fixture"); input.dispatchEvent(new Event("input",{bubbles:true}));});
  await act(async () => optionsHost.querySelector("form")!.dispatchEvent(new Event("submit",{bubbles:true,cancelable:true})));
  const cloudSave = last("browser:selection:set");
  assert.deepEqual(cloudSave.selection, {mode: "cloud", cloud_provider: "browserbase", project_id: "project-fixture"});
  await act(async () => receipt(cloudSave,{type:"browser:selection:result",ok:true,scope:"default",selection:cloudSave.selection,revision:11}));
  assert.equal(browser.getBrowserSettings().catalog?.default.selection.project_id,"project-fixture","cloud selection fields survive acknowledgement");
  await act(async () => optionsHost.querySelector<HTMLInputElement>('input[value="managed"]')!.click());
  assert.ok(optionInput("Show the browser window"));
  assert.ok(optionInput("Command timeout"));
  assert.equal(optionInput("Browserbase project ID"),undefined,"switching modes removes provider-specific fields");
  await act(async () => {const input=optionInput("Command timeout")!; Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value")!.set!.call(input,"301");input.dispatchEvent(new Event("input",{bubbles:true}));});
  assert.equal(optionsHost.querySelector<HTMLButtonElement>('button[type="submit"]')!.disabled,true,"out-of-range timeouts cannot be submitted");
  await act(async () => browser.refreshBrowserRecordings("chat-a")); const recordingsRequest=last("browser:recordings:get");
  await act(async () => receipt(recordingsRequest,{type:"browser:recordings",chat_id:"chat-b",items:[]}));
  assert.equal(browser.getBrowserSettings().recordings["chat-b"],undefined,"another chat cannot acknowledge recording results");
  await act(async () => receipt(recordingsRequest,{type:"browser:recordings",chat_id:"chat-a",items:[{name:"run.webm",path:"C:/fixture/run.webm",complete:true,bytes:1000}]}));
  assert.equal(browser.getBrowserSettings().recordings["chat-a"].length,1);
  assert.notEqual(browser.browserSelectionKey({mode:"managed",evaluate_enabled:true}),browser.browserSelectionKey({mode:"managed",evaluate_enabled:false}),"advanced changes participate in conflict detection");
  await act(async()=>optionsRoot.unmount());optionsHost.remove();browser.__resetBrowserSettingsForTests();
  console.log("Browser settings: eight explicit profiles, missing identity, scoped revisions/acks, retryable drafts, pending-operation isolation, no navigation replay, catalog-gated options, cloud fields, timeout bounds and owned recordings passed");
}
