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
  getKernelInventory, refreshKernelInventory, releaseKernel} from "../frontend/main-deck/src/kernelInventoryStore";

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
  const memoryMetric = () => {
    const metric = [...host.querySelectorAll(".python-kernels__strip .deck-metric")].find(item => item.querySelector(".deck-metric__label")?.textContent === "Memory")!;
    return [metric.querySelector(".deck-metric__value")!.textContent, metric.querySelector(".deck-metric__detail")!.textContent];
  };
  assert.equal(memoryMetric()[0], "Unknown", "unmeasured kernels do not total zero bytes");
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

  // A history read lost to a disconnect must not refuse later reads, and an
  // open row reads its cells again once the backend is back.
  const historyReads = () => commands.filter(command => command.type === "kernel:history");
  await act(async () => setKernelInventoryConnection("connected"));
  await act(async () => host.querySelector<HTMLButtonElement>(".python-kernel__toggle")!.click());
  assert.equal(historyReads().length, 1, "opening a row reads its cells");
  const lost = historyReads()[0];
  await act(async () => setKernelInventoryConnection("offline"));
  assert.equal(getKernelInventory().history.A?.requestId, "", "a disconnect releases the pending history read");
  await act(async () => setKernelInventoryConnection("connected"));
  assert.equal(historyReads().length, 2, "an open row reads its cells again after a reconnect");
  const reread = historyReads()[1];
  assert.notEqual(reread.request_id, lost.request_id);
  await act(async () => ingestKernelInventory({type: "kernel:history", request_id: lost.request_id, chat_id: "A",
    items: [{sequence: 1, execution_id: "lost", status: "completed", label: "lost_cell()"}]}));
  assert.doesNotMatch(host.textContent || "", /lost_cell/, "a reply to the lost read is ignored");
  await act(async () => ingestKernelInventory({type: "kernel:history", request_id: reread.request_id, chat_id: "A",
    items: [{sequence: 2, execution_id: "x2", status: "completed", duration_ms: 40, label: "df.describe()"}]}));
  assert.match(host.textContent || "", /df\.describe\(\)/);

  // A cell that finishes while a history read is in flight gets exactly one
  // catch-up read once that reply lands.
  const observe = async (items: Record<string, unknown>[]) => {
    if (!getKernelInventory().requestId) await act(async () => {refreshKernelInventory();});
    const id = getKernelInventory().requestId;
    await act(async () => ingestKernelInventory({type: "kernel:inventory:result", request_id: id, items}));
  };
  const finished = (executionId: string) => ({...row, generation: 2, last_cell: {execution_id: executionId, status: "completed", duration_ms: 12}});
  await observe([finished("c3")]);
  assert.equal(historyReads().length, 3, "a finished cell rereads the open row");
  const inFlight = historyReads()[2];
  await observe([finished("c4")]);
  assert.equal(historyReads().length, 3, "a read in flight is not duplicated");
  await act(async () => ingestKernelInventory({type: "kernel:history", request_id: inFlight.request_id, chat_id: "A",
    items: [{sequence: 3, execution_id: "c3", status: "completed", label: "third_cell()"}]}));
  assert.equal(historyReads().length, 4, "the reply catches up on the cell that finished meanwhile");
  await act(async () => ingestKernelInventory({type: "kernel:history", request_id: historyReads()[3].request_id, chat_id: "A",
    items: [{sequence: 3, execution_id: "c3", status: "completed", label: "third_cell()"}, {sequence: 4, execution_id: "c4", status: "completed", label: "fourth_cell()"}]}));
  assert.equal(historyReads().length, 4, "exactly one catch-up read");
  assert.match(host.textContent || "", /fourth_cell\(\)/);

  // Memory sums measured kernels and marks the total partial when some are not.
  await observe([finished("c4"), {...row, chat_id: "B", title: "Measured chat", resources: {process: {rss_bytes: 128 * 1024 * 1024},
    tree: {rss_bytes: 256 * 1024 * 1024, processes: 2, complete: true}, measurement_source: "psutil"}}]);
  assert.deepEqual(memoryMetric(), ["≥ 256 MiB", "1 unmeasured"]);
  await act(async () => root.unmount()); host.remove();
  console.log("Session experience: authoritative readiness, no auto-send, advanced mutation, correlated inventory, generation-fenced release, kernel history across reconnects and catch-up, and partial memory totals passed");
}
