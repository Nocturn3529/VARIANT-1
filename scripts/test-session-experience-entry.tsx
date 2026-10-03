import assert from "node:assert/strict";
import {act} from "react";
import {createRoot} from "react-dom/client";
import {PythonKernelsTab} from "../frontend/main-deck/src/overview/PythonKernelsTab";
import {ChatComposer} from "../frontend/main-deck/src/chat/ChatComposer";
import {initialChatState, setChatState, setChatContext} from "../frontend/main-deck/src/chat/stateCore";
import {ingest as ingestPlatform} from "../frontend/main-deck/src/store";
import {noteDisplayedSession} from "../frontend/main-deck/src/state/sessionStore";
import {applyRuntimeSnapshot} from "../frontend/main-deck/src/chat/session";
import {getContextForSession, ingestSessionContext, requestSessionContext, setSessionContextConnection} from "../frontend/main-deck/src/sessionContextStore";
import {setKernelInventoryContext, setKernelInventoryConnection, ingestKernelInventory,
  getKernelInventory, releaseKernel} from "../frontend/main-deck/src/kernelInventoryStore";

export async function run() {
  const host = document.createElement("div"); document.body.append(host);
  const root = createRoot(host);
  const commands: Record<string, unknown>[] = [];
  const context = {send: (command: Record<string, unknown>) => {commands.push(command); return true;}, notify() {}};
  setChatContext(context);
  // Model readiness is scoped to this chat's route; the empty chat no longer renders a setup guide.
  await act(async () => {
    setChatState({...initialChatState(), sessionId: "A", connected: true}); noteDisplayedSession("A");
    ingestPlatform({type: "hello", model_ready: false});
  });
  const button = (text: string) => [...host.querySelectorAll<HTMLButtonElement>("button")].find(item => item.textContent === text)!;
  const configured = () => getContextForSession("A").modelConfigured;
  assert.notEqual(configured(), true, "an unready model cannot be advertised as ready");
  await act(async () => ingestPlatform({type: "engine", model_ready: true}));
  assert.notEqual(configured(), true, "global readiness must not advertise another chat's route");
  await act(async () => {
    requestSessionContext("A");
    setSessionContextConnection("connected");
    ingestSessionContext({type: "chat:context", session_id: "A", route: "cloud", provider: "xai", model: "grok-4.7", model_configured: true});
  });
  assert.equal(configured(), true);
  await act(async () => ingestSessionContext({type: "chat:context", session_id: "A", route: "cloud", provider: "xai", model: "grok-4.7"}));
  assert.equal(configured(), true, "usage-only projections preserve matching configuration observations");
  await act(async () => ingestSessionContext({type: "chat:context", session_id: "A", route: "cloud", provider: "openai", model: "gpt-4o"}));
  assert.notEqual(configured(), true, "configuration observations cannot transfer to another model route");
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
  await act(async () => {setKernelInventoryConnection("connected"); root.render(<PythonKernelsTab/>);});
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
