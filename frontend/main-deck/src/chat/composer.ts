/**
 * Composer: draft, optimistic send, active-turn delivery, cancel, refresh.
 */
import type {ChatSendCommand} from "../protocol";
import {getSessionState} from "../state/sessionStore";
import type {ChatAttachment, ChatMessage, PendingActiveInput} from "./types";
import {
  beginSharedTurn,
  endSharedTurn,
  emit,
  getChatState,
  getChatContext,
  getComposerRevision,
  notifyChat,
  patchChatState,
  replaceChatState,
  sendChat,
  setSubtitle,
  sharedTurnActive,
  turnApi,
} from "./stateCore";
import {invalidatePendingChatAttachments} from "./attachments";
import {stopSpeech} from "./speech";
import {getContextForSession} from "../sessionContextStore";
import {queueAdmissionPending} from "./inputQueue";
import {parseGoalCommand} from "./goalCommand";
import {submitComposerGoal} from "./goals";
import {dismissRecoveryNotice} from "./recovery";

function newOptimisticTurnId(): string {
  return `turn-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

function newMutationRequestId(): string {
  return `mutation-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 9)}`;
}

function attachmentOnlyDisplayText(items: ChatMessage["attachments"]): string {
  const attachments = items || [];
  if (attachments.length === 1) {
    const item = attachments[0];
    return item.kind === "image" ? `📷 ${item.name}` : `Attached ${item.name}`;
  }
  const names = attachments.slice(0, 3).map(item => item.name || "file").join(", ");
  const more = attachments.length > 3 ? ` +${attachments.length - 3}` : "";
  return `Attached ${attachments.length} files: ${names}${more}`;
}

/** Request one correlated, between-turn mutation write-authority change. */
export function setMutationWriteEnabled(enabled: boolean, expectedSessionId = getChatState().sessionId): boolean {
  const state = getChatState();
  if (!state.sessionId || state.sessionId!==expectedSessionId || state.mutationTogglePending || getSessionState().pendingAction) return false;
  if(getContextForSession(state.sessionId).settingsPending)return false;
  const requestId = newMutationRequestId();
  const baseRevision = Math.max(0, state.runtime?.mutationAuthorityRevision || 0);
  patchChatState({mutationTogglePending: {
    requestId,
    enabled: Boolean(enabled),
    baseRevision,
  }});
  const ok = sendChat({
    type: "chat:runtime:mutation:set",
    id: state.sessionId,
    enabled: Boolean(enabled),
    request_id: requestId,
    expected_revision: baseRevision,
  });
  if (!ok) {
    patchChatState({mutationTogglePending: null});
    notifyChat("Could not update mutation authority — still connecting");
    return false;
  }
  notifyChat(`Turning mutation authoring ${enabled ? "on" : "off"} for this chat`);
  return true;
}

export function setChatDraft(value: string, expectedSessionId = getChatState().sessionId) {
  const state = getChatState();
  if(state.sessionId!==expectedSessionId)return;
  if (state.draft === value) return;
  patchChatState({draft: value});
}

export function setChatDelivery(deliveryMode: "steer" | "follow_up", expectedSessionId = getChatState().sessionId) {
  if(getChatState().sessionId!==expectedSessionId)return;
  patchChatState({deliveryMode});
}

function pendingInputIndex(
  pending: PendingActiveInput[],
  ticketId = "",
): number {
  const exact = ticketId
    ? pending.findIndex(item => item.optimisticTurnId === ticketId)
    : -1;
  // A supplied ticket must match exactly; it cannot acknowledge another input.
  return ticketId ? exact : (pending.length ? 0 : -1);
}

function restoreRejectedText(current: string, rejected: string): string {
  if (!current) return rejected;
  if (!rejected || current === rejected) return current;
  return `${rejected}\n\n${current}`;
}

/** Settle the admission receipt without removing the optimistic bubble. */
export function acknowledgeActiveInput(ticketId = ""): PendingActiveInput | null {
  const state = getChatState();
  const index = pendingInputIndex(state.pendingActiveInputs, ticketId);
  if (index < 0) return null;
  const item = state.pendingActiveInputs[index];
  replaceChatState({
    ...state,
    messages: state.messages.map(message => (
      message.localId === item.localId && ticketId
        ? {
            ...message,
            ticketId,
            activeInputAccepted: true,
            activeInputState: "queued" as const,
          }
        : message
    )),
    pendingActiveInputs: state.pendingActiveInputs.filter((_, i) => i !== index),
  });
  emit();
  return item;
}

/** Mark the exact durable input as delivered at a safe agent boundary. */
export function markActiveInputDelivered(ticketId: string): boolean {
  const id = String(ticketId || "");
  if (!id) return false;
  const state = getChatState();
  let changed = false;
  const messages = state.messages.map(message => {
    if (
      message.ticketId !== id
      && message.optimisticTurnId !== id
    ) return message;
    changed = true;
    return {
      ...message,
      activeInputAccepted: true,
      activeInputState: "delivered" as const,
    };
  });
  if (!changed) return false;
  patchChatState({messages});
  emit();
  return true;
}

/** Roll back the exact active-input bubble rejected by the backend. */
export function rejectActiveInput(ticketId = ""): PendingActiveInput | null {
  const state = getChatState();
  const index = pendingInputIndex(state.pendingActiveInputs, ticketId);
  if (index < 0) return null;
  const item = state.pendingActiveInputs[index];
  replaceChatState({
    ...state,
    messages: state.messages.filter(message => message.localId !== item.localId),
    draft: restoreRejectedText(state.draft, item.text),
    pendingActiveInputs: state.pendingActiveInputs.filter((_, i) => i !== index),
  });
  emit();
  return item;
}

/** Remove accepted/unaccepted active-input bubbles cancelled at turn teardown. */
export function settleActiveInputTickets(ticketIds: readonly string[]): number {
  const ids = new Set(ticketIds.filter(Boolean));
  if (!ids.size) return 0;
  const state = getChatState();
  const rejectedMessages = state.messages.filter(message => (
    (message.ticketId && ids.has(message.ticketId))
    || (message.optimisticTurnId && ids.has(message.optimisticTurnId))
  ));
  const rejectedPending = state.pendingActiveInputs.filter(item => (
    ids.has(item.optimisticTurnId)
  ));
  const pendingOnly = rejectedPending.filter(
    item => !rejectedMessages.some(message => message.localId === item.localId),
  );
  const texts = [
    ...rejectedMessages.filter(message => message.role === "user").map(message => message.text),
    ...pendingOnly.map(item => item.text),
  ];
  if (!rejectedMessages.length && !rejectedPending.length) return 0;
  replaceChatState({
    ...state,
    messages: state.messages.filter(message => !(
      (message.ticketId && ids.has(message.ticketId))
      || (message.optimisticTurnId && ids.has(message.optimisticTurnId))
    )),
    draft: restoreRejectedText(state.draft, texts.join("\n\n")),
    pendingActiveInputs: state.pendingActiveInputs.filter(
      item => !ids.has(item.optimisticTurnId),
    ),
  });
  emit();
  return rejectedMessages.length + pendingOnly.length;
}

/** Admission failed before a turn started; restore the initial composer input. */
export function rejectOptimisticTurn(error: string): boolean {
  const state = getChatState();
  const turnId = state.activeTurnId;
  if (!turnId) return false;
  const optimistic = state.messages.filter(
    message => message.optimisticTurnId === turnId,
  );
  const user = optimistic.find(message => message.role === "user");
  const retryableAttachments = (
    user?.optimisticAttachmentRetry || user?.attachments || []
  ).filter(item => (
    !!item.path || !!item.data || item.text != null
  ));
  const rejectedIds = new Set([
    turnId,
    ...state.pendingActiveInputs.map(item => item.optimisticTurnId),
  ]);
  replaceChatState({
    ...state,
    messages: state.messages.filter(
      message => !message.optimisticTurnId
        || !rejectedIds.has(message.optimisticTurnId),
    ),
    draft: restoreRejectedText(state.draft,[user?.optimisticDraft ?? user?.text ?? "",...state.pendingActiveInputs.map(item=>item.text)].filter(Boolean).join("\n\n")),
    attachments: state.attachments.length
      ? state.attachments
      : retryableAttachments,
    turnActive: false,
    stopPending: false,
    pause: null,
    streaming: false,
    streamText: "",
    turnSteps: [],
    queuedFollowUps: 0,
    activeTurnId: null,
    pendingActiveInputs: [],
    lastError: error,
  });
  emit();
  return true;
}

export type UserInputBundle = {
  source: "composer"; text: string; sessionId: string | null; revision: number; attachments: ChatAttachment[];
} | {source: "voice"; text: string; sessionId: string};

export function sendUserMessage(text: string, delivery: "steer" | "follow_up" = "steer"): boolean {
  const state = getChatState();
  return submitUserInput({source: "composer", text, sessionId: state.sessionId,
    revision: getComposerRevision(), attachments: state.attachments}, delivery);
}

/** Only a matching composer bundle can consume its draft and attachments. */
export function submitUserInput(input: UserInputBundle, delivery: "steer" | "follow_up" = "steer"): boolean {
  const state = getChatState();
  const ownsComposer = input.source === "composer";
  if(queueAdmissionPending()){notifyChat("Wait for the selected queued message to start. Your draft is kept.");return false;}
  if (!state.sessionId) { notifyChat("The chat is still loading");return false; }
  if (input.sessionId !== state.sessionId || (input.source === "composer" && input.revision !== getComposerRevision())) return false;
  if (getChatContext()?.isOpen?.() === false) {
    notifyChat("Reconnect to send. Your draft is kept.");
    return false;
  }
  if (state.stopPending || state.attachmentsPreparing > 0) {
    notifyChat(state.stopPending ? "Wait for this task to stop before sending." : "Files are still preparing. Your draft is kept.");
    return false;
  }
  if(getContextForSession(state.sessionId).settingsPending) {
    notifyChat("Waiting for the model setting to be confirmed. Your draft is kept.");return false;
  }
  if (getSessionState().pendingAction) {
    notifyChat("Finishing the chat change — try again in a moment");
    return false;
  }
  const value = String(input.text || "").trim();
  const goalCommand=ownsComposer ? parseGoalCommand(value) : null;
  if(goalCommand)return submitComposerGoal(goalCommand.objective);
  const pending = input.source === "composer" ? input.attachments : [];
  if (!value && !pending.length) return false;
  // The backend owns the active-turn queues. Send steering/follow-up input
  // immediately instead of holding a second client-side work queue.
  if (sharedTurnActive() || state.turnActive) {
    if (pending.length) {
      notifyChat("Wait for VARIANT-1 to finish before attaching files");
      return false;
    }
    if (!value) return false;
    const now = Date.now() / 1000;
    // Each active input owns its own optimistic group/ticket. The initial
    // turn id remains reserved for its user + final assistant pair.
    const turnId = newOptimisticTurnId();
    const localId = `active-input-${now}-${Math.random().toString(36).slice(2, 7)}`;
    const queuedMessage: ChatMessage = {
      role: "user",
      text: value,
      ts: now,
      localId,
      optimisticTurnId: turnId,
      ticketId: turnId,
      activeInputAccepted: false,
      optimisticOwnsComposer: ownsComposer,
      delivery,
    };
    const pendingInput: PendingActiveInput = {
      ownsComposer,
      optimisticTurnId: turnId,
      localId,
      text: value,
      delivery,
    };
    replaceChatState({
      ...state,
      messages: [...state.messages, queuedMessage],
      draft: ownsComposer ? "" : state.draft,
      pendingActiveInputs: [...state.pendingActiveInputs, pendingInput],
    });
    emit();
    const ok = sendChat({
      type: "chat",
      text: value,
      client_id: state.clientId,
      session_id: state.sessionId,
      ...(turnApi().snapshot().admissionId ? {admission_id:turnApi().snapshot().admissionId} : {}),
      ...(turnApi().snapshot().runId ? {run_id:turnApi().snapshot().runId} : {}),
      delivery,
      ticket_id: turnId,
    });
    if (!ok) {
      const current = getChatState();
      replaceChatState({
        ...current,
        messages: current.messages.filter(item => item.localId !== queuedMessage.localId),
        draft: ownsComposer ? restoreRejectedText(current.draft, value) : current.draft,
        pendingActiveInputs: current.pendingActiveInputs.filter(
          item => item.optimisticTurnId !== turnId,
        ),
      });
      setSubtitle("Backend offline — reconnecting", "offline");
      emit();
    }
    return ok;
  }
  const now = Date.now() / 1000;
  // Keep attachment-only optimistic text byte-for-byte aligned with the
  // backend's durable transcript label. Attachment metadata remains a second
  // reconciliation key for older/mixed backend versions.
  const displayText = value || attachmentOnlyDisplayText(pending);
  if (ownsComposer) invalidatePendingChatAttachments();
  // Transcript bubbles retain display metadata only. Base64/text/path payloads
  // belong to this single send call and must not sit in renderer state for the
  // whole turn (or indefinitely if an append acknowledgement is lost).
  const bubbleAtts = pending.map(a => ({
    id: a.id,
    name: a.name,
    kind: a.kind,
    mime: a.mime,
    size: a.size,
    previewUrl: a.previewUrl,
  }));
  const turnId = newOptimisticTurnId();
  const userMsg: ChatMessage = {
    role: "user",
    text: displayText,
    ts: now,
    localId: `u-${now}`,
    optimisticTurnId: turnId,
    optimisticDraft: value,
    optimisticOwnsComposer: ownsComposer,
    optimisticAttachmentRetry: pending.map(item => ({...item})),
    attachments: bubbleAtts.length ? bubbleAtts : undefined,
  };
  const wireAttachments = pending.map(a => ({
    name: a.name,
    kind: a.kind,
    mime: a.mime,
    ...(a.data ? {data: a.data} : {}),
    ...(a.text != null ? {text: a.text} : {}),
    ...(a.path ? {path: a.path} : {}),
  }));
  replaceChatState({
    ...state,
    messages: [...state.messages, userMsg],
    draft: ownsComposer ? "" : state.draft,
    attachments: ownsComposer ? [] : state.attachments,
    turnActive: true,
    stopPending: false,
    pause: null,
    streaming: true,
    streamText: "",
    lastError: "",
    activeTurnId: turnId,
    pendingActiveInputs: [],
  });
  setSubtitle("VARIANT-1 is thinking", "working");
  beginSharedTurn({
    clientId: getChatState().clientId,
    source: "chat",
    sessionId: getChatState().sessionId,
  });
  // Fresh STEPS strip for this send.
  patchChatState({turnSteps: []});
  const afterBegin = getChatState();
  // patchChatState already emitted; ensure subtitle is visible.
  emit();
  const payload: ChatSendCommand = {
    type: "chat",
    text: value,
    client_id: afterBegin.clientId,
    session_id: input.sessionId!,
  };
  if (wireAttachments.length) payload.attachments = wireAttachments;
  const ok = sendChat(payload);
  if (!ok) {
    endSharedTurn("error");
    const cur = getChatState();
    // Restore draft attachments for retry.
    replaceChatState({
      ...cur,
      turnActive: false,
      streaming: false,
      streamText: "",
      attachments: ownsComposer && !cur.attachments.length ? pending : cur.attachments,
      messages: cur.messages.filter(m => m.localId !== userMsg.localId),
      draft: ownsComposer ? restoreRejectedText(cur.draft, value) : cur.draft,
      activeTurnId: null,
      pendingActiveInputs: [],
    });
    setSubtitle("Backend offline — reconnecting", "offline");
    emit();
  }
  return ok;
}

export function resumeOrphanedTask(): boolean {
  const state = getChatState();
  if (!state.connected || !state.sessionId || !state.orphanedTask || state.turnActive) return false;
  if(String(state.orphanedTask.session_id || state.orphanedTask.chat_id || "")!==state.sessionId || getSessionState().pendingAction)return false;
  const ok = sendChat({
    type: "chat",
    text: "",
    client_id: state.clientId,
    session_id: state.sessionId,
    resume: true,
  });
  if (ok) {
    dismissRecoveryNotice();
    patchChatState({orphanedTask: null, lastError: ""});
    setSubtitle("Resuming interrupted task…", "working");
  }
  return ok;
}

export function cancelChatTurn(expectedSessionId?: string | null, expectedAdmissionId?: string) {
  stopSpeech();
  if (!sharedTurnActive() || getChatState().stopPending) return;
  const owner = turnApi().snapshot(), id = getChatState().sessionId;
  if (expectedSessionId !== undefined && expectedSessionId !== id) return;
  if (expectedAdmissionId && expectedAdmissionId !== owner.admissionId) return;
  if (!id || (owner.sessionId && owner.sessionId !== id)) return;
  if (!sendChat({type: "cancel", session_id:id,
    ...(owner.admissionId ? {admission_id:owner.admissionId} : {}), ...(owner.runId ? {run_id:owner.runId} : {})})) {
    setSubtitle("Backend offline — reconnecting", "offline");
    emit();
    return;
  }
  patchChatState({stopPending:true});
  setSubtitle("Stopping task…", "working");
  emit();
}

export function refreshChat() {
  const state = getChatState();
  if (getSessionState().pendingAction) return;
  // Prefer the current session; fall back to active session from backend.
  if (state.sessionId) {
    sendChat({type: "chat:session:get", id: state.sessionId});
  } else {
    sendChat({type: "chat:session:get"});
  }
  sendChat({type: "chat:sessions"});
}
