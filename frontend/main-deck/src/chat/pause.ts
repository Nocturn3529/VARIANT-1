import type {ChatPauseStateMessage} from "../protocol/chatEvents";
import type {ChatRuntimeState} from "./types";
import {getChatContext,getChatState,notifyChat,patchChatState,sendChat,turnApi} from "./stateCore";

function owner() {
  const state=getChatState(),turn=turnApi().snapshot();
  return {sessionId:state.sessionId || "",active:turn.active && (!turn.sessionId || turn.sessionId===state.sessionId),
    admissionId:turn.admissionId || state.runtime?.activeAdmissionId || "",runId:turn.runId || state.runtime?.activeRunId || ""};
}

export function currentPause() {
  const state=getChatState(),active=owner(),pause=state.pause;
  if (!pause || !active.active || (active.admissionId && pause.admissionId!==active.admissionId) || (active.runId && pause.runId!==active.runId)) return null;
  return pause;
}

export function requestChatPause(action:"pause"|"resume",expectedSessionId:string|null,expectedAdmissionId?:string): boolean {
  const state=getChatState(),active=owner(),pause=currentPause();
  if (!active.active || !active.sessionId || state.sessionId!==expectedSessionId
    || (expectedAdmissionId && active.admissionId!==expectedAdmissionId) || (!active.admissionId && !active.runId)
    || !state.connected || getChatContext()?.isOpen?.()===false || state.stopPending || pause?.pending || pause?.synced===false) return false;
  if (action==="resume" ? pause?.state!=="paused" : pause?.state==="pausing" || pause?.state==="paused" || pause?.state==="idle") return false;
  const requestId=`pause-${crypto.randomUUID()}`;
  const previous=pause || {admissionId:active.admissionId,runId:active.runId,state:"running" as const,revision:-1,synced:true};
  patchChatState({pause:{...previous,pending:{requestId,action,baseRevision:previous.revision}}});
  const sent=sendChat({type:action==="pause"?"chat:pause":"chat:resume",session_id:active.sessionId,
    ...(active.admissionId?{admission_id:active.admissionId}:{}),...(active.runId?{run_id:active.runId}:{}),request_id:requestId});
  if (!sent) {
    if (getChatState().pause?.pending?.requestId===requestId) patchChatState({pause});
    notifyChat("Could not send the request. Reconnect and try again.");
  }
  return sent;
}

export function ingestPauseState(message:ChatPauseStateMessage,snapshot=false): boolean {
  const active=owner(),pause=currentPause();
  if (!active.active || message.session_id!==active.sessionId || !message.state || message.pause_revision<0
    || (!active.admissionId && !active.runId)
    || (active.admissionId && message.admission_id!==active.admissionId)
    || (active.runId && message.run_id!==active.runId)) return false;
  const pending=pause?.pending;
  if (message.pause_revision<(pause?.revision ?? -1)) return false;
  if (!message.accepted) {
    if (!pending || message.request_id!==pending.requestId) return false;
    patchChatState({pause:{...pause!,pending:undefined}});
    notifyChat(message.error || "The task control request was rejected.");return true;
  }
  // Equal revisions may confirm the same state, never change its meaning.
  if (message.pause_revision===pause?.revision && message.state!==pause.state) return false;
  const settled=!!pending && (message.request_id===pending.requestId || message.pause_revision>pending.baseRevision);
  patchChatState({pause:{admissionId:message.admission_id,runId:message.run_id,state:message.state,
    revision:message.pause_revision,synced:true,pending:settled || (snapshot && pause?.synced===false) ? undefined : pending}});
  return true;
}

export function restorePauseFromRuntime(runtime:ChatRuntimeState|null): void {
  if (!runtime || runtime.pauseState===undefined || runtime.pauseRevision===undefined) return;
  ingestPauseState({type:"chat:pause_state",session_id:getChatState().sessionId || "",
    admission_id:runtime.activeAdmissionId || "",run_id:runtime.activeRunId || "",pause_revision:runtime.pauseRevision,
    state:runtime.pauseState,accepted:true},true);
}

export function runtimeConflictsWithActiveRun(runtime:ChatRuntimeState): boolean {
  const active=owner(),pause=getChatState().pause;
  // A reconnect snapshot may discover a new admission after missed lifecycle
  // events. Once synchronized, an older owner cannot rebind this active run.
  if (active.active && runtime.busy===false
    && ((active.admissionId && runtime.activeAdmissionId!==active.admissionId)
      || (active.runId && runtime.activeRunId!==active.runId))) return true;
  if (pause?.synced===false) return false;
  return active.active && runtime.busy===true && !!(
    (active.admissionId && runtime.activeAdmissionId && active.admissionId!==runtime.activeAdmissionId)
    || (active.runId && runtime.activeRunId && active.runId!==runtime.activeRunId));
}
