import {setChatProjectContext,setChatProject,ingestChatProjects,getChatProjects} from "../frontend/main-deck/src/state/chatProjectStore";
import {traceSummary} from "../frontend/main-deck/src/chat/traceModel";
import assert from "node:assert/strict";
import {
  BackendClient,
  type BackendClientScheduler,
  type BackendSocket,
  type SocketEventMap,
} from "../frontend/main-deck/src/runtime/BackendClient";
import {DeckRuntime} from "../frontend/main-deck/src/runtime/DeckRuntime";
import {INITIAL_DECK_COMMANDS} from "../frontend/main-deck/src/runtime/initialHydration";
import {
  __resetTurnStoreForTests,
  turnController,
} from "../frontend/main-deck/src/state/turnStore";
import {
  __resetSessionStoreForTests,
  getSessionState,
  ingestSessions,
  noteDisplayedSession,
  requestNewSession,
  setSessionConnection,
  setSessionContext,
  setSessionSearchQuery,
  switchSession,
} from "../frontend/main-deck/src/state/sessionStore";
import {
  MicController,
  encodeWav,
  type AudioContextLike,
} from "../frontend/main-deck/src/runtime/MicController";
import {
  parseTurnSteps,
  reconcileAuthoritativeTurn,
} from "../frontend/main-deck/src/chat/messages";
import type {
  ChatMessage,
  ChatRuntimeState,
} from "../frontend/main-deck/src/chat/types";
import {
  sendUserMessage,
  submitUserInput,
  setMutationWriteEnabled,
} from "../frontend/main-deck/src/chat/composer";
import {
  addChatFiles,
  invalidatePendingChatAttachments,
} from "../frontend/main-deck/src/chat/attachments";
import {
  getAutomationState,
  openAutomationBuilder,
  saveAutomation,
  setAutomationContext,
} from "../frontend/main-deck/src/automationStore";
import {
  addCoreFact,
  createLoop,
  setDraftFact,
  setDraftLoopGoal,
  setDraftLoopTitle,
  setMemoryContext,
} from "../frontend/main-deck/src/memoryStore";
import {
  disposeTerminalRuntime,
  getTerminalSnapshot,
  ingestTerminal,
  interruptTerminal,
  killTerminal,
  openNewTerminal,
  setTerminalConnection,
  setTerminalContext,
  writeTerminalInputFor,
  selectTerminal,
} from "../frontend/main-deck/src/context/terminalStore";
import {mutationToggleControlState} from "../frontend/main-deck/src/chat/mutationControl";
import {
  REACT_MODULE_MESSAGE_TYPES,
  parseChatWsMessage,
} from "../frontend/main-deck/src/protocol";
import {getGeneralState, ingestGeneral} from "../frontend/main-deck/src/generalStore";
import {isActiveModel} from "../frontend/main-deck/src/generalStore";
import {
  beginTurnReceipt,
  noteReceiptTool,
  readableToolName,
  resetTurnReceipt,
  shortModelName,
  snapshotTurnReceipt,
} from "../frontend/main-deck/src/chat/receipt";
import {
  extractEvidence,
} from "../frontend/main-deck/src/chat/evidence";
import {ingestChat} from "../frontend/main-deck/src/chat/ingest";
import {finishStream, ingestActivityMessage} from "../frontend/main-deck/src/chat/turn";
import {
  formatActivityDuration,
  normalizeActivityStatus,
} from "../frontend/main-deck/src/chat/activityModel";
import {
  activeConversationIndex,
  conversationTimelineEntry,
} from "../frontend/main-deck/src/chat/ConversationTimeline";
import {applySession} from "../frontend/main-deck/src/chat/session";
import {
  getChatState,
  getComposerRevision,
  initialChatState,
  resetChatStateBag,
  setChatContext,
  setChatState,
} from "../frontend/main-deck/src/chat/stateCore";
import {
  __resetSessionContextStoreForTests,
  getSessionContextState,
  ingestSessionContext,
  requestSessionContext,
} from "../frontend/main-deck/src/sessionContextStore";
import {pushWireStatus, resetWireStatus} from "../frontend/main-deck/src/connectionUi";
import {
  closeSettings,
  getAppState,
  navigateTo,
  selectSettingsCategory,
} from "../frontend/main-deck/src/state/appStore";
import {
  __resetWorkbenchForTests,
  closePane,
  getWorkbenchState,
  PANE,
  revealPane,
  togglePane,
} from "../frontend/main-deck/src/workbench/workbenchStore";
import {allPaneIds, findGroupOfPane} from "../frontend/main-deck/src/workbench/layoutModel";
type Scheduled = {
  id: number;
  callback: () => void;
  delay: number;
  cancelled: boolean;
};

class FakeScheduler implements BackendClientScheduler {
  private nextId = 1;
  readonly tasks: Scheduled[] = [];

  setTimeout(callback: () => void, delay: number): number {
    const id = this.nextId++;
    this.tasks.push({id, callback, delay, cancelled: false});
    return id;
  }

  clearTimeout(handle: unknown): void {
    const task = this.tasks.find(row => row.id === handle);
    if (task) task.cancelled = true;
  }

  runNext(): boolean {
    const candidates = this.tasks
      .filter(row => !row.cancelled)
      .sort((a, b) => a.delay - b.delay || a.id - b.id);
    const task = candidates[0];
    if (!task) return false;
    task.cancelled = true;
    task.callback();
    return true;
  }

  runAll(): void {
    let guard = 0;
    while (this.runNext()) {
      guard += 1;
      assert.ok(guard < 100, "scheduler entered an unexpected timer loop");
    }
  }
}

class FakeSocket implements BackendSocket {
  readyState = 0;
  readonly sent: string[] = [];
  private readonly listeners: {
    [K in keyof SocketEventMap]?: Array<(event: SocketEventMap[K]) => void>;
  } = {};

  constructor(readonly url: string) {}

  send(data: string): void {
    if (this.readyState !== 1) throw new Error("socket is not open");
    this.sent.push(data);
  }

  close(): void {
    if (this.readyState === 3) return;
    this.readyState = 3;
    this.emit("close", {code: 1000, wasClean: true} as CloseEvent);
  }

  addEventListener<K extends keyof SocketEventMap>(
    type: K,
    listener: (event: SocketEventMap[K]) => void,
  ): void {
    const list = (this.listeners[type] ??= []) as Array<
      (event: SocketEventMap[K]) => void
    >;
    list.push(listener);
  }

  open(): void {
    this.readyState = 1;
    this.emit("open", {} as Event);
  }

  message(value: unknown): void {
    const data = typeof value === "string" ? value : JSON.stringify(value);
    this.emit("message", {data} as MessageEvent);
  }

  disconnect(code = 1006): void {
    if (this.readyState === 3) return;
    this.readyState = 3;
    this.emit("close", {code, wasClean: false} as CloseEvent);
  }

  private emit<K extends keyof SocketEventMap>(
    type: K,
    event: SocketEventMap[K],
  ): void {
    const listeners = this.listeners[type] as
      | Array<(item: SocketEventMap[K]) => void>
      | undefined;
    listeners?.forEach(listener => listener(event));
  }
}

function flush(): Promise<void> {
  return new Promise(resolve => setImmediate(resolve));
}

async function testBackendClient(): Promise<void> {
  const scheduler = new FakeScheduler();
  const sockets: FakeSocket[] = [];
  const logs: string[] = [];
  let backendInfoCalls = 0;
  let backendStatus: ((status: {status?: string}) => void) | null = null;
  const client = new BackendClient({
    api: {
      async getBackendInfo() {
        backendInfoCalls += 1;
        return {port: 8123, token: "secret token"};
      },
      onBackendStatus(listener) {
        backendStatus = listener;
      },
      log(message) {
        logs.push(message);
      },
    },
    createSocket(url) {
      const socket = new FakeSocket(url);
      sockets.push(socket);
      return socket;
    },
    scheduler,
    connectTimeoutMs: 100,
    reconnectDelay: () => 1,
  });

  const states: string[] = [];
  const received: string[] = [];
  let openCount = 0;
  let closeCount = 0;
  client.subscribeState(state => states.push(state));
  client.subscribeMessage(() => {
    throw new Error("listener isolation probe");
  });
  client.subscribeMessage(message => received.push(String(message.type)));
  client.subscribeOpen(() => { openCount += 1; });
  client.subscribeClose(() => { closeCount += 1; });

  client.start();
  client.start();
  await flush();
  assert.equal(backendInfoCalls, 1, "repeated start must not reconnect");
  assert.equal(sockets.length, 1, "one start creates one socket");
  assert.match(sockets[0].url, /8123\/ws\?token=secret%20token$/);
  assert.equal(client.getDiagnostics().maxConcurrentSocketCount, 1);

  sockets[0].open();
  assert.equal(client.isOpen(), true);
  assert.equal(openCount, 1);
  assert.equal(states.at(-1), "connected");

  sockets[0].message("{bad json");
  sockets[0].message({type: "hello"});
  assert.deepEqual(received, ["hello"], "malformed JSON is ignored");
  assert.ok(
    logs.some(line => line.includes("listener isolation probe")),
    "one bad subscriber must be logged and isolated",
  );

  assert.equal(client.send({type: "config:get"}), true);
  assert.deepEqual(JSON.parse(sockets[0].sent[0]), {type: "config:get"});

  sockets[0].disconnect();
  assert.equal(closeCount, 1);
  assert.equal(states.at(-1), "offline");
  assert.equal(client.getDiagnostics().liveSocketCount, 0);

  assert.equal(scheduler.runNext(), true, "disconnect schedules reconnect");
  await flush();
  assert.equal(sockets.length, 2);
  sockets[1].open();
  assert.equal(openCount, 2);
  assert.equal(client.getDiagnostics().maxConcurrentSocketCount, 1);

  client.stop();
  scheduler.runAll();
  await flush();
  assert.equal(sockets.length, 2, "stop suppresses pending reconnects");
  assert.equal(client.getDiagnostics().started, false);

  backendStatus?.({status: "ready"});
  await flush();
  assert.equal(sockets.length, 2, "backend-ready cannot restart a stopped client");
}

async function testDeckRuntimeHydration(): Promise<void> {
  const scheduler = new FakeScheduler();
  const sockets: FakeSocket[] = [];
  const handled: string[] = [];
  const lifecycle: string[] = [];
  const runtime = new DeckRuntime({
    api: {
      async getBackendInfo() {
        return {port: 9000, token: "token"};
      },
    },
    clientOptions: {
      createSocket(url) {
        const socket = new FakeSocket(url);
        sockets.push(socket);
        return socket;
      },
      scheduler,
      connectTimeoutMs: 100,
      reconnectDelay: () => 1,
    },
  });
  runtime.register({
    id: "test-filtered-module",
    messageTypes: ["accepted"],
    start() { lifecycle.push("start"); },
    enter(_context, view) { lifecycle.push(`enter:${view}`); },
    settingsCategory(_context, category) { lifecycle.push(`settings:${category}`); },
    connection(_context, state) { lifecycle.push(`connection:${state}`); },
    handle(_context, message) { handled.push(String(message.type)); },
  });
  runtime.register({
    id: "test-throwing-module",
    messageTypes: ["accepted"],
    handle() { throw new Error("expected isolated test failure"); },
  });
  runtime.register({
    id: "test-throwing-parser",
    messageTypes: ["accepted"],
    parse() { throw new Error("expected isolated parser failure"); },
    handle() { throw new Error("a failed parser must never reach its handler"); },
  });
  runtime.register({
    id: "test-after-throw-module",
    messageTypes: ["accepted"],
    handle() { handled.push("after-throw"); },
  });

  runtime.start();
  runtime.start();
  assert.equal(lifecycle.filter(item => item === "start").length, 1);
  await flush();
  assert.equal(sockets.length, 1);
  sockets[0].open();
  sockets[0].message({type: "ignored"});
  const priorConsoleError = console.error;
  const moduleErrors: string[] = [];
  console.error = label => { moduleErrors.push(String(label)); };
  try {
    sockets[0].message({type: "accepted"});
  } finally {
    console.error = priorConsoleError;
  }
  assert.ok(moduleErrors.some(label => label.includes("test-throwing-parser parse failed")));
  assert.ok(moduleErrors.some(label => label.includes("test-throwing-module handle failed")));
  assert.deepEqual(handled, ["accepted", "after-throw"],
    "typed module router filters inbound messages and isolates peer failures");
  runtime.setView("settings");
  assert.equal(lifecycle.filter(item => item === "settings:general").length, 1,
    "entering Settings must hydrate the active category exactly once");
  runtime.setSettingsCategory("skills");
  assert.ok(lifecycle.includes("enter:settings"));
  assert.equal(lifecycle.filter(item => item === "settings:skills").length, 1,
    "switching Settings category must hydrate only the selected category");
  assert.deepEqual(
    sockets[0].sent.map(item => JSON.parse(item).type),
    [...INITIAL_DECK_COMMANDS],
    "initial hydration is sent once and in order",
  );

  sockets[0].disconnect();
  scheduler.runNext();
  await flush();
  assert.equal(sockets.length, 2);
  sockets[1].open();
  assert.equal(lifecycle.filter(item => item === "settings:skills").length, 2,
    "a reconnect must rehydrate the active Settings category exactly once");
  assert.deepEqual(
    sockets[1].sent.map(item => JSON.parse(item).type),
    [...INITIAL_DECK_COMMANDS],
    "a new connection generation receives one fresh hydration burst",
  );
  runtime.stop();
}

function testTurnStore(): void {
  __resetTurnStoreForTests();
  const events: Array<{active: boolean; previous: boolean; status: string}> = [];
  const unsubscribe = turnController.subscribe((snapshot, previous) => {
    events.push({
      active: snapshot.active,
      previous: previous.active,
      status: snapshot.lastEndStatus,
    });
  });

  assert.equal(turnController.begin({
    clientId: "chat-1",
    source: "chat",
    sessionId: "session-a",
  }), true);
  assert.equal(turnController.begin({clientId: "duplicate"}), false);
  assert.equal(turnController.getSessionId(), "session-a");
  assert.equal(turnController.matchesEvent({
    type: "token",
    client_id: "chat-1",
    session_id: "session-a",
  }), true);
  assert.equal(turnController.matchesEvent({
    type: "activity",
    source: "chat",
    session_id: "session-b",
  }), false, "another chat's activity must not enter this transcript");
  assert.equal(turnController.matchesEvent({
    type: "token",
    client_id: "voice-1",
  }), false);
  assert.equal(turnController.matchesEvent({
    type: "token",
    source: "voice",
    client_id: "chat-1",
  }), false);
  assert.equal(turnController.matchesEvent({type: "token"}), true);

  assert.equal(turnController.end({status: "complete"}), true);
  assert.equal(turnController.isActive(), false);
  assert.equal(
    turnController.matchesEvent({type: "token"}),
    false,
    "idle chat cannot adopt an untagged stream",
  );

  turnController.end({status: "cancelled", force: true});
  assert.equal(turnController.getClientId(), "");
  assert.equal(turnController.getSessionId(), "");
  assert.ok(events.some(event => event.active && !event.previous));
  assert.ok(events.some(event => !event.active && event.status === "cancelled"));
  unsubscribe();
}

function testSessionStore(): void {
  __resetTurnStoreForTests();
  __resetSessionStoreForTests();
  const sent: Array<Record<string, unknown>> = [];
  const notices: string[] = [];
  let canSend = true;
  setSessionContext({
    send(command) {
      if (!canSend) return false;
      sent.push(command);
      return true;
    },
    notify(message) {
      notices.push(message);
    },
    isOpen: () => true,
  });
  setSessionConnection("connected");
  ingestSessions({
    type: "chat:sessions",
    active_id: "session-a",
    items: [
      {
        id: "session-a",
        title: "A",
        message_count: 2,
        updated_at: 1,
      },
      {id: "session-b", title: "B", message_count: 1, updated_at: 2},
    ],
  });
  assert.deepEqual(sent.at(-1), {
    type: "chat:session:get",
    id: "session-a",
  });
  noteDisplayedSession("session-a");
  setChatState({
    ...initialChatState(),
    connected: true,
    sessionId: "session-a",
    title: "A",
  });
  assert.equal(getSessionState().displayedSessionId, "session-a");
  assert.equal(getSessionState().items[0]?.title, "A");
  assert.equal(getSessionState().items[1]?.title, "B");

  sent.length = 0;
  turnController.begin({
    clientId: "chat-a",
    source: "chat",
    sessionId: "session-a",
  });
  assert.equal(switchSession("session-b"), true);
  assert.equal(sent.length, 1, "switching the view does not wait for another chat's active turn");
  assert.equal(getSessionState().pendingAction?.type, "switch");
  assert.ok(getSessionState().workingSessionIds.includes("session-a"));
  assert.deepEqual(sent.at(-1), {
    type: "chat:session:switch",
    id: "session-b",
    request_id: getSessionState().pendingAction?.type === "switch" ? (getSessionState().pendingAction as {requestId: string}).requestId : "",
  });
  assert.equal(
    getSessionState().pendingAction?.type,
    "switch",
    "navigation intent remains pending until its session snapshot arrives",
  );
  const chatSends: Array<Record<string, unknown>> = [];
  setChatContext({
    send(command) {
      chatSends.push(command);
      return true;
    },
    notify() {},
    isOpen: () => true,
  });
  assert.equal(sendUserMessage("must not land in the wrong chat"), false);
  assert.equal(
    chatSends.length,
    0,
    "composer is gated until the requested session snapshot is applied",
  );
  applySession({id: "session-b", title: "B", messages: []}, {request_id: String(sent.at(-1)?.request_id), requested_id: "session-b", effective_id: "session-b", status: "switched"});
  assert.equal(getChatState().sessionId, "session-b");
  assert.ok(getSessionState().workingSessionIds.includes("session-a"), "A continues after B is displayed");
  assert.equal(getSessionState().pendingAction, null);
  applySession({id: "session-a", title: "late old snapshot", messages: []});
  assert.equal(
    getChatState().sessionId,
    "session-b",
    "a late runtime snapshot for the previous session cannot repaint the Deck",
  );

  sent.length = 0;
  turnController.begin({
    clientId: "chat-a2",
    source: "chat",
    sessionId: "session-b",
  });
  canSend = false;
  assert.equal(requestNewSession(), false);
  assert.equal(sent.length, 0, "an unsent new-chat request waits for reconnect, not for another chat's run");
  setSessionConnection("offline");
  turnController.end({status: "complete"});
  assert.equal(sent.length, 0, "a disconnected flush cannot pretend navigation was sent");
  assert.equal(
    getSessionState().pendingAction?.type,
    "new",
    "disconnect keeps the queued new-chat action retryable",
  );
  canSend = true;
  setSessionConnection("connected");
  assert.equal(sent.at(-1)?.type, "chat:session:new");
  applySession({id: "session-c", title: "C", messages: []}, {request_id:String(sent.at(-1)?.request_id),requested_id:"",effective_id:"session-c",status:"created"});
  assert.equal(getChatState().sessionId, "session-c");
  assert.equal(getSessionState().pendingAction, null);

  sent.length = 0;
  assert.equal(requestNewSession(), true);
  assert.equal(sent.at(-1)?.type, "chat:session:new");
  const newRequest = sent.at(-1);
  setSessionConnection("offline");
  setSessionConnection("connected");
  ingestSessions({
    type: "chat:sessions",
    active_id: "session-b",
    items: [
      {id: "session-c", title: "C"},
      {id: "session-d", title: "D"},
    ],
  });
  assert.deepEqual(
    sent.at(-1),
    newRequest,
    "reconnect replays the same idempotent request instead of adopting the shared default",
  );
  applySession({id: "session-d", title: "D", messages: []}, {request_id:String(newRequest?.request_id),requested_id:"",effective_id:"session-d",status:"created"});
  assert.equal(getChatState().sessionId, "session-d");
  assert.equal(getSessionState().pendingAction, null);

  setSessionSearchQuery("first");
  ingestSessions({
    type: "chat:search:results",
    query: "first",
    items: [{id: "session-a", title: "A"}],
  });
  assert.equal(getSessionState().searchResults?.length, 1);
  setSessionSearchQuery("second");
  assert.equal(
    getSessionState().searchResults,
    null,
    "changing the query clears stale hits while the replacement is pending",
  );

  __resetSessionStoreForTests();
  setSessionConnection("offline");
  assert.match(
    getSessionState().error,
    /offline/i,
    "cold-start disconnect must not masquerade as an empty history",
  );
  __resetTurnStoreForTests();resetChatStateBag();
}

function testAuthoritativeTurnReconciliation(): void {
  const steps = [{
    id: "step-1",
    kind: "tool" as const,
    label: "Read file",
    status: "ok" as const,
    ts: 1,
  }];
  const local: ChatMessage[] = [
    {role: "assistant", text: "Earlier answer"},
    {role: "user", text: "start", optimisticTurnId: "old-turn", localId: "u-1"},
    {
      role: "user",
      text: "follow up",
      optimisticTurnId: "active-input-1",
      ticketId: "ticket-1",
      localId: "u-2",
    },
    // Submitted just before the prior done frame, but promoted by the backend
    // to a new turn. No durable ticket in this append means it must survive.
    {
      role: "user",
      text: "promoted turn",
      optimisticTurnId: "promoted-turn",
      ticketId: "client-ticket-2",
      localId: "u-promoted",
    },
    {
      role: "user",
      text: "cancelled queued input",
      optimisticTurnId: "cancelled-input",
      ticketId: "ticket-cancelled",
      activeInputAccepted: true,
      localId: "u-cancelled",
    },
    {role: "assistant", text: "second", optimisticTurnId: "old-turn", steps},
    // A new turn can begin in the small done -> chat:appended window. It must
    // remain untouched when the older authoritative append arrives.
    {role: "user", text: "start", optimisticTurnId: "new-turn", localId: "u-3"},
  ];
  const authoritative: ChatMessage[] = [
    {role: "user", text: "start", ts: 10},
    {role: "assistant", text: "first", ts: 11},
    {role: "user", text: "follow up", ts: 12, ticketId: "ticket-1"},
    {role: "assistant", text: "second", ts: 13},
  ];
  const reconciled = reconcileAuthoritativeTurn(local, authoritative);
  assert.equal(reconciled.optimisticTurnId, "old-turn");
  assert.deepEqual(
    reconciled.messages.map(message => `${message.role}:${message.text}`),
    [
      "assistant:Earlier answer",
      "user:start",
      "assistant:first",
      "user:follow up",
      "assistant:second",
      "user:promoted turn",
      "user:cancelled queued input",
      "user:start",
    ],
    "authoritative rows replace the optimistic turn without duplicate bubbles",
  );
  assert.equal(reconciled.messages[4].steps, steps, "local Steps survive reconciliation");
  assert.deepEqual(
    reconciled.optimisticTurnIds.sort(),
    ["active-input-1", "old-turn"],
  );
  assert.equal(reconciled.messages[5].optimisticTurnId, "promoted-turn");
  assert.equal(reconciled.messages[6].optimisticTurnId, "cancelled-input");
  assert.equal(reconciled.messages[7].optimisticTurnId, "new-turn");

  const unmatched: ChatMessage[] = [
    {role: "user", text: "not durable", optimisticTurnId: "lonely"},
  ];
  const safe = reconcileAuthoritativeTurn(unmatched, [
    {role: "user", text: "not durable"},
  ]);
  assert.equal(safe.optimisticTurnId, null);
  assert.equal(safe.messages, unmatched, "no complete match must never fall back to ids[0]");

  const previewUrl = "blob:attachment-preview";
  const attachmentLocal: ChatMessage[] = [
    {
      role: "user",
      text: "📷 Attached image",
      optimisticTurnId: "attachment-turn",
      localId: "attachment-user",
      attachments: [{
        id: "local-shot",
        name: "shot.png",
        kind: "image",
        mime: "image/png",
        size: 12,
        data: "large-base64-payload",
        previewUrl,
      }],
    },
    {
      role: "assistant",
      text: "I can see it.",
      optimisticTurnId: "attachment-turn",
    },
  ];
  const attachmentRemote: ChatMessage[] = [
    {
      role: "user",
      text: "📷 shot.png",
      attachments: [{
        id: "remote-shot",
        name: "shot.png",
        kind: "image",
        mime: "",
        size: 0,
      }],
    },
    {role: "assistant", text: "I can see it."},
  ];
  const attachmentResult = reconcileAuthoritativeTurn(
    attachmentLocal,
    attachmentRemote,
  );
  assert.equal(
    attachmentResult.optimisticTurnId,
    "attachment-turn",
    "attachment metadata reconciles older generic optimistic labels",
  );
  assert.equal(attachmentResult.messages.length, 2);
  assert.equal(attachmentResult.messages[0].attachments?.[0].previewUrl, previewUrl);
  assert.equal(
    attachmentResult.messages[0].attachments?.[0].data,
    undefined,
    "durable rows cannot retain the optimistic base64 transport payload",
  );

  const revoked: string[] = [];
  const originalRevoke = URL.revokeObjectURL;
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    value: (url: string) => { revoked.push(url); },
  });
  try {
    setChatState({
      ...initialChatState(),
      messages: attachmentResult.messages,
    });
    setChatState(initialChatState());
  } finally {
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      value: originalRevoke,
    });
  }
  assert.deepEqual(revoked, [previewUrl], "orphaned transcript previews are revoked");
}

function testActiveInputAndRejectionContract(): void {
  __resetTurnStoreForTests();
  const sent: Array<Record<string, unknown>> = [];
  const notices: string[] = [];
  setChatContext({
    send(command) {
      sent.push(command);
      return true;
    },
    notify(message) {
      notices.push(message);
    },
    isOpen: () => true,
  });
  setChatState({
    ...initialChatState(),
    connected: true,
    sessionId: "session-active",
    turnActive: true,
    streaming: true,
    activeTurnId: "initial-turn",
    messages: [{
      role: "user",
      text: "initial",
      optimisticTurnId: "initial-turn",
      localId: "initial-user",
    }],
  });
  turnController.begin({
    clientId: getChatState().clientId,
    source: "chat",
    sessionId: "session-active",
  });

  assert.equal(sendUserMessage("use the other file", "steer"), true);
  assert.equal(sendUserMessage("summarize after", "follow_up"), true);
  const activeMessages = getChatState().messages.slice(1);
  assert.equal(activeMessages.length, 2);
  assert.equal(activeMessages[0].delivery, "steer");
  assert.equal(activeMessages[1].delivery, "follow_up");
  assert.notEqual(activeMessages[0].optimisticTurnId, activeMessages[1].optimisticTurnId);
  assert.notEqual(activeMessages[0].optimisticTurnId, "initial-turn");
  assert.deepEqual(sent.slice(-2).map(item => item.delivery), ["steer", "follow_up"]);
  assert.deepEqual(
    sent.slice(-2).map(item => item.ticket_id),
    activeMessages.map(item => item.optimisticTurnId),
  );

  ingestChat({
    type: "chat:queued",
    id: "server-ticket-steer",
    delivery: "steer",
    queue_size: 1,
  });
  assert.equal(getChatState().pendingActiveInputs.length, 2, "an unrelated ticket cannot acknowledge a current input");
  const steerTicket = activeMessages[0].optimisticTurnId!;
  ingestChat({type:"chat:queued",id:steerTicket,session_id:"session-active",delivery:"steer",queue_size:1});
  assert.equal(getChatState().pendingActiveInputs.length, 1);
  assert.equal(getChatState().messages[1].ticketId, steerTicket);

  finishStream({
    type: "done",
    text: "Task stopped.",
    cancelled: true,
  });
  assert.equal(
    getChatState().pendingActiveInputs.length,
    1,
    "done must not discard an input whose queue receipt is still in flight",
  );
  assert.equal(getChatState().queuedFollowUps, 1);

  ingestChat({
    type: "chat:queue_rejected",
    error: "queue_closed",
  });
  assert.equal(getChatState().pendingActiveInputs.length, 0);
  assert.equal(getChatState().messages.some(message => message.text === "summarize after"), false);
  assert.equal(getChatState().draft, "summarize after");
  assert.equal(getChatState().turnActive, false, "queue rejection must not reopen a finished run");
  assert.ok(notices.at(-1)?.includes("queue_closed"));

  ingestChat({
    type: "chat:queue_settled",
    ids: [steerTicket],
    reason: "turn_failed_before_input_delivery",
    session_id: "session-active",
    queue_size: 0,
  });
  assert.equal(
    getChatState().messages.some(message => message.text === "use the other file"),
    false,
    "terminal settlement removes an already-accepted optimistic bubble",
  );
  assert.equal(getChatState().draft, "use the other file\n\nsummarize after");
  assert.equal(getChatState().queuedFollowUps, 0);
  assert.equal(getChatState().turnActive, false, "settlement preserves the done/error boundary");
  assert.ok(notices.at(-1)?.includes("turn_failed_before_input_delivery"));

  __resetTurnStoreForTests();
  setChatState({...initialChatState(), connected: true, sessionId: "session-rejected"});
  assert.equal(sendUserMessage("start expensive task"), true);
  assert.equal(getChatState().turnActive, true);
  ingestChat({
    type: "chat:rejected",
    error: "paused_budget_exhausted",
    text: "Budget is paused",
  });
  assert.equal(getChatState().turnActive, false);
  assert.equal(getChatState().streaming, false);
  assert.equal(getChatState().activeTurnId, null);
  assert.equal(getChatState().messages.length, 0, "rejected optimistic bubble rolls back");
  assert.equal(getChatState().draft, "start expensive task");
  assert.equal(turnController.isActive(), false, "Stop state settles after admission rejection");

  setChatState({
    ...initialChatState(),
    connected: true,
    sessionId: "session-delivery",
    queuedFollowUps: 1,
    messages: [{
      role: "user",
      text: "finish with current evidence",
      localId: "steer-user",
      ticketId: "ticket-delivered",
      delivery: "steer",
      activeInputAccepted: true,
      activeInputState: "queued",
    }],
  });
  ingestChat({
    type: "chat:queue_progress",
    id: "ticket-delivered",
    delivery: "steer",
    state: "delivered",
    session_id: "session-delivery",
    queue_size: 0,
  });
  assert.equal(getChatState().messages[0].activeInputState, "delivered");
  assert.equal(getChatState().queuedFollowUps, 0);
}

function mutationRuntime(
  overrides: Partial<ChatRuntimeState> = {},
): ChatRuntimeState {
  return {
    actionSurface: "trusted-local.v1",
    trustProfile: "trusted-local.v1",
    catalogReleaseId: "catalog-1",
    selectedCategoryId: "",
    mountRevision: 0,
    overlayRevision: 0,
    kernelState: "absent",
    kernelGeneration: 0,
    activeSlots: 0,
    probationSlots: 0,
    activeChildren: 0,
    queuedInputs: 0,
    continuationState: "ready",
    warning: "",
    mutationEnabled: false,
    mutationEffectiveEnabled: false,
    mutationAuthorityRevision: 7,
    mutationToggleAvailable: true,
    mutationToggleLocked: false,
    mutationToggleReason: "Mutation authoring is off",
    ...overrides,
  };
}

function testMutationToggleProtocolStoreAndSnapshots(): void {
  const parsedDone = parseChatWsMessage({
    type: "chat:runtime:mutation:set:done",
    id: "session-mutation",
    enabled: true,
    effective_enabled: false,
    request_id: "request-1",
    authority_revision: 8,
  });
  assert.deepEqual(parsedDone, {
    type: "chat:runtime:mutation:set:done",
    id: "session-mutation",
    enabled: true,
    effective_enabled: false,
    request_id: "request-1",
    authority_revision: 8,
  });
  const malformedBoolean = parseChatWsMessage({
    type: "chat:runtime:mutation:set:done",
    enabled: "false",
    effective_enabled: "true",
  });
  assert.equal(
    malformedBoolean.type === "chat:runtime:mutation:set:done"
      && malformedBoolean.enabled,
    false,
    "wire strings must not coerce into enabled authority",
  );
  const parsedRejected = parseChatWsMessage({
    type: "chat:runtime:mutation:set:rejected",
    id: "session-mutation",
    enabled: false,
    request_id: "request-2",
    error: "active run",
  });
  assert.equal(parsedRejected.type, "chat:runtime:mutation:set:rejected");
  assert.equal(
    parsedRejected.type === "chat:runtime:mutation:set:rejected"
      ? parsedRejected.error
      : "",
    "active run",
  );

  __resetTurnStoreForTests();
  const sent: Array<Record<string, unknown>> = [];
  const notices: string[] = [];
  setChatContext({
    send(command) {
      sent.push(command);
      return true;
    },
    notify(message) {
      notices.push(message);
    },
    isOpen: () => true,
  });
  setChatState({
    ...initialChatState(),
    connected: true,
    sessionId: "session-mutation",
    runtime: mutationRuntime(),
  });

  assert.equal(setMutationWriteEnabled(true), true);
  const pendingOn = getChatState().mutationTogglePending;
  assert.ok(pendingOn);
  assert.equal(pendingOn?.baseRevision, 7);
  assert.equal(setMutationWriteEnabled(false), false, "only one toggle may be pending");
  assert.deepEqual(sent.at(-1), {
    type: "chat:runtime:mutation:set",
    id: "session-mutation",
    enabled: true,
    request_id: pendingOn?.requestId,
    expected_revision: 7,
  });

  ingestChat({
    type: "chat:runtime:mutation:set:done",
    id: "session-mutation",
    enabled: true,
    effective_enabled: true,
    request_id: "older-other-window-request",
    authority_revision: 8,
  });
  assert.equal(getChatState().mutationTogglePending?.requestId, pendingOn?.requestId);
  assert.equal(
    getChatState().runtime?.mutationEnabled,
    false,
    "an unrelated acknowledgement cannot overwrite a newer local target",
  );

  ingestChat({
    type: "chat:runtime:mutation:set:done",
    id: "session-mutation",
    enabled: true,
    effective_enabled: false,
    request_id: pendingOn?.requestId || "",
    authority_revision: 8,
  });
  assert.equal(getChatState().mutationTogglePending, null);
  assert.equal(getChatState().runtime?.mutationEnabled, true);
  assert.equal(
    getChatState().runtime?.mutationEffectiveEnabled,
    false,
    "the effective gate must follow server qualification, not the requested switch",
  );
  assert.equal(getChatState().runtime?.mutationAuthorityRevision, 8);
  assert.ok(notices.at(-1)?.includes("on for this chat"));

  assert.equal(setMutationWriteEnabled(false), true);
  const pendingOff = getChatState().mutationTogglePending;
  ingestChat({
    type: "chat:runtime:mutation:set:rejected",
    id: "session-mutation",
    enabled: false,
    request_id: "unrelated-rejection",
    error: "not ours",
  });
  assert.equal(getChatState().mutationTogglePending?.requestId, pendingOff?.requestId);
  ingestChat({
    type: "chat:runtime:mutation:set:rejected",
    id: "session-mutation",
    enabled: false,
    request_id: pendingOff?.requestId || "",
    error: "active run",
  });
  assert.equal(getChatState().mutationTogglePending, null);
  assert.equal(getChatState().runtime?.mutationEnabled, true);
  assert.ok(notices.at(-1)?.includes("active run"));

  assert.equal(setMutationWriteEnabled(false), true);
  ingestChat({
    type: "chat:session",
    session: {
      id: "session-mutation",
      title: "Mutation",
      messages: [],
      runtime: {
        action_surface: "trusted-local.v1",
        mutation_enabled: false,
        mutation_authority_revision: 8,
        mutation_toggle_available: true,
      },
    },
  });
  assert.ok(
    getChatState().mutationTogglePending,
    "a same-revision snapshot is not proof that the pending command settled",
  );
  ingestChat({
    type: "chat:session",
    session: {
      id: "session-mutation",
      title: "Mutation",
      messages: [],
      runtime: {
        action_surface: "trusted-local.v1",
        mutation_enabled: false,
        mutation_authority_revision: 9,
        mutation_toggle_available: true,
      },
    },
  });
  assert.equal(
    getChatState().mutationTogglePending,
    null,
    "a newer authoritative snapshot settles a lost acknowledgement",
  );

  assert.equal(setMutationWriteEnabled(true), true);
  turnController.begin({
    clientId: "turn-after-toggle",
    source: "chat",
    sessionId: "session-mutation",
  });
  setChatState({...getChatState(), turnActive: true});
  ingestChat({
    type: "chat:session",
    session: {
      id: "session-mutation",
      title: "Mutation",
      messages: [],
      runtime: {
        action_surface: "trusted-local.v1",
        mutation_enabled: true,
        mutation_authority_revision: 10,
        mutation_toggle_available: false,
        mutation_toggle_reason: "Finish the active turn",
      },
    },
  });
  assert.equal(
    getChatState().mutationTogglePending,
    null,
    "same-session snapshots also settle commands after a turn starts",
  );
  turnController.end({status: "complete", force: true});
}

function testMutationToggleControlReasons(): void {
  const native = mutationToggleControlState({
    runtime: mutationRuntime({actionSurface: "native-tools.v1"}),
    pending: null,
    connected: true,
    turnActive: false,
    sessionId: "session",
  });
  assert.equal(native.visible, false);

  const unavailable = mutationToggleControlState({
    runtime: mutationRuntime({
      mutationToggleAvailable: false,
      mutationToggleReason: "Model route is not qualified for mutation",
    }),
    pending: null,
    connected: true,
    turnActive: false,
    sessionId: "session",
  });
  assert.equal(unavailable.visible, true, "mutation switch stays visible when unavailable");
  assert.equal(unavailable.disabled, true);
  assert.equal(unavailable.title, "Model route is not qualified for mutation");

  const active = mutationToggleControlState({
    runtime: mutationRuntime(),
    pending: null,
    connected: true,
    turnActive: true,
    sessionId: "session",
  });
  assert.equal(active.disabled, true);
  assert.match(active.title, /active turn/);

  const offline = mutationToggleControlState({
    runtime: mutationRuntime(),
    pending: null,
    connected: false,
    turnActive: false,
    sessionId: "session",
  });
  assert.equal(offline.disabled, true);
  assert.match(offline.title, /Reconnect/);

  const pending = mutationToggleControlState({
    runtime: mutationRuntime(),
    pending: {requestId: "pending", enabled: true, baseRevision: 7},
    connected: true,
    turnActive: false,
    sessionId: "session",
  });
  assert.equal(pending.disabled, true);
  assert.equal(pending.title, "Changing mutation authority…");
  assert.equal(pending.valueText, "Turning on…");
}

function fakeMicResources() {
  let stopped = 0;
  let closed = 0;
  let endedListener: (() => void) | null = null;
  const track = {
    stop() { stopped += 1; },
    addEventListener(type: string, listener: () => void) {
      if (type === "ended") endedListener = listener;
    },
    removeEventListener(type: string, listener: () => void) {
      if (type === "ended" && endedListener === listener) endedListener = null;
    },
  };
  const stream = {
    getTracks() {
      return [track];
    },
    getAudioTracks() { return [track]; },
  } as unknown as MediaStream;
  const source = {connect() {}, disconnect() {}};
  const gain = {gain: {value: 1}, connect() {}, disconnect() {}};
  const worklet = {
    port: {onmessage: null as ((event: MessageEvent) => void) | null},
    connect() {},
    disconnect() {},
  };
  const context: AudioContextLike = {
    sampleRate: 48_000,
    state: "running",
    destination: {},
    async resume() {},
    async close() { closed += 1; },
    createMediaStreamSource() { return source; },
    createGain() { return gain; },
  };
  return {
    stream,
    context,
    worklet,
    stopped: () => stopped,
    closed: () => closed,
    endTrack: () => endedListener?.(),
  };
}

async function testMicController(): Promise<void> {
  const resources = fakeMicResources();
  const phases: string[] = [];
  const commands: Array<Record<string, unknown>> = [];
  let mediaCalls = 0;
  const controller = new MicController({
    isOpen: () => true,
    getSessionId: () => "chat-mic",
    send(command) {
      commands.push(command);
      return true;
    },
    notify() {},
    onPhase(phase) {
      phases.push(phase);
    },
    async getUserMedia() {
      mediaCalls += 1;
      return resources.stream;
    },
    createAudioContext: () => resources.context,
    async loadWorklet() {},
    createWorkletNode: () => resources.worklet,
    encodeAudio: () => "encoded-wav",
    now: () => 100,
  });
  const start = controller.start();
  void controller.start();
  await start;
  assert.equal(mediaCalls, 1, "double start acquires one microphone stream");
  assert.equal(controller.getPhase(), "recording");
  resources.worklet.port.onmessage?.({
    data: {
      type: "frame",
      samples: new Float32Array([0.2, -0.2]),
      rms: 0.2,
      frames: 2,
    },
  } as MessageEvent);
  controller.stop(true);
  assert.equal(controller.getPhase(), "transcribing");
  assert.equal(commands.length, 1);
  assert.equal(commands[0].type, "voice:transcribe");
  assert.equal(commands[0].audio, "encoded-wav");
  assert.equal(commands[0].session_id, "chat-mic");
  assert.match(String(commands[0].request_id || ""), /^voice-\d+-1$/);
  assert.equal(resources.stopped(), 1);
  assert.equal(resources.closed(), 1);
  assert.equal(controller.completeTranscription(),false,"unscoped transcripts cannot complete a scoped recording");
  controller.completeTranscription(String(commands[0].request_id),"chat-mic");
  assert.equal(controller.getPhase(), "idle");
  assert.ok(phases.includes("requesting"));
  assert.ok(phases.includes("recording"));
  await controller.start();
  resources.worklet.port.onmessage?.({data:{type:"frame",samples:new Float32Array([0.2,0.3]),rms:0.2,frames:2}} as unknown as MessageEvent);
  controller.stop(true);
  const discarded=commands.at(-1)!;
  controller.stop(false);
  assert.equal(controller.getPhase(),"idle","discard is immediate even before the backend acknowledges cancellation");
  assert.equal(commands.at(-1)?.type,"voice:transcribe:cancel");
  assert.equal(controller.completeTranscription(String(discarded.request_id),"chat-mic"),false,"a late normal transcript after discard cannot send speech");

  const pending = fakeMicResources();
  let resolveMedia: ((stream: MediaStream) => void) | null = null;
  const pendingController = new MicController({
    isOpen: () => true,
    send: () => true,
    notify() {},
    onPhase() {},
    getUserMedia: () => new Promise(resolve => { resolveMedia = resolve; }),
    createAudioContext: () => pending.context,
    async loadWorklet() {},
    createWorkletNode: () => pending.worklet,
  });
  const pendingStart = pendingController.start();
  pendingController.stop(false);
  if (resolveMedia) resolveMedia(pending.stream);
  await pendingStart;
  assert.equal(pendingController.getPhase(), "idle");
  assert.equal(
    pending.stopped(),
    1,
    "stop during permission request releases the late media stream",
  );

  const timed = fakeMicResources();
  let maxTimer: (() => void) | null = null;
  const timedController = new MicController({
    isOpen: () => true,
    send: () => true,
    notify() {},
    onPhase() {},
    getUserMedia: async () => timed.stream,
    createAudioContext: () => timed.context,
    async loadWorklet() {},
    createWorkletNode: () => timed.worklet,
    setTimer(callback) {
      maxTimer = callback;
      return 1 as unknown as ReturnType<typeof setTimeout>;
    },
    clearTimer() {},
  });
  await timedController.start();
  assert.equal(timedController.getPhase(), "recording");
  (maxTimer as (() => void) | null)?.();
  assert.equal(timedController.getPhase(), "idle");
  assert.equal(timed.stopped(), 1, "maximum duration stops a dead stream");

  const ended = fakeMicResources();
  const endedController = new MicController({
    isOpen: () => true,
    send: () => true,
    notify() {},
    onPhase() {},
    getUserMedia: async () => ended.stream,
    createAudioContext: () => ended.context,
    async loadWorklet() {},
    createWorkletNode: () => ended.worklet,
  });
  await endedController.start();
  ended.endTrack();
  assert.equal(endedController.getPhase(), "error");

  const deniedController = new MicController({
    isOpen: () => true,
    send: () => true,
    notify() {},
    onPhase() {},
    async getUserMedia() {
      throw new DOMException("denied", "NotAllowedError");
    },
  });
  await deniedController.start();
  assert.equal(deniedController.getPhase(), "error");

  const wav = new DataView(encodeWav(
    [new Float32Array([0, 0.5, -0.5, 0])],
    16_000,
    16_000,
  ));
  assert.equal(
    String.fromCharCode(...new Uint8Array(wav.buffer.slice(0, 4))),
    "RIFF",
  );
  assert.equal(wav.getUint32(24, true), 16_000);
}

function testWorkbenchLayoutHelpers(): void {
  __resetWorkbenchForTests();
  const initial = getWorkbenchState();
  assert.ok(allPaneIds(initial.layout).includes(PANE.workspace));
  assert.ok(findGroupOfPane(initial.layout, PANE.files));
  revealPane("preview:url:test", "right");
  assert.ok(findGroupOfPane(getWorkbenchState().layout, "preview:url:test"));
  closePane("preview:url:test");
  assert.equal(allPaneIds(getWorkbenchState().layout).includes("preview:url:test"), false);

  setChatState({
    ...initialChatState(),
    turnActive: true,
    streaming: true,
  });
  ingestActivityMessage({
    type: "tool:activity",
    event: "tool:start",
    tool: "ipython",
    call_id: "call-workbench",
    status: "running",
    text: "Running Python",
    args_preview: "",
    title: "",
    surface: "side",
  });
  assert.equal(
    getChatState().turnSteps[0]?.tool,
    "ipython",
    "tool activity marked side must still appear in Chat STEPS",
  );
}

function testTurnReceiptHelpers(): void {
  __resetSessionContextStoreForTests();
  setChatState({...initialChatState(), sessionId:"receipt-session"});
  requestSessionContext("receipt-session");
  ingestSessionContext({
    type: "chat:context",
    session_id: "receipt-session",
    status: "ready",
    route: "cloud",
    provider: "xai",
    model: "grok-4.6",
    reasoning_effort: "high",
    reasoning_efforts: ["low", "medium", "high"],
    used_tokens: 500,
    cached_input_tokens: 50,
  });
  assert.equal(getSessionContextState().reasoningEffort, "high");
  assert.deepEqual(
    getSessionContextState().reasoningEfforts,
    ["low", "medium", "high"],
  );
  resetTurnReceipt();
  assert.equal(readableToolName("ipython"), "Python");
  assert.equal(readableToolName("apply_patch"), "Edit file");
  assert.equal(shortModelName("models/user/qwen3.5-4b.gguf"), "qwen3.5 4b");
  beginTurnReceipt();
  noteReceiptTool("ipython");
  noteReceiptTool("ipython");
  ingestSessionContext({
    type: "chat:context",
    session_id: "receipt-session",
    status: "ready",
    used_tokens: 600,
    cached_input_tokens: 60,
  });
  const receipt = snapshotTurnReceipt();
  assert.ok(receipt);
  assert.equal(receipt?.toolCount, 1);
  assert.equal(
    receipt?.promptTokens,
    600,
    "receipt reports the request prompt, not growth from the previous request",
  );
  assert.equal(receipt?.cachedInputTokens, 60);
  assert.equal(snapshotTurnReceipt(), undefined);
}

function testRunSettlementProtocol(): void {
  const settled = parseChatWsMessage({
    type: "run:settled",
    run_id: "run-1",
    session_id: "session-1",
    status: "truncated",
    stop_reason: "length",
    terminal_reason: "model_output_limit",
    cause_class: "model",
    length_recoveries: 2,
    settled: true,
    receipt: {version: 2, settled: true},
  });
  assert.equal(settled.type, "run:settled");
  if (settled.type !== "run:settled") return;
  assert.equal(settled.settled, true);
  assert.equal(settled.stop_reason, "length");
  assert.equal(settled.terminal_reason, "model_output_limit");
  assert.equal(settled.cause_class, "model");
  assert.equal(settled.length_recoveries, 2);
  assert.deepEqual(settled.receipt, {version: 2, settled: true});
}

function testWireStatusColdStart(): void {
  const states: boolean[] = [];
  pushWireStatus("cold-start-test", "offline", online => states.push(online));
  pushWireStatus("cold-start-test", "offline", online => states.push(online));
  assert.deepEqual(
    states,
    [false],
    "cold-start offline paints once while repeated offline probes stay quiet",
  );
  resetWireStatus("cold-start-test");
}

function testEvidenceExtraction(): void {
  const file = extractEvidence({
    tool: "read_file",
    argsPreview: JSON.stringify({
      path: "C:\\Users\\ExampleUser\\Desktop\\VARIANT-1\\frontend\\main-deck\\src\\chat\\turn.ts",
      offset: 1,
    }),
  });
  assert.equal(file[0]?.kind, "file");
  assert.equal(file[0]?.label, "turn.ts");
  assert.match(file[0]?.value || "", /turn\.ts$/);

  const truncated = extractEvidence({
    tool: "read_file",
    argsPreview: '{"path": "C:\\\\Users\\\\ExampleUser\\\\project\\\\src\\\\very\\\\long\\\\name.ts',
  });
  assert.equal(truncated[0]?.kind, "file");
  assert.match(truncated[0]?.value || "", /name\.ts$/);

  const link = extractEvidence({
    tool: "browser_navigate",
    argsPreview: '{"url": "https://docs.python.org/3/library/json.html"}',
  });
  assert.equal(link[0]?.kind, "url");
  assert.equal(link[0]?.value, "https://docs.python.org/3/library/json.html");

  const search = extractEvidence({
    tool: "web_search",
    argsPreview: '{"query": "native snapshots vs event journal"}',
  });
  assert.equal(search[0]?.kind, "search");
  assert.equal(search[0]?.value, "native snapshots vs event journal");

  const shot = extractEvidence({tool: "browser_screenshot"});
  assert.equal(shot[0]?.kind, "screen");

  const command = extractEvidence({
    tool: "run_command",
    argsPreview: '{"command": "git status"}',
  });
  assert.equal(command[0]?.kind, "command");
  assert.equal(command[0]?.label, "git");

  const desktop = extractEvidence({
    tool: "computer",
    argsPreview: "action=click path=C:\\Users\\ExampleUser\\notes.txt",
  });
  assert.equal(desktop.some(item => item.kind === "file" && item.value.endsWith("notes.txt")), true);
  assert.equal(
    extractEvidence({tool: "computer", argsPreview: "action=click target=OK"}).some(item => item.kind === "file"),
    false,
    "UI target names must not become file cards",
  );

  const patch = extractEvidence({
    tool: "apply_patch",
    text: "*** Update File: frontend/main-deck/src/chat/turn.ts",
  });
  assert.equal(patch[0]?.kind, "file");
  assert.equal(patch[0]?.label, "turn.ts");

  const markdown = extractEvidence({
    text: "See [the docs](https://example.com/guide) and C:\\Users\\ExampleUser\\Desktop\\report.md",
  });
  assert.equal(markdown.some(item => item.kind === "url" && item.value === "https://example.com/guide"), true);
  assert.equal(markdown.some(item => item.kind === "file" && item.label === "report.md"), true);

  const parsed = parseTurnSteps([{
    id: "s1",
    kind: "tool",
    label: "Read file",
    status: "ok",
    evidence: [{kind: "file", label: "turn.ts", value: "src/chat/turn.ts"}],
  }]);
  assert.equal(parsed?.[0].evidence?.[0].kind, "file");
  assert.equal(parsed?.[0].evidence?.[0].label, "turn.ts");

  setChatState({
    ...initialChatState(),
    turnActive: true,
    streaming: true,
  });
  ingestActivityMessage({
    type: "tool:activity",
    event: "tool:start",
    tool: "read_file",
    call_id: "call-evidence",
    status: "running",
    text: "",
    args_preview: '{"path": "C:\\\\Users\\\\ExampleUser\\\\Desktop\\\\VARIANT-1\\\\README.md"}',
    title: "",
    surface: "main",
  });
  const live = getChatState().turnSteps[0];
  assert.equal(live?.label, "Read file");
  assert.equal(live?.evidence?.[0].kind, "file");
  assert.equal(live?.evidence?.[0].label, "README.md");
}

function testHermesActivityProjection(): void {
  const shaped = parseChatWsMessage({
    type: "activity", event: "tool:result", tool: "read_file",
    call_id: "wire-call", status: "ok", duration_ms: 45,
  });
  assert.equal(shaped.type, "activity");
  if (shaped.type === "activity") {
    assert.equal(shaped.call_id, "wire-call");
    assert.equal(shaped.duration_ms, 45);
  }
  assert.equal(normalizeActivityStatus("invalid_arguments", "tool:result"), "error");
  assert.equal(normalizeActivityStatus("needs_reconciliation", "tool:result"), "error");
  assert.equal(normalizeActivityStatus("cancelled", "tool:result"), "error");
  assert.equal(normalizeActivityStatus("ok", "tool:result"), "ok");
  assert.equal(formatActivityDuration(1_250), "1.3s");

  setChatState({...initialChatState(), turnActive: true, streaming: true});
  ingestActivityMessage({
    type: "activity",
    event: "tool:start",
    tool: "read_file",
    status: "running",
    text: "duplicate operational start",
    args_preview: "{}",
    title: "",
    surface: "side",
  });
  assert.equal(getChatState().turnSteps.length, 0,
    "identity-free operational starts must not duplicate targeted Chat starts");

  for (const [callId, path] of [["call-a", "src/a.ts"], ["call-b", "src/b.ts"]] as const) {
    ingestActivityMessage({
      type: "tool:activity",
      event: "tool:start",
      tool: "read_file",
      call_id: callId,
      status: "running",
      text: "",
      args_preview: JSON.stringify({path}),
      title: "",
      surface: "main",
      ts: 1_700_000_000,
    });
  }
  assert.equal(getChatState().turnSteps.length, 2,
    "same-name calls must retain independent rows");
  ingestActivityMessage({
    type: "activity",
    event: "tool:result",
    tool: "read_file",
    call_id: "call-a",
    status: "invalid_arguments",
    text: "path is required",
    args_preview: "",
    title: "",
    surface: "side",
    duration_ms: 125,
    ts: 1_700_000_001,
  });
  let steps = getChatState().turnSteps;
  assert.equal(steps.find(step => step.callId === "call-a")?.status, "error");
  assert.equal(steps.find(step => step.callId === "call-a")?.resultPreview, "path is required");
  assert.equal(steps.find(step => step.callId === "call-b")?.status, "running");

  ingestActivityMessage({
    type: "activity",
    event: "tool:result",
    tool: "read_file",
    call_id: "call-b",
    status: "ok",
    text: "contents",
    args_preview: "",
    title: "",
    surface: "side",
    duration_ms: 250,
    ts: 1_700_000_002,
  });
  steps = getChatState().turnSteps;
  assert.equal(traceSummary(steps).label, "2 actions · 1 issue");

  const parsed = parseTurnSteps([{...steps[0], call_id: "call-a", duration_ms: 125}]);
  assert.equal(parsed?.[0].callId, "call-a");
  assert.equal(parsed?.[0].durationMs, 125);

  const entries = [
    conversationTimelineEntry("u1", "first question", 0),
    conversationTimelineEntry("u2", "second question", 2),
    conversationTimelineEntry("u3", "third question", 4),
    conversationTimelineEntry("u4", "fourth question", 6),
  ].filter(Boolean) as NonNullable<ReturnType<typeof conversationTimelineEntry>>[];
  assert.equal(entries.length, 4);
  assert.equal(activeConversationIndex(entries, 450, index => index * 100), 2);
}

function testOfflineWritesKeepDrafts(): void {
  const notices: string[] = [];
  setAutomationContext({
    send: () => false,
    notify: message => notices.push(message),
  });
  openAutomationBuilder();
  assert.equal(saveAutomation({
    name: "Daily brief",
    prompt: "Summarize today",
    trigger: {type: "daily", time: "09:00"},
    misfire_policy: "latest",
  }), false);
  assert.match(getAutomationState().saveError, /offline/i);

  const sent: Array<Record<string, unknown>> = [];
  setMemoryContext({
    send: () => false,
    notify: message => notices.push(message),
  });
  setDraftFact("Keep this fact");
  assert.equal(addCoreFact(), false);
  setDraftLoopTitle("Retained run title");
  setDraftLoopGoal("Retained run goal");
  assert.equal(createLoop(), false);

  setMemoryContext({
    send: payload => {
      sent.push(payload as Record<string, unknown>);
      return true;
    },
    notify: message => notices.push(message),
  });
  assert.equal(addCoreFact(), true, "offline fact draft must remain retryable");
  assert.equal(sent.at(-1)?.text, "Keep this fact");
  assert.equal(createLoop(), true, "offline project-run draft must remain retryable");
  assert.equal(sent.at(-1)?.title, "Retained run title");
  assert.equal(sent.at(-1)?.goal, "Retained run goal");
}

function testMemoryProjectRunRouting(): void {
  const routed = REACT_MODULE_MESSAGE_TYPES["react-runtime-memory"];
  for (const type of ["memory:loops", "memory:loop", "memory:loop:promote"] as const) {
    assert.ok(routed.includes(type), `${type} must reach the Memory runtime module`);
  }
}

function testWorkbenchToggleReopensHiddenPane(): void {
  __resetSessionStoreForTests();
  __resetWorkbenchForTests();
  togglePane(PANE.files, "right");
  assert.equal(getWorkbenchState().hidden[PANE.files], true);
  togglePane(PANE.files, "right");
  assert.equal(getWorkbenchState().hidden[PANE.files], false);
}

function testSettingsOverlayNavigation(): void {
  navigateTo("memory");
  navigateTo("settings");
  assert.equal(getAppState().view, "settings");
  assert.equal(
    getAppState().settingsReturnView,
      "chat",
      "opening Settings must retain the Chat workspace",
  );
  selectSettingsCategory("local-models");
  assert.equal(getAppState().settingsCategory, "local-models");
  closeSettings();
  assert.equal(
    getAppState().view,
      "chat",
      "closing Settings must reveal the Chat workspace",
  );

  navigateTo("settings");
  navigateTo("overview");
  assert.equal(getAppState().view, "overview");
    assert.equal(getAppState().settingsReturnView, "chat");
  navigateTo("chat");
}

async function testAttachmentCommitIsolation(): Promise<void> {
  const runtimeGlobal = globalThis as unknown as {FileReader?: typeof FileReader};
  const originalFileReader = runtimeGlobal.FileReader;
  const pendingReads: Array<() => void> = [];

  class DeferredFileReader {
    result: string | ArrayBuffer | null = null;
    error: Error | null = null;
    onload: (() => void) | null = null;
    onerror: (() => void) | null = null;

    readAsText(file: File) {
      pendingReads.push(() => {
        this.result = `contents:${file.name}`;
        this.onload?.();
      });
    }

    readAsArrayBuffer() {
      throw new Error("unexpected binary read");
    }
  }

  try {
    runtimeGlobal.FileReader = DeferredFileReader as unknown as typeof FileReader;
    __resetTurnStoreForTests();
    setChatState({...initialChatState(), draft:"retained while loading"});
    assert.equal(sendUserMessage("retained while loading"), false, "a send needs an acknowledged chat identity");
    assert.equal(getChatState().draft,"retained while loading");
    setChatState({...initialChatState(), sessionId:"attachment-session"});
    const sent: Array<Record<string, unknown>> = [];
    setChatContext({
      send: payload => {
        sent.push(payload as Record<string, unknown>);
        return true;
      },
      notify: () => {},
    });
    const lateFile = {
      name: "late.txt",
      type: "text/plain",
      size: 12,
    } as File;
    const pending = addChatFiles([lateFile]);
    await Promise.resolve();
    await Promise.resolve();
    assert.equal(pendingReads.length, 1);
    assert.equal(sendUserMessage("send before read finishes"), false,"preparing attachments gate composer sends");
    pendingReads.shift()?.();
    await pending;
    assert.equal(
      getChatState().attachments.length,
      1,
      "the prepared attachment stays available for the intended send",
    );
    assert.equal(sent.length,0);
    assert.equal(sendUserMessage("send after read finishes"),true);
    assert.equal((sent[0]?.attachments as unknown[]).length,1);

    __resetTurnStoreForTests();
    setChatState({...initialChatState(), sessionId:"attachment-session"});
    class ImmediateFileReader extends DeferredFileReader {
      override readAsText(file: File) {
        this.result = `contents:${file.name}`;
        this.onload?.();
      }
    }
    runtimeGlobal.FileReader = ImmediateFileReader as unknown as typeof FileReader;
    const makeFiles = (prefix: string) => Array.from({length: 4}, (_, index) => ({
      name: `${prefix}-${index}.txt`,
      type: "text/plain",
      size: 10,
    } as File));
    await Promise.all([
      addChatFiles(makeFiles("first")),
      addChatFiles(makeFiles("second")),
    ]);
    assert.equal(
      getChatState().attachments.length,
      6,
      "concurrent attachment batches must share one serialized capacity budget",
    );
    assert.equal(sendUserMessage(""), true);
    assert.equal(
      getChatState().messages.at(-1)?.text,
      "Attached 6 files: first-0.txt, first-1.txt, first-2.txt +3",
      "attachment-only optimistic text matches the backend durable label",
    );
    assert.equal(
      getChatState().messages.at(-1)?.attachments?.every(item => (
        item.data == null && item.text == null && item.path == null
      )),
      true,
      "optimistic transcript bubbles retain display metadata, not transport payloads",
    );
    assert.match(
      String((sent.at(-1)?.attachments as Array<Record<string, unknown>>)?.[0]?.text || ""),
      /contents:first-0\.txt/,
      "the one-shot wire payload still includes the attachment contents",
    );

    __resetTurnStoreForTests();
    setChatState({...initialChatState(), sessionId:"attachment-session"});
    await addChatFiles([{
      name: "retry.txt",
      type: "text/plain",
      size: 10,
    } as File]);
    setChatContext({send: () => false, notify: () => {}});
    assert.equal(sendUserMessage(""), false);
    assert.match(
      getChatState().attachments[0]?.text || "",
      /contents:retry\.txt/,
      "an immediate send failure restores the full attachment for retry",
    );
  } finally {
    invalidatePendingChatAttachments();
    __resetTurnStoreForTests();
    setChatState(initialChatState());
    if (originalFileReader) runtimeGlobal.FileReader = originalFileReader;
    else delete runtimeGlobal.FileReader;
  }
}

function testCanonicalLocalModelIdentity(): void {
  const first = "C:\\VARIANT-1\\models\\user\\repo-a\\model.gguf";
  const second = "C:\\VARIANT-1\\models\\user\\repo-b\\model.gguf";
  assert.equal(isActiveModel({path: first}, "c:/variant-1/models/user/repo-a/model.gguf"), true,
    "full local-model identities should normalize slash and case differences");
  assert.equal(isActiveModel({name: "model.gguf", path: first}, "model.gguf"), false,
    "a basename must not activate one of multiple same-named repository models");
  assert.equal(isActiveModel({name: "model.gguf", path: second}, "model.gguf"), false,
    "ambiguous basenames must leave every candidate inactive");
  assert.equal(isActiveModel({name: "repo-a/model.gguf", path: first}, "repo-a/model.gguf"), false,
    "relative display names must not bypass canonical full-path identity");

  ingestGeneral({
    type: "models",
    current: first,
    items: [{name: "repo-a/model.gguf", path: first}],
  });
  assert.equal(getGeneralState().model, first);
  ingestGeneral({type: "engine", model: "model.gguf", inference_runtime: "llamacpp"});
  assert.equal(getGeneralState().model, first,
    "engine display basenames must not overwrite the canonical models snapshot");

  ingestGeneral({type: "config", voice: {tts: {provider: "kokoro"}}});
  ingestGeneral({
    type: "tts:voices", provider: "xai",
    items: [{id: "cloud-only", name: "Cloud only"}],
  });
  assert.equal(
    getGeneralState().voices.some(item => item.id === "cloud-only"),
    false,
    "an out-of-order voice list from the old provider must be ignored",
  );
  ingestGeneral({
    type: "tts:voices", provider: "kokoro",
    items: [{id: "local-voice", name: "Local voice"}],
  });
  assert.equal(getGeneralState().voices[0]?.id, "local-voice");
  assert.equal(getGeneralState().voicesLoaded, true);
}

async function testDurableTerminalRuntime(): Promise<void> {
  const sent: Array<Record<string, unknown>> = [];
  setTerminalContext({
    send(command) {
      sent.push(command as Record<string, unknown>);
      return true;
    },
    notify() {},
  });
  setTerminalConnection("connected");
  assert.equal(getTerminalSnapshot().supported, true);
  assert.equal(sent[0]?.type, "execution:get",
    "backend connection must hydrate the durable execution snapshot");

  const opening = openNewTerminal("C:/workspace");
  const repeated = Array.from({length:20}, () => openNewTerminal("C:/workspace"));
  assert.equal(sent.filter(command => command.type === "terminal:open").length,1,"repeat clicks share the pending creation");
  assert.equal(getTerminalSnapshot().opening,true);
  const open = sent.find(command => command.type === "terminal:open");
  assert.ok(open, "opening a terminal must use the backend WebSocket command");
  assert.equal(open?.cwd, "C:/workspace");
  ingestTerminal({
    type: "terminal:accepted",
    operation: "open",
    request_id: open?.request_id,
    result: {
      id: "terminal-1",
      state: "running",
      cwd: "C:/workspace",
      profile: "powershell",
      transport: "conpty",
      dimensions: {cols: 120, rows: 30},
      capabilities: {true_pty: true},
    },
  });
  assert.equal(await opening, true);
  assert.ok((await Promise.all(repeated)).every(Boolean));
  assert.equal(getTerminalSnapshot().opening,false);
  assert.equal(getTerminalSnapshot().terminals.length, 1);
  assert.equal(getTerminalSnapshot().activeId, "terminal-1");
  assert.equal(getTerminalSnapshot().running, true);

  ingestTerminal({
    type: "terminal:accepted",
    operation: "read",
    result: {frames: [{text: "durable output\r\n"}], next_cursor: 16},
  });
  assert.match(getTerminalSnapshot().output, /durable output/);
  assert.equal(writeTerminalInputFor("terminal-1", "Get-Date\n"), true);
  assert.equal(sent.at(-1)?.type, "terminal:write");
  assert.equal(interruptTerminal(), true);
  assert.equal(sent.at(-1)?.type, "terminal:signal");
  assert.equal(killTerminal(), true);
  assert.equal(sent.at(-1)?.type, "terminal:close");
  const closeRequest=sent.at(-1)?.request_id;
  assert.equal(killTerminal(),false,"pending close cannot be duplicated");
  ingestTerminal({type:"terminal:accepted",operation:"close",request_id:closeRequest,result:{id:"terminal-1",state:"terminated"}});
  assert.equal(getTerminalSnapshot().activeId,"");
  assert.equal(getTerminalSnapshot().output,"");
  ingestTerminal({type:"terminal:accepted",operation:"read",result:{entity:{id:"terminal-1",kind:"terminal"},frames:[{text:"late TUI"}],next_cursor:20}});
  assert.equal(getTerminalSnapshot().output,"");

  ingestTerminal({type:"execution:snapshot",terminals:[
    {id:"terminal-1",state:"running"},{id:"terminal-2",state:"running"},{id:"archived",state:"exited"},
  ]});
  assert.equal(getTerminalSnapshot().terminals.some(t=>t.id==="terminal-1"),false,"snapshot cannot reopen explicitly closed terminal");
  selectTerminal("archived");
  const count = sent.filter(command => command.type === "terminal:open").length;
  assert.equal(writeTerminalInputFor("archived","\x1b[2;1R"),false);
  assert.equal(writeTerminalInputFor("archived", "replayed data"),false);
  assert.equal(writeTerminalInputFor("terminal-2","\x1b[2;1R"),true);
  assert.equal(sent.at(-1)?.terminal_id,"terminal-2","a terminal reply remains bound to its originating PTY");
  assert.equal(getTerminalSnapshot().activeId,"archived","protocol replies cannot change the selected terminal");
  assert.equal(sent.filter(command => command.type === "terminal:open").length,count);

  setTerminalConnection("offline");
  assert.equal(getTerminalSnapshot().supported, false);
  assert.equal(writeTerminalInputFor("terminal-1", "echo bypass\n"), false,
    "terminal input must not fall back to an Electron-spawned shell");
  disposeTerminalRuntime();
}

function testNavigationAndVoiceOwnership(): void {
  const sent: Array<Record<string, unknown>> = [];
  const context = {send: (command: Record<string, unknown>) => { sent.push(command); return true; }, notify() {}, isOpen: () => true};
  const reset = () => {
    __resetTurnStoreForTests(); __resetSessionStoreForTests();
    setChatState({...initialChatState(), sessionId: "A", connected: true});
    noteDisplayedSession("A"); setSessionContext(context); setChatContext(context); sent.length = 0;
  };
  reset();
  switchSession("deleted-B");
  const request = sent.at(-1)!;
  ingestChat(parseChatWsMessage({type: "chat:session", session: {id: "A", messages: []}, navigation: {
    request_id: request.request_id, requested_id: "deleted-B", effective_id: "A", status: "fallback",
  }}));
  assert.equal(getSessionState().pendingAction, null);
  assert.equal(sendUserMessage("A is still usable"), true);
  reset();
  switchSession("B"); const old = sent.at(-1)!;
  switchSession("C"); const current = sent.at(-1)!;
  ingestChat(parseChatWsMessage({type: "chat:session", session: {id: "B", messages: []}, navigation: {
    request_id: old.request_id, requested_id: "B", effective_id: "B", status: "switched",
  }}));
  assert.equal(getChatState().sessionId, "A");
  assert.equal(getSessionState().pendingAction?.type === "switch" && getSessionState().pendingAction?.id, "C");
  ingestSessions({...current, type: "chat:switch:result", status: "rejected",
    requested_id: "C", effective_id: "A", error: "Unavailable"});
  assert.equal(getSessionState().pendingAction, null);
  assert.equal(getChatState().sessionId, "A");
  switchSession("B"); switchSession("A");
  assert.equal(sent.at(-1)?.id, "A", "selecting the displayed chat supersedes an outstanding navigation");

  for (const active of [false, true]) {
    reset();
    const attachments = [{id: "typed-attachment", name: "notes.txt", kind: "text" as const, mime: "text/plain", size: 5, text: "notes"}];
    setChatState({...getChatState(), draft: "independently typed", attachments, turnActive: active});
    if (active) turnController.begin({clientId: getChatState().clientId, source: "chat", sessionId: "A"});
    assert.equal(submitUserInput({source: "voice", text: "spoken instruction", sessionId: "A"}), true);
    const command = sent.find(message => message.type === "chat")!;
    assert.equal(command.text, "spoken instruction"); assert.equal(command.attachments, undefined);
    assert.equal(getChatState().draft, "independently typed");
    assert.equal(getChatState().attachments, attachments, "voice input cannot consume attachments owned by typing");
    assert.equal(submitUserInput({source: "voice", text: "wrong owner", sessionId: "B"}), false);
    ingestChat(parseChatWsMessage(active
      ? {type:"chat:queue_rejected",session_id:"A",client_id:getChatState().clientId,id:command.ticket_id,error:"session_configuration_pending"}
      : {type:"chat:rejected",session_id:"A",client_id:getChatState().clientId,error:"session_configuration_pending"}));
    assert.match(getChatState().draft,/spoken instruction/);
    assert.match(getChatState().draft,/independently typed/);
    assert.equal(getChatState().attachments,attachments,"late voice rejection preserves independently attached files");
  }
  reset();
  const stale = {source: "composer" as const, text: "old draft", sessionId: "A", revision: getComposerRevision(), attachments: []};
  setChatState({...getChatState(), draft: "new draft"});
  assert.equal(submitUserInput(stale), false);
  assert.equal(getChatState().draft, "new draft");
  reset();
}

async function testMicCaptureOwner(): Promise<void> {
  for (const phase of ["permission", "recording"]) {
    let owner = "A";
    const resources = fakeMicResources();
    let resolveMedia!: (value: MediaStream) => void;
    const commands: Array<Record<string, unknown>> = [];
    const mic = new MicController({isOpen: () => true, getSessionId: () => owner,
      send: command => { commands.push(command); return true; }, notify() {}, onPhase() {},
      getUserMedia: () => new Promise(resolve => { resolveMedia = resolve; }),
      createAudioContext: () => resources.context, loadWorklet: async () => {},
      createWorkletNode: () => resources.worklet, encodeAudio: () => "dummy-audio"});
    const pending = mic.start();
    if (phase === "permission") owner = "B";
    resolveMedia(resources.stream); await pending;
    if (phase === "recording") owner = "B";
    resources.worklet.port.onmessage?.({data: {type: "frame", samples: new Float32Array([.2]), rms: .2, frames: 1}} as MessageEvent);
    mic.stop(true);
    assert.equal(commands[0].session_id, "A", `capture retains its owner across a switch during ${phase}`);
    mic.dispose();
  }
}

function testChatResourceOwnership(): void {
  disposeTerminalRuntime();__resetSessionStoreForTests();
  const sent:Record<string,unknown>[]=[];
  const context={send:(command:any)=>{sent.push(command);return true;},notify:()=>{}};
  noteDisplayedSession("owner-a");setTerminalContext(context);setTerminalConnection("connected");
  ingestTerminal({type:"execution:snapshot",chat_id:"owner-a",terminals:[{id:"pty-a",state:"running"}]});
  noteDisplayedSession("owner-b");
  ingestTerminal({type:"execution:snapshot",chat_id:"owner-b",terminals:[{id:"pty-b",state:"running"}]});
  ingestTerminal({type:"terminal:accepted",chat_id:"owner-a",operation:"read",result:{entity:{id:"pty-a",kind:"terminal"},frames:[{text:"A TUI"}],next_cursor:1}});
  assert.equal(getTerminalSnapshot().activeId,"pty-b");assert.equal(getTerminalSnapshot().output,"");
  assert.equal(getTerminalSnapshot("owner-a").output,"A TUI");
  assert.equal(killTerminal("owner-a"),true);
  const close=sent.at(-1)!;assert.equal(close.chat_id,"owner-a");assert.equal(close.terminal_id,"pty-a");
  ingestTerminal({type:"terminal:accepted",chat_id:"owner-a",request_id:close.request_id,operation:"close",result:{id:"pty-a",state:"terminated"}});
  assert.equal(getTerminalSnapshot("owner-a").activeId,"");assert.equal(getTerminalSnapshot().activeId,"pty-b");
  setChatProjectContext(context);assert.equal(setChatProject("owner-a","C:/project-a"),true);
  const request=sent.at(-1)!;
  ingestChatProjects({type:"chat:project:result",chat_id:"owner-b",request_id:request.request_id,ok:true,project:{root:"C:/wrong",name:"wrong"}});
  assert.equal(getChatProjects().projects["owner-b"],undefined);
  ingestChatProjects({type:"chat:project:result",chat_id:"owner-a",request_id:request.request_id,ok:true,project:{root:"C:/project-a",name:"project-a"}});
  assert.equal(getChatProjects().projects["owner-a"]?.root,"C:/project-a");
  assert.equal(getSessionState().displayedSessionId,"owner-b");
  disposeTerminalRuntime();__resetSessionStoreForTests();
}

export async function run(): Promise<void> {
  await testBackendClient();
  await testDeckRuntimeHydration();
  testTurnStore();
  testSessionStore();
  testAuthoritativeTurnReconciliation();
  testActiveInputAndRejectionContract();
  testMutationToggleProtocolStoreAndSnapshots();
  testRunSettlementProtocol();
  testMutationToggleControlReasons();
  testTurnReceiptHelpers();
  testWireStatusColdStart();
  testWorkbenchLayoutHelpers();
  testHermesActivityProjection();
  testEvidenceExtraction();
  testOfflineWritesKeepDrafts();
  testMemoryProjectRunRouting();
  testSettingsOverlayNavigation();
  testWorkbenchToggleReopensHiddenPane();
  await testAttachmentCommitIsolation();
  await testDurableTerminalRuntime();
  testCanonicalLocalModelIdentity();
  await testMicController();
  testNavigationAndVoiceOwnership();
  await testMicCaptureOwner();
  __resetSessionContextStoreForTests();
  testChatResourceOwnership();
  console.log("deck TypeScript runtime: all tests passed");
}
