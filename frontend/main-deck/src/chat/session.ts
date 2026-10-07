/** Session apply and message rehydration. */
import {activateTerminalSession} from "../context/terminalStore";
import {
  acceptIncomingSession,
  noteDisplayedSession,
} from "../state/sessionStore";
import {
  emit,
  activateChatState,
  getChatState,
  replaceChatState,
  setSubtitle,
  sharedTurnActive,
  turnApi,
} from "./stateCore";
import {
  mergeMessageEnrichment,
  parseMessages,
  parseTurnSteps,
  reconcileActiveTranscript,
} from "./messages";
import {ACTION_SURFACE} from "./runtimeProfile";
import type {ChatRuntimeState, ChatState, ChatTurnStep} from "./types";
import type {ChatNavigationOutcome} from "../protocol/chatEvents";
import {invalidatePendingChatAttachments} from "./attachments";
import {restorePauseFromRuntime,runtimeConflictsWithActiveRun} from "./pause";
import {parseInputQueueSnapshot} from "../protocol/chatQueue";
import {applyInputQueue,refreshInputQueue,reconcileContinuedHistory} from "./inputQueue";
import {refreshComposerGoal} from "./goals";
import {refreshAgentTeam} from "./agentTeam";
import {restoreRecoveryNotice,dismissRecoveryNotice} from "./recovery";
import {persistTrace} from "./annotations";

function parseRuntime(value: unknown): ChatRuntimeState | null {
  if (!value || typeof value !== "object") return null;
  const row = value as Record<string, unknown>;
  const kernel = row.kernel && typeof row.kernel === "object"
    ? row.kernel as Record<string, unknown>
    : {};
  const activeSlots = Array.isArray(row.active_slots) ? row.active_slots : [];
  const children = row.children && typeof row.children === "object"
    ? row.children as Record<string, unknown>
    : {};
  const actionSurface = String(row.action_surface || ACTION_SURFACE);
  return {
    inputQueue:parseInputQueueSnapshot(row.input_queue) || undefined,
    busy: typeof row.busy === "boolean" ? row.busy : undefined,
    activeAdmissionId: String(row.active_admission_id || ""),
    activeRunId: String(row.active_run_id || ""),
    pauseState: ["pausing","paused","running","idle"].includes(String(row.pause_state)) ? row.pause_state as ChatRuntimeState["pauseState"] : undefined,
    pauseRevision: typeof row.pause_revision === "number" && Number.isSafeInteger(row.pause_revision) && row.pause_revision >= 0 ? row.pause_revision : undefined,
    actionSurface,
    trustProfile: String(row.trust_profile || "trusted-local.v1"),
    catalogReleaseId: String(row.catalog_release_id || ""),
    selectedCategoryId: String(row.selected_category_id || ""),
    mountRevision: Math.max(0, Number(row.mount_revision) || 0),
    overlayRevision: Math.max(0, Number(row.overlay_revision) || 0),
    kernelState: String(kernel.state || "absent"),
    kernelGeneration: Math.max(0, Number(kernel.generation) || 0),
    activeSlots: activeSlots.length,
    probationSlots: activeSlots.filter(item => {
      if (!item || typeof item !== "object") return false;
      const probation = (item as Record<string, unknown>).probation;
      return !!probation && typeof probation === "object"
        && String((probation as Record<string, unknown>).status || "") === "probation";
    }).length,
    activeChildren: Math.max(0, Number(children.active) || 0),
    queuedInputs: Math.max(0, Number(row.queued_inputs) || 0),
    continuationState: String(row.continuation_state || "ready"),
    warning: String(row.warning || ""),
    mutationEnabled: typeof row.mutation_enabled === "boolean"
      ? row.mutation_enabled
      : false,
    mutationEffectiveEnabled: typeof row.mutation_effective_enabled === "boolean"
      ? row.mutation_effective_enabled
      : Boolean(row.mutation_enabled),
    mutationAuthorityRevision: Math.max(
      0,
      Number(row.mutation_authority_revision) || 0,
    ),
    mutationToggleAvailable: Boolean(row.mutation_toggle_available),
    mutationToggleLocked: Boolean(row.mutation_toggle_locked),
    mutationToggleReason: String(row.mutation_toggle_reason || ""),
  };
}

export function applyRuntimeSnapshot(id: string, value: unknown): void {
  const state = getChatState();
  if (id && state.sessionId && id !== state.sessionId) return;
  const runtime = parseRuntime(value);
  if (!runtime) return;
  if(runtime.inputQueue)applyInputQueue(runtime.inputQueue);
  else refreshInputQueue();
  if (runtimeConflictsWithActiveRun(runtime)) return;
  hydrateTurn(runtime);
  const mutationSettled = Boolean(
    state.mutationTogglePending
    && runtime.mutationEnabled === state.mutationTogglePending.enabled
    && runtime.mutationAuthorityRevision
      > state.mutationTogglePending.baseRevision
  );
  replaceChatState({
    ...getChatState(),
    connected: true,
    runtime,
    pause:runtime.busy===false ? null : getChatState().pause,
    stopPending: runtime.busy === false ? false : getChatState().stopPending,
    mutationTogglePending: mutationSettled
      ? null
      : state.mutationTogglePending,
  });
  restorePauseFromRuntime(runtime);
  emit();
}

function hydrateTurn(runtime: ChatRuntimeState | null): void {
  if (!runtime) return;
  const turn = turnApi(), state = getChatState();
  if (runtime.busy) {
    dismissRecoveryNotice();
    if (!turn.isActive()) turn.begin({sessionId:state.sessionId || "", admissionId:runtime.activeAdmissionId, runId:runtime.activeRunId});
    else turn.bind(runtime.activeAdmissionId, runtime.activeRunId);
  } else if (runtime.busy === false && turn.snapshot().admissionId) turn.end({status:"complete"});
}

type RunSnapshot = {steps: ChatTurnStep[]; segment?: number; text: string};
/** Newest snapshot revision applied per admission; a lower one arriving late is stale. */
const appliedSnapshots = new Map<string, number>();

/** The backend's view of the active run, sent with chat:session while it runs. */
function parseRunSnapshot(value: unknown, runId: string, admissionId: string): RunSnapshot | null {
  if (!value || typeof value !== "object" || !runId) return null;
  const row = value as Record<string, unknown>;
  if (row.run_id !== runId) return null;
  // A resumed run keeps its logical id under a new admission; only the
  // admission this window follows may speak for it.
  const snapshotAdmission = typeof row.admission_id === "string" ? row.admission_id : "";
  if (admissionId && snapshotAdmission !== admissionId) return null;
  const fence = `${runId}:${snapshotAdmission}`;
  const segment = Number.isSafeInteger(row.segment) && (row.segment as number) > 0 ? row.segment as number : undefined;
  // Live results merge on the same key a live start gets.
  const steps = (parseTurnSteps(row.steps, true) || []).map(step => step.callId && !step.key ? {...step, key: `call:${step.callId}`} : step);
  const revision = Number.isSafeInteger(row.revision) ? row.revision as number : 0;
  // The revision moves on bindings and step changes, not as a call's text
  // grows, so an equal revision can still carry newer text for that call.
  // Only a lower one is stale; the merge keeps longer or later text seen here.
  if (revision && revision < (appliedSnapshots.get(fence) ?? 0)) return null;
  if (revision) {
    appliedSnapshots.set(fence, revision);
    while (appliedSnapshots.size > 64) appliedSnapshots.delete(appliedSnapshots.keys().next().value!);
  }
  return {steps, segment, text: typeof row.text === "string" ? row.text : ""};
}

/**
 * Opening a chat mid-run starts from the backend's snapshot. Steps this
 * window already saw keep their place and pick up newer results; narration
 * is one step per model call.
 */
function mergeRunSnapshot(state: ChatState, snapshot: RunSnapshot): Pick<ChatState, "turnSteps" | "streamText" | "streamSegment" | "streaming"> {
  const local = state.turnSteps;
  const identity = (step: ChatTurnStep) => step.kind === "text" && step.segment ? `text:${step.segment}` : step.callId || step.id;
  const remote = new Map(snapshot.steps.map(step => [identity(step), step]));
  const merged = local.map(step => {
    const newer = remote.get(identity(step));
    if (!newer) return step;
    remote.delete(identity(step));
    return step.status === "running" && newer.status !== "running" ? {...step, ...newer, id: step.id} : step;
  });
  const steps = [...merged, ...remote.values()].map((step, order) => ({step, order}))
    .sort((a, b) => (a.step.ts - b.step.ts) || (a.order - b.order)).map(({step}) => step);
  // Text this window already saw for the same or a later call is newer than
  // the snapshot; text the snapshot already saved as narration is not live.
  const seenSegment = state.streamSegment ?? 0, remoteSegment = snapshot.segment ?? 0;
  const keepLocal = seenSegment > remoteSegment || (seenSegment === remoteSegment && state.streamText.length >= snapshot.text.length);
  let streamText = keepLocal ? state.streamText : snapshot.text;
  const streamSegment = keepLocal ? state.streamSegment : snapshot.segment;
  if (streamSegment && steps.some(step => step.kind === "text" && step.segment === streamSegment)) streamText = "";
  return {turnSteps: steps, streamText, streamSegment, streaming: state.streaming || !!streamText};
}

export function applySession(session: Record<string, unknown>, navigation?: ChatNavigationOutcome) {
  const prior = getChatState();
  const id = String(session.id || session.session_id || "") || null;
  if (!acceptIncomingSession(id || "", prior.sessionId, navigation) || !id) return;
  if (id !== prior.sessionId) invalidatePendingChatAttachments();
  activateChatState(id);
  restoreRecoveryNotice();
  let state = getChatState();
  const title = String(session.title || "New chat");
  const messages = parseMessages(session.messages);
  const sessionChanged = prior.sessionId !== id;
  const candidateRuntime = parseRuntime(session.runtime);
  const parsedRuntime = candidateRuntime && !runtimeConflictsWithActiveRun(candidateRuntime) ? candidateRuntime : null;
  hydrateTurn(parsedRuntime);state = getChatState();
  const mutationSettledBySnapshot = Boolean(
    state.mutationTogglePending
    && parsedRuntime
    && parsedRuntime.mutationEnabled === state.mutationTogglePending.enabled
    && parsedRuntime.mutationAuthorityRevision
      > state.mutationTogglePending.baseRevision
  );

  // A selected running chat keeps its cached prefix and optimistic inputs.
  if (sharedTurnActive()) {
    const turnSession = turnApi()?.getSessionId() || state.sessionId || "";
    if (id && turnSession && id !== turnSession) return;
    const acceptPrefix=!candidateRuntime || !runtimeConflictsWithActiveRun(candidateRuntime);
    const delivered=new Set(acceptPrefix?messages.map(row=>row.ticketId).filter((id):id is string=>!!id):[]);
    const snapshot = acceptPrefix ? parseRunSnapshot(session.run_snapshot, turnApi().snapshot().runId || parsedRuntime?.activeRunId || "",
      turnApi().snapshot().admissionId || parsedRuntime?.activeAdmissionId || "") : null;
    replaceChatState({
      ...state,
      ...(snapshot ? {...mergeRunSnapshot(state, snapshot), turnActive: true} : parsedRuntime?.busy ? {turnActive: true} : {}),
      connected: true,
      sessionId: state.sessionId || id,
      title: (!id || id === state.sessionId) ? (title || state.title) : state.title,
      messages: acceptPrefix?reconcileActiveTranscript(state.messages,messages,state.activeTurnId):state.messages,
      pendingActiveInputs:state.pendingActiveInputs.filter(input=>!delivered.has(input.optimisticTurnId)),
      runtime: parsedRuntime || state.runtime,
      mutationTogglePending: mutationSettledBySnapshot
        ? null
        : state.mutationTogglePending,
    });
    noteDisplayedSession(id);
    restorePauseFromRuntime(parsedRuntime);
    if(candidateRuntime?.inputQueue)applyInputQueue(candidateRuntime.inputQueue);else refreshInputQueue();
    refreshComposerGoal();
    refreshAgentTeam();
    emit();
    activateTerminalSession();
    return;
  }

  let nextMessages = messages;
  let recoveredTrace = false;
  const recoveredRun=turnApi().snapshot().runId;
  if(!sessionChanged && !turnApi().isActive() && state.turnSteps.length && recoveredRun) {
    nextMessages=nextMessages.map(message=>{
      if(message.role!=="assistant" || message.runId!==recoveredRun || message.steps?.some(step=>!step.peerMessage))return message;
      recoveredTrace=true;
      return {...message,steps:state.turnSteps};
    });
  }
  if (!sessionChanged) {
    nextMessages = mergeMessageEnrichment(nextMessages, state.messages);
  }

  replaceChatState({
    ...state,
    connected: true,
    sessionId: id,
    title,
    messages: nextMessages,
    turnActive: false,
    stopPending: false,
    pause: null,
    streaming: false,
    streamText: "",
    queuedFollowUps: 0,
    activeTurnId: null,
    turnSteps: [],
    pendingActiveInputs: [],
    mutationTogglePending: sessionChanged || mutationSettledBySnapshot
      ? null
      : state.mutationTogglePending,
    runtime: parsedRuntime,
  });
  setSubtitle("Connected locally", "ready");
  if(recoveredTrace)persistTrace({type:"chat:session:annotate",id,run_id:recoveredRun!,steps:state.turnSteps});
  if(candidateRuntime?.inputQueue)applyInputQueue(candidateRuntime.inputQueue);else refreshInputQueue();
    refreshComposerGoal();
    refreshAgentTeam();
  reconcileContinuedHistory();
  noteDisplayedSession(id);
  emit();
  activateTerminalSession();
}
