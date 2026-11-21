import type {ChatGoal, ChatGoalState, GoalAction, GoalDraft, GoalMessage, GoalReplacement, GoalUpdatedMessage, PendingGoal} from "../protocol/goals";
import {goalCanEdit, goalCanResume, goalLength, goalUnfinished, MAX_GOAL_OBJECTIVE} from "../protocol/goals";
import {getChatState, getChatContext, getComposerRevision, patchChatState, sendChat, notifyChat, withCachedChatState} from "./stateCore";
import {getSessionState} from "../state/sessionStore";

const patchGoal = (partial: Partial<ChatGoalState>) => patchChatState({goal: {...getChatState().goal, ...partial}});
const replyTimers = new Map<string, number>();
const readFinished = {readRequestId: undefined, readForRequestId: undefined, readStale: false};
const targetChanged = "The current goal changed. Review it and choose Set goal again.";

function stopWaiting(id?: string): void {
  if (!id) return;
  window.clearTimeout(replyTimers.get(id));
  replyTimers.delete(id);
}

function awaitReply(sessionId: string, id: string, timeout: () => void): void {
  replyTimers.set(id, window.setTimeout(() => {
    replyTimers.delete(id);
    withCachedChatState(sessionId, timeout);
  }, 15000));
}

export function setGoalComposing(on: boolean, sessionId = getChatState().sessionId): void {
  const state = getChatState();
  if (state.sessionId !== sessionId || !!state.goal.composing === on) return;
  patchGoal({composing: on, ...(on ? {} : {confirmReplace: undefined})});
}

function beginWrite(sessionId: string, pending: PendingGoal): void {
  // A read sent before this write cannot be its receipt.
  stopWaiting(getChatState().goal.readRequestId);
  patchGoal({...readFinished, pending, error: undefined, readError: undefined, confirmReplace: undefined, confirmedRequestId: undefined});
  awaitReply(sessionId, pending.requestId, () => {
    if (getChatState().goal.pending?.requestId === pending.requestId) checkUnconfirmedWrite();
  });
}

function sendSubmission(submission: GoalDraft, expectedGoalId?: string): boolean {
  const state = getChatState();
  if (state.sessionId !== submission.sessionId || !state.connected || state.goal.pending ||
      getSessionState().pendingAction || getChatContext()?.isOpen?.() === false) return false;
  if (state.attachments.length || state.attachmentsPreparing || state.references.length) {
    notifyChat("Send files and tab mentions in a separate message. Your goal draft is kept.");
    return false;
  }
  if (!expectedGoalId && goalUnfinished(state.goal.goal)) {
    patchGoal({confirmReplace: {...submission, expectedGoalId: state.goal.goal.goal_id}, error: undefined});
    return false;
  }
  const requestId = "goal-submit-" + crypto.randomUUID();
  beginWrite(submission.sessionId, {requestId, operation: "submit", submission, expectedGoalId});
  const command = {type: "goal:submit", session_id: submission.sessionId, request_id: requestId, objective: submission.objective} as const;
  if (sendChat(expectedGoalId ? {...command, replace: true, expected_goal_id: expectedGoalId} : command)) return true;
  stopWaiting(requestId);
  patchGoal({pending: undefined, error: "Could not set the goal. Your draft is kept."});
  return false;
}

export function submitComposerGoal(objective: string): boolean {
  const state = getChatState();
  if (!objective) { notifyChat("Describe the goal first. Your draft is kept."); return false; }
  if (goalLength(objective) > MAX_GOAL_OBJECTIVE) { notifyChat("A goal is limited to 32,000 characters."); return false; }
  if (!state.sessionId || state.goal.pending || state.goal.confirmReplace) return false;
  const submission = {sessionId: state.sessionId, objective, draft: state.draft, draftRevision: getComposerRevision()};
  return sendSubmission(submission);
}

export function answerGoalReplace(held: GoalReplacement, replace: boolean): boolean {
  const state = getChatState();
  if (state.goal.confirmReplace !== held || held.sessionId !== state.sessionId) return false;
  if (!replace) { patchGoal({confirmReplace: undefined}); return false; }
  if (!state.goal.synced || !held.expectedGoalId) return false;
  if (state.goal.goal?.goal_id !== held.expectedGoalId || !goalUnfinished(state.goal.goal)) {
    patchGoal({confirmReplace: undefined, error: targetChanged});
    return false;
  }
  return sendSubmission(held, held.expectedGoalId);
}

export function requestGoalControl(action: GoalAction, goal: ChatGoal, objective?: string): string | false {
  const state = getChatState(), current = state.goal;
  if (goal.session_id !== state.sessionId || current.goal?.goal_id !== goal.goal_id || current.goal.version !== goal.version ||
      !current.synced || !state.connected || current.pending || getSessionState().pendingAction || getChatContext()?.isOpen?.() === false) return false;
  if (action === "resume" && !goalCanResume(goal) || action === "pause" && goal.status !== "active" || action === "edit" && !goalCanEdit(goal)) return false;
  const text = objective?.trim();
  if (action === "edit" && (!text || goalLength(text) > MAX_GOAL_OBJECTIVE || text === goal.objective)) return false;
  const requestId = "goal-" + action + "-" + crypto.randomUUID();
  beginWrite(goal.session_id, {requestId, operation: action, goalId: goal.goal_id, version: goal.version, objective: text});
  if (sendChat({type: "goal:control", session_id: goal.session_id, request_id: requestId, goal_id: goal.goal_id, action,
    expected_version: goal.version, ...(action === "edit" ? {objective: text} : {})})) return requestId;
  stopWaiting(requestId);
  patchGoal({pending: undefined, error: "Could not reach VARIANT-1. Try again when it reconnects."});
  return false;
}

export function refreshComposerGoal(): boolean {
  const {sessionId, goal} = getChatState();
  if (!sessionId || getChatContext()?.isOpen?.() === false) return false;
  stopWaiting(goal.readRequestId);
  const requestId = "goal-read-" + crypto.randomUUID();
  patchGoal({readRequestId: requestId, readStale: false, readError: undefined,
    readForRequestId: goal.pending?.uncertain ? goal.pending.requestId : undefined});
  awaitReply(sessionId, requestId, () => {
    if (getChatState().goal.readRequestId === requestId)
      patchGoal({...readFinished, synced: false, readError: "The saved goal check timed out."});
  });
  if (sendChat({type: "goal:current:get", session_id: sessionId, request_id: requestId})) return true;
  stopWaiting(requestId);
  patchGoal({...readFinished, synced: false, readError: "Could not check the saved goal."});
  return false;
}

function checkUnconfirmedWrite(): void {
  const {pending} = getChatState().goal;
  if (!pending) return;
  stopWaiting(pending.requestId);
  patchGoal({pending: {...pending, uncertain: true}, synced: false});
  refreshComposerGoal();
}

function applyGoal(goal: ChatGoal | null): void {
  const current = getChatState().goal;
  if (goal && current.goal?.goal_id === goal.goal_id && goal.version < current.goal.version) return;
  const held = current.confirmReplace;
  let confirmation: Partial<ChatGoalState> = {};
  if (held) {
    if (!goalUnfinished(goal) || (held.expectedGoalId && goal.goal_id !== held.expectedGoalId)) {
      confirmation = {confirmReplace: undefined, error: targetChanged};
    } else if (!held.expectedGoalId) {
      confirmation = {confirmReplace: {...held, expectedGoalId: goal.goal_id}};
    }
  }
  patchGoal({goal, synced: true, readError: undefined, ...confirmation});
}

function showsWrite(pending: PendingGoal, goal: ChatGoal | null): boolean {
  if (pending.operation === "submit") return goal?.request_id === pending.requestId;
  if (pending.operation === "clear") return goal === null;
  if (!goal || goal.goal_id !== pending.goalId || goal.version <= pending.version) return false;
  if (pending.operation === "edit") return goal.objective === pending.objective;
  return goal.status === (pending.operation === "pause" ? "paused" : "active");
}

function confirmWrite(pending: PendingGoal): void {
  stopWaiting(pending.requestId);
  const state = getChatState();
  const spent = pending.operation === "submit" && pending.submission.draftRevision === getComposerRevision() &&
    pending.submission.draft === state.draft;
  patchChatState({...(spent ? {draft: ""} : {}), goal: {...state.goal, pending: undefined, error: undefined,
    confirmedRequestId: pending.requestId, ...(spent ? {composing: false} : {})}});
}

function receiveRead(message: GoalMessage): void {
  const current = getChatState().goal;
  if (message.operation !== "current" || message.request_id !== current.readRequestId ||
      (message.type !== "goal:current" && message.type !== "goal:rejected")) return;
  stopWaiting(message.request_id);
  patchGoal(readFinished);
  if (message.type === "goal:rejected" || message.goal === undefined) {
    patchGoal({synced: false, readError: message.error || "The saved goal could not be read."});
    return;
  }
  // A push received after this read began wins across a clear or replacement.
  if (!current.readStale || (message.goal && message.goal.goal_id === current.goal?.goal_id)) {
    applyGoal(message.goal);
    if (current.pending && !current.pending.observedUpdate && current.goal?.goal_id !== message.goal?.goal_id)
      patchGoal({pending: {...current.pending, observedUpdate: true}});
  }
  const pending = current.pending;
  if (!pending || current.readForRequestId !== pending.requestId) return;
  if (showsWrite(pending, getChatState().goal.goal)) confirmWrite(pending);
  else patchGoal({pending: undefined, error: pending.operation === "submit" ?
    "The goal may not have been set. Your draft is kept." : "Could not confirm the change. Review the saved goal before trying again."});
}

export function ingestComposerGoal(message: GoalMessage | GoalUpdatedMessage): void {
  const state = getChatState(), current = state.goal;
  if (message.session_id !== state.sessionId) return;
  const pending = current.pending;
  if (message.type === "goal:updated") {
    applyGoal(message.goal);
    if (current.readRequestId) patchGoal({readStale: true});
    if (pending?.operation === "submit" && showsWrite(pending, message.goal)) confirmWrite(pending);
    else if (pending && !pending.observedUpdate) patchGoal({pending: {...pending, observedUpdate: true}});
    return;
  }
  if (message.operation === "current") { receiveRead(message); return; }
  if (!pending || pending.requestId !== message.request_id || message.type === "goal:current" ||
      message.operation !== (pending.operation === "submit" ? "submit" : "control")) return;
  if (message.type === "goal:rejected") {
    stopWaiting(pending.requestId);
    const conflict = ["GoalConflict", "GoalNotFound", "GoalVersionConflict"].includes(message.reason_code);
    const needsTarget = message.reason_code === "GoalConflict" && pending.operation === "submit" && !pending.expectedGoalId;
    patchGoal({pending: undefined, synced: conflict ? false : current.synced,
      error: needsTarget ? undefined : message.error || "VARIANT-1 could not change the goal.",
      ...(needsTarget ? {confirmReplace: pending.submission} : {})});
    if (conflict) refreshComposerGoal();
    return;
  }
  if (message.goal === undefined || !showsWrite(pending, message.goal)) { checkUnconfirmedWrite(); return; }
  const newerIdentity = pending.observedUpdate && current.goal?.goal_id !== message.goal?.goal_id;
  if (!newerIdentity) applyGoal(message.goal);
  if (current.readRequestId) patchGoal({readStale: true});
  confirmWrite(pending);
  if (newerIdentity) refreshComposerGoal();
}

export function disconnectComposerGoal(): void {
  const {pending, readRequestId} = getChatState().goal;
  stopWaiting(pending?.requestId);
  stopWaiting(readRequestId);
  patchGoal({...readFinished, synced: false, pending: pending ? {...pending, uncertain: true} : undefined});
}
