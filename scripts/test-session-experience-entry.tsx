import assert from "node:assert/strict";
import {act} from "react";
import {createRoot} from "react-dom/client";
import {ChatSetupGuide} from "../frontend/main-deck/src/chat/ChatSetupGuide";
import {KernelInventory} from "../frontend/main-deck/src/KernelInventory";
import {ChatComposer} from "../frontend/main-deck/src/chat/ChatComposer";
import {initialChatState, setChatState, getChatState, setChatContext} from "../frontend/main-deck/src/chat/stateCore";
import {ingest as ingestPlatform} from "../frontend/main-deck/src/store";
import {getAppState} from "../frontend/main-deck/src/state/appStore";
import {noteDisplayedSession} from "../frontend/main-deck/src/state/sessionStore";
import {applyRuntimeSnapshot} from "../frontend/main-deck/src/chat/session";
import {setKernelInventoryContext, setKernelInventoryConnection, ingestKernelInventory,
  getKernelInventory, releaseKernel} from "../frontend/main-deck/src/kernelInventoryStore";

export async function run() {
  const host = document.createElement("div"); document.body.append(host);
  const root = createRoot(host);
  const commands: Record<string, unknown>[] = [];
  const context = {send: (command: Record<string, unknown>) => {commands.push(command); return true;}, notify() {}};
  setChatContext(context);
  await act(async () => {
    setChatState({...initialChatState(), sessionId: "A", connected: true}); noteDisplayedSession("A");
    ingestPlatform({type: "hello", model_ready: false});
    root.render(<ChatSetupGuide/>);
  });
  const button = (text: string) => [...host.querySelectorAll<HTMLButtonElement>("button")].find(item => item.textContent === text)!;
  assert.ok(button("Use an example").disabled, "an unready model cannot be advertised as ready");
  await act(async () => button("Connect model").click());
  assert.equal(getAppState().settingsCategory, "providers");
  await act(async () => ingestPlatform({type: "engine", model_ready: true}));
  assert.ok(!button("Use an example").disabled);
  await act(async () => button("Use an example").click());
  assert.match(getChatState().draft, /average/);
  assert.ok(!commands.some(command => command.type === "chat"), "examples fill drafts without sending inference");
  await act(async () => {
    applyRuntimeSnapshot("A", {action_surface: "trusted-local.v1", mutation_enabled: false,
      mutation_toggle_available: true, kernel: {state: "ready", generation: 1}});
    root.render(<ChatComposer/>);
  });
  const tools = host.querySelector<HTMLDetailsElement>(".composer-session-tools")!;
  assert.ok(tools && !tools.open, "mutation authoring is in collapsed advanced session tools");
  await act(async () => {tools.open = true;});
  assert.match(tools.textContent || "", /already activated tools/);
  assert.ok(tools.querySelector("[role=switch]"), "advanced authoring remains accessible");

  setKernelInventoryContext(context);
  await act(async () => {setKernelInventoryConnection("connected"); root.render(<KernelInventory/>);});
  const requestId = getKernelInventory().requestId;
  const row = {chat_id: "A", title: "Sample chat", generation: 1, state: "ready", busy: false,
    age_s: 120, idle_s: 30, resources: {process: {}, tree: {}, measurement_source: "unavailable"}};
  await act(async () => ingestKernelInventory({type: "kernel:inventory:result", request_id: "stale", items: [row]}));
  assert.equal(getKernelInventory().items.length, 0);
  await act(async () => ingestKernelInventory({type: "kernel:inventory:result", request_id: requestId, items: [row]}));
  assert.match(host.textContent || "", /Unknown/);
  assert.equal(releaseKernel("A", 2), false, "a confirmation for another generation cannot dispatch");
  await act(async () => button("Release session").click());
  assert.match(host.textContent || "", /background Python work/);
  await act(async () => button("Close kernel").click());
  const release = commands.find(command => command.type === "kernel:release")!;
  assert.equal(release.generation, 1);
  assert.equal(getKernelInventory().items.length, 1, "dispatch does not pretend release completed");
  await act(async () => ingestKernelInventory({type: "kernel:release:result", request_id: release.request_id,
    chat_id: "A", ok: false, error: "Stop the active run before closing this kernel."}));
  const refreshId = getKernelInventory().requestId;
  await act(async () => ingestKernelInventory({type: "kernel:inventory:result", request_id: refreshId, items: [{...row, generation: 2, busy: true}]}));
  assert.match(host.textContent || "", /Stop the active run/, "authoritative refresh must preserve a failed close explanation");
  assert.ok(button("Release session").disabled, "active runs cannot be released");
  await act(async () => {
    setKernelInventoryConnection("offline");
    ingestKernelInventory({type: "kernel:inventory:result", request_id: refreshId, items: []});
  });
  assert.equal(getKernelInventory().items.length, 1, "late replies cannot overwrite offline observations");
  await act(async () => root.unmount()); host.remove();
  console.log("Session experience: authoritative readiness, no auto-send, advanced mutation, correlated inventory and generation-fenced release passed");
}
