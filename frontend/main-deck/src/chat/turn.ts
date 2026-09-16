/**
 * Live turn: STEPS strip, activity ingest, and stream finish.
 */
import type {
  ChatAppendedMessage,
  StreamActivityMessage,
  StreamDoneMessage,
} from "../protocol";
import type {
  ChatEvidence,
  ChatMessage,
  ChatTurnStep,
} from "./types";
import {
  MAX_TURN_STEPS,
  emit,
  endSharedTurn,
  getChatState,
  patchChatState,
  replaceChatState,
  sendChat,
  setSubtitle,
  sharedTurnActive,
  turnApi,
} from "./stateCore";
import {
  newStepId,
  parseMessage,
  parseMessages,
  reconcileAuthoritativeTurn,
  chatMessagesMatch,
} from "./messages";
import {
  noteReceiptTool,
  parseTurnReceipt,
  readableToolName,
  snapshotTurnReceipt,
} from "./receipt";
import {extractEvidence, mergeEvidence} from "./evidence";
import {syncPaneFromActivity} from "../workbench/activityRouting";
import {normalizeActivityStatus} from "./activityModel";

function activityTimeMs(value: number | undefined): number {
  const numeric = Number(value) || 0;
  if (!numeric) return Date.now();
  return numeric < 1_000_000_000_000 ? numeric * 1_000 : numeric;
}

/** Upsert a turn step (tool start/result merge on the same key). */
export function pushTurnStep(partial: {
  id?: string;
  summaryState?: ChatTurnStep["summaryState"];
  summaryRevision?: number;
  kind: ChatTurnStep["kind"];
  label: string;
  detail?: string;
  status?: ChatTurnStep["status"];
  tool?: string;
  key?: string;
  evidence?: ChatEvidence[];
  callId?: string;
  rawStatus?: string;
  argsPreview?: string;
  resultPreview?: string;
  startedAt?: number;
  completedAt?: number;
  durationMs?: number;
  admissionMs?: number;
  appendDetail?: boolean;
  peerMessage?:ChatTurnStep["peerMessage"];
}) {
  const state = getChatState();
  if (!sharedTurnActive() && !state.turnActive && !state.streaming) return;
  const key = partial.key || "";
  const steps = [...state.turnSteps];
  if (key) {
    for (let i = steps.length - 1; i >= 0; i -= 1) {
      // A later thought belongs after the intervening cell, not in a completed thought.
      const exactIdentity = !!partial.callId || !!partial.id;
      if (steps[i].key === key && (exactIdentity || steps[i].status === "running")) {
        const revision = steps[i].summaryRevision;
        if (partial.summaryState && revision !== undefined
          && (partial.summaryRevision === undefined || partial.summaryRevision <= revision)) return;
        if (partial.summaryState === "running" && steps[i].summaryState && steps[i].summaryState !== "running") return;
        const priorDetail = steps[i].detail || "";
        const incomingDetail = partial.detail || "";
        const detail = partial.appendDetail && incomingDetail
          ? (incomingDetail.startsWith(priorDetail)
            ? incomingDetail
            : `${priorDetail}${incomingDetail}`).slice(-16_000)
          : partial.detail != null ? partial.detail : steps[i].detail;
        steps[i] = {
          ...steps[i],
          summaryState: partial.summaryState || steps[i].summaryState,
          summaryRevision: partial.summaryRevision ?? steps[i].summaryRevision,
          label: partial.label || steps[i].label,
          detail,
          status: partial.status || steps[i].status,
          kind: partial.kind || steps[i].kind,
          tool: partial.tool || steps[i].tool,
          callId: partial.callId || steps[i].callId,
          rawStatus: partial.rawStatus || steps[i].rawStatus,
          argsPreview: partial.argsPreview || steps[i].argsPreview,
          resultPreview: partial.resultPreview != null ? partial.resultPreview : steps[i].resultPreview,
          startedAt: steps[i].startedAt || partial.startedAt,
          completedAt: partial.completedAt || steps[i].completedAt,
          durationMs: partial.durationMs ?? steps[i].durationMs,
          admissionMs: partial.admissionMs ?? steps[i].admissionMs,
          evidence: mergeEvidence(steps[i].evidence, partial.evidence),
          peerMessage:partial.peerMessage || steps[i].peerMessage,
          ts: partial.completedAt || partial.startedAt || Date.now(),
        };
        patchChatState({turnSteps: steps.slice(-MAX_TURN_STEPS)});
        return;
      }
    }
  }
  steps.push({
    id: partial.id || newStepId(),
    summaryState: partial.summaryState,
    summaryRevision: partial.summaryRevision,
    kind: partial.kind,
    label: partial.label,
    detail: partial.detail,
    status: partial.status || "done",
    tool: partial.tool,
    callId: partial.callId,
    rawStatus: partial.rawStatus,
    argsPreview: partial.argsPreview,
    resultPreview: partial.resultPreview,
    startedAt: partial.startedAt,
    completedAt: partial.completedAt,
    durationMs: partial.durationMs,
    admissionMs: partial.admissionMs,
    key: key || undefined,
    evidence: partial.evidence?.length ? partial.evidence : undefined,
    peerMessage:partial.peerMessage,
    ts: partial.completedAt || partial.startedAt || Date.now(),
  });
  patchChatState({turnSteps: steps.slice(-MAX_TURN_STEPS)});
}

/**
 * Main-chat STEPS show tools and named task rows. Loop internals stay off
 * the transcript even though they share the same activity feed.
 */
function activityShowsOnMainChat(message: StreamActivityMessage): boolean {
  const event = message.event;
  const tool = message.tool.trim();
  if (event === "tool:start" || event === "tool:result" || event === "tool:activity") {
    return true;
  }
  if (tool && (message.status || "").toLowerCase() === "running") return true;
  if (event === "loop:detail") return false;
  if (event === "task:step" && !(message.text || message.title)) return false;
  if (message.surface.toLowerCase() === "side") return false;
  return true;
}

export function ingestActivityMessage(message: StreamActivityMessage) {
  if(message.event==="peer:sent" && message.peer_message){
    if(message.durable_replay)return;
    const peer=message.peer_message;
    pushTurnStep({kind:"step",label:`Message to ${peer.target_display_name || "Peer agent"}`,key:`peer:${peer.message_id}`,callId:`peer:${peer.message_id}`,peerMessage:peer,status:peer.state==="failed" ? "error" : "done",rawStatus:peer.state});return;
  }
  if(message.durable_replay) {
    enrichReplayedActivity(message);
    return;
  }
  const event = message.event;
  const tool = message.tool.trim();
  const statusRaw = message.status.toLowerCase();
  const text = (
    message.text || message.args_preview || message.title || ""
  ).replace(/\s+/g, " ").trim().slice(0, 140);
  const evidence = extractEvidence({
    tool,
    argsPreview: message.args_preview,
    text: [message.text, message.title].filter(Boolean).join(" "),
  });

  // The targeted `tool:activity` frame is the Chat authority for starts. The
  // shared operational activity hub broadcasts the same start without a call
  // identity; rendering both would create a second, unpairable row.
  if (message.type === "activity" && event === "tool:start" && !message.call_id) {
    return;
  }

  // Side-only events never enter the main-chat STEPS strip.
  if (!activityShowsOnMainChat(message)) {
    // Still update subtitle lightly for tool activity so the header isn't frozen.
    if ((event === "tool:start" || event === "tool:result") && text) {
      setSubtitle(text.slice(0, 90), "working");
      emit();
    }
    return;
  }

  if (event === "tool:result" || event === "tool:start" || (tool && statusRaw === "running")) {
    syncPaneFromActivity({
      event,
      tool,
      status: message.status,
      text: message.text || message.title,
      argsPreview: message.args_preview,
    });
  }

  if (event === "tool:start" || event === "tool:activity" || (tool && statusRaw === "running")) {
    if (tool) noteReceiptTool(tool);
    settleRunningThinking();
    const startedAt = activityTimeMs(message.ts);
    const callId = String(message.call_id || "").trim();
    pushTurnStep({
      kind: "tool",
      label: readableToolName(tool || "tool"),
      detail: text || undefined,
      status: "running",
      tool: tool || undefined,
      callId: callId || undefined,
      rawStatus: message.status || "running",
      argsPreview: message.args_preview || undefined,
      startedAt,
      admissionMs: message.admission_ms,
      key: callId ? `call:${callId}` : tool ? `legacy-tool:${tool}` : undefined,
      evidence,
    });
    if (text) setSubtitle(text.slice(0, 90), "working");
    if (tool === "ipython") {
      const sessionId = getChatState().sessionId;
      if (sessionId) sendChat({type: "chat:runtime:get", id: sessionId});
    }
    return;
  }
  if (event === "tool:result") {
    const status = normalizeActivityStatus(statusRaw, event);
    const callId = String(message.call_id || "").trim();
    const completedAt = activityTimeMs(message.ts);
    const durationMs = message.duration_ms ?? message.total_duration_ms;
    if (tool) noteReceiptTool(tool);
    pushTurnStep({
      kind: "tool",
      label: readableToolName(tool || "tool"),
      status,
      tool: tool || undefined,
      callId: callId || undefined,
      rawStatus: message.status || (status === "error" ? "error" : "ok"),
      resultPreview: text || undefined,
      completedAt,
      startedAt: durationMs ? completedAt - durationMs : undefined,
      durationMs,
      admissionMs: message.admission_ms,
      key: callId ? `call:${callId}` : tool ? `legacy-tool:${tool}` : undefined,
      evidence,
    });
    if (text) setSubtitle(text.slice(0, 90), "working");
    if (tool === "ipython") {
      const sessionId = getChatState().sessionId;
      if (sessionId) sendChat({type: "chat:runtime:get", id: sessionId});
    }
    return;
  }
  if (event === "task:thinking" || event === "thinking") {
    pushTurnStep({
      kind: "thinking",
      label: "Thinking",
      detail: message.text || text || undefined,
      appendDetail: true,
      status: "running",
      key: "thinking",
      startedAt: activityTimeMs(message.ts),
    });
    if (text) setSubtitle(text.slice(0, 90), "working");
    return;
  }
  if (event === "task:step") {
    // Bare step counters (no text) just noise the STEPS strip when tools already
    // log real rows — only surface steps that carry a human-readable label.
    if (!text) return;
    const stepNo = message.step || "";
    pushTurnStep({
      kind: "step",
      label: text.slice(0, 100),
      detail: stepNo ? `Step ${stepNo}` : undefined,
      status: "running",
      key: stepNo ? `step:${stepNo}` : `step:${Date.now()}`,
      evidence,
    });
    setSubtitle(text.slice(0, 90), "working");
    return;
  }
  if (event === "task:done") {
    // Finalize any still-running rows so the committed strip is not stuck on "Live".
    const state = getChatState();
    const completedAt = activityTimeMs(message.ts);
    const steps = state.turnSteps.map(s => (
      s.status === "running" ? {
        ...s,
        status: "done" as const,
        completedAt,
        durationMs: s.startedAt ? Math.max(0, completedAt - s.startedAt) : s.durationMs,
        ts: completedAt,
      } : s
    ));
    if (text) {
      steps.push({
        id: newStepId(),
        kind: "note",
        label: text.slice(0, 100),
        status: "done",
        ts: Date.now(),
      });
    }
    patchChatState({turnSteps: steps.slice(-MAX_TURN_STEPS)});
    if (text) setSubtitle(text.slice(0, 90), "working");
    return;
  }
  if (event === "task:start" || event === "note"
    || event === "loop:progress" || event === "loop:start" || event === "loop:status") {
    if (event === "task:start" && /^working on your request$/i.test(text)) return;
    if (text) {
      pushTurnStep({
        kind: "note",
        label: text.slice(0, 120),
        status: "done",
        key: event.startsWith("loop:")
          ? `loop:${message.loop_id || message.kind || event}:${Date.now()}`
          : undefined,
        evidence,
      });
      setSubtitle(text.slice(0, 90), "working");
    }
  }
}

/** Replay enriches an existing exact call only; it cannot create work or reveal UI. */
function enrichReplayedActivity(message:StreamActivityMessage):void {
  const callId=message.call_id;
  if(!callId || message.event!=="tool:result")return;
  const status=normalizeActivityStatus(message.status.toLowerCase(),message.event);
  if(status==="running")return;
  let changed=false;
  const update=(step:ChatTurnStep):ChatTurnStep=>{
    if(step.callId!==callId)return step;
    const next={...step,status,rawStatus:message.status || step.rawStatus,
      resultPreview:message.text?message.text.slice(0,16000):step.resultPreview,
      durationMs:message.duration_ms ?? message.total_duration_ms ?? step.durationMs,
      completedAt:message.ts?activityTimeMs(message.ts):step.completedAt};
    if(JSON.stringify(step)===JSON.stringify(next))return step;
    changed=true;return next;
  };
  const state=getChatState(),turnSteps=state.turnSteps.map(update);
  const messages=state.messages.map(row=>row.steps?.some(step=>step.callId===callId)?{...row,steps:row.steps.map(update)}:row);
  if(changed)patchChatState({turnSteps,messages});
}

export function settleRunningThinking(): void {
  const state = getChatState();
  let changed = false;
  const completedAt = Date.now();
  const steps = state.turnSteps.map(step => {
    if (step.kind !== "thinking" || step.status !== "running") return step;
    changed = true;
    return {
      ...step,
      status: "done" as const,
      completedAt,
      durationMs: step.startedAt ? Math.max(0, completedAt - step.startedAt) : step.durationMs,
      ts: completedAt,
    };
  });
  if (changed) patchChatState({turnSteps: steps});
}

export function finishStream(message: StreamDoneMessage) {
  if(!sharedTurnActive())return;
  const state = getChatState();
  const runId = message.run_id || turnApi().snapshot().runId || undefined;
  const pending = state.pendingTurnCommit?.owner === (state.activeTurnId || runId) ? state.pendingTurnCommit : undefined;
  const finalText = (message.text || state.streamText || "…").trim() || "…";
  const cancelled = message.cancelled;
  const nowMs = Date.now();
  const now = nowMs / 1000;
  // Finalize still-running rows so the committed expander is not stuck on Live.
  const steps = state.turnSteps.length
    ? state.turnSteps.map(s => (
      s.status === "running" ? {
        ...s,
        status: "done" as const,
        summaryState: s.summaryState === "running" ? (cancelled ? "cancelled" as const : "done" as const) : s.summaryState,
        completedAt: nowMs,
        durationMs: s.startedAt ? Math.max(0, nowMs - s.startedAt) : s.durationMs,
        ts: nowMs,
      } : s
    ))
    : undefined;
  // Commit the assistant turn optimistically. The later chat:appended replaces
  // this local turn group with the backend's ordered durable transcript.
  const nextMessages = [...state.messages];
  // Drop any residual streaming placeholder markers.
  const cleaned = nextMessages.filter(m => !m.streaming);
  cleaned.push({
    role: "assistant",
    runId,
    text: finalText,
    ts: now,
    steps,
    receipt: snapshotTurnReceipt(),
    optimisticTurnId: state.activeTurnId || undefined,
    durability: message.durable === false || pending?.failed ? "failed" : undefined,
  });
  replaceChatState({
    ...state,
    messages: cleaned,
    turnActive: false,
    stopPending: false,
    pause: null,
    streaming: false,
    streamText: "",
    turnSteps: [],
    pendingTurnCommit: undefined,
    // Active-input bubbles remain provisional until chat:appended proves
    // delivery or chat:queue_settled rejects them and restores their drafts.
    queuedFollowUps: state.queuedFollowUps,
    pendingActiveInputs: state.pendingActiveInputs,
  });
  endSharedTurn(cancelled ? "cancelled" : "complete");
  if (pending?.appended) handleAppended(pending.appended);
  setSubtitle(cancelled ? "Task stopped" : "Connected locally", cancelled ? "idle" : "ready");
  emit();
}

/** Replace the provisional done meter with the authoritative run aggregate. */
export function applySettledReceipt(
  raw: Readonly<Record<string, unknown>>,
  sessionId = "",
  runId = "",
): boolean {
  const receipt = parseTurnReceipt(raw);
  if (!receipt) return false;
  const state = getChatState();
  if (!runId || (sessionId && state.sessionId !== sessionId)) return false;
  let index = -1;
  for (let cursor = state.messages.length - 1; cursor >= 0; cursor -= 1) {
    if (state.messages[cursor].role === "assistant" && state.messages[cursor].runId === runId) {
      index = cursor;
      break;
    }
  }
  if (index < 0) return false;
  if (JSON.stringify(state.messages[index].receipt) === JSON.stringify(receipt)) return false;
  const messages = [...state.messages];
  messages[index] = {...messages[index], receipt};
  patchChatState({messages});
  const targetSession = sessionId || state.sessionId || "";
  if (targetSession) {
    sendChat({
      type: "chat:session:annotate",
      id: targetSession,
      run_id: runId,
      steps: messages[index].steps || [],
      receipt,
    });
  }
  return true;
}

/** Mark the history row that owns the in-flight turn (working badge). */
export function isForeignStream(message: {
  client_id?: string;
  source?: string;
}): boolean {
  const source = message.source || "";
  if (source === "voice") return true;
  const client = message.client_id || "";
  if (client.startsWith("voice-") || client.startsWith("mic-")) {
    return true;
  }
  return false;
}

export function activeTurnClientIds(): Set<string> {
  const ids = new Set<string>();
  const state = getChatState();
  if (state.clientId) ids.add(state.clientId);
  const turn = turnApi();
  if (turn) {
    const turnClient = String(turn.getClientId() || "");
    if (turnClient) ids.add(turnClient);
  }
  return ids;
}

export function handleAppended(message: ChatAppendedMessage) {
  const state = getChatState();
  const sessionId = message.session_id || "";
  if (state.sessionId && sessionId && sessionId !== state.sessionId) return;

  // Non-chat voice surfaces persist to history but must not inject a second
  // copy into the live Chat transcript while Chat is open.
  if (isForeignStream(message)) return;

  // De-duplicate against the Chat client id and shared turn client id so a
  // committed exchange is not appended again after its live stream finishes.
  const eventClient = message.client_id || "";
  const user = parseMessage(message.user);
  const assistant = parseMessage(message.assistant);
  const authoritative = parseMessages(message.messages);
  const continuedTicket = user?.ticketId || authoritative.find(row=>row.role==="user" && row.ticketId)?.ticketId;
  const sameClient = (!!eventClient && activeTurnClientIds().has(eventClient))
    || (message.source==="queue_continue" && !!continuedTicket && state.messages.some(row=>row.role==="user" && row.ticketId===continuedTicket));
  const now = Date.now() / 1000;

  if (sameClient) {
    // Replace exactly this optimistic turn with the backend's genuine ordered
    // transcript, including intermediate assistant and queued-user boundaries.
    if (authoritative.length) {
      const current = getChatState();
      const turn = turnApi().snapshot();
      const group = current.messages.filter(row => row.optimisticTurnId === current.activeTurnId);
      if (turn.active && current.activeTurnId && !group.some(row => row.role === "assistant")
        && (!message.run_id || !turn.runId || message.run_id === turn.runId)
        && (!message.admission_id || !turn.admissionId || message.admission_id === turn.admissionId)
        && group.some(row => row.role === "user" && authoritative.some(remote => chatMessagesMatch(row, remote)))) {
        patchChatState({pendingTurnCommit: {owner: current.activeTurnId, appended: message}});
        return;
      }
      const reconciled = reconcileAuthoritativeTurn(current.messages, authoritative);
      if (reconciled.optimisticTurnId) {
        const deliveredActiveInputs = reconciled.optimisticTurnIds.filter(
          id => id !== reconciled.optimisticTurnId,
        ).length;
        const nextMessages = reconciled.messages;
        patchChatState({
          messages: nextMessages,
          activeTurnId: current.activeTurnId === reconciled.optimisticTurnId
            ? null
            : current.activeTurnId,
          pendingActiveInputs: current.pendingActiveInputs.filter(
            item => !reconciled.optimisticTurnIds.includes(item.optimisticTurnId),
          ),
          queuedFollowUps: Math.max(
            0,
            current.queuedFollowUps - deliveredActiveInputs,
          ),
        });
        // The backend assistant row now exists. Persist local STEPS/receipt
        // only after this correlated durable append, never on the earlier
        // visible `done` boundary where it could annotate the prior turn.
        const committedAssistant = reconciled.committedAssistant;
        if (
          sessionId
          && (committedAssistant?.steps?.length || committedAssistant?.receipt)
          && (committedAssistant.runId || committedAssistant === [...nextMessages].reverse().find(row => row.role === "assistant"))
        ) {
          sendChat({
            type: "chat:session:annotate",
            id: sessionId,
            ...(committedAssistant.runId ? {run_id: committedAssistant.runId} : {}),
            steps: committedAssistant.steps || [],
            receipt: committedAssistant.receipt,
          });
        }
        return;
      }
    }
    return;
  }

  // Remote append from another chat surface such as an overlay.
  const extra: ChatMessage[] = authoritative.length ? authoritative : [];
  if (!extra.length && user?.text) extra.push({...user, ts: user.ts ?? now});
  if (!extra.length && assistant?.text) extra.push({...assistant, ts: assistant.ts ?? now});
  if (!extra.length) return;
  const cur = getChatState();
  patchChatState({messages: [...cur.messages, ...extra]});
}
