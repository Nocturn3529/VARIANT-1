import type {GoalMessage,ComposerGoalSnapshot,GoalControl} from "../protocol/goals";
import {goalIsTerminal,parseComposerGoalSnapshot} from "../protocol/goals";
import {getChatState,getChatContext,getComposerRevision,patchChatState,sendChat,notifyChat,sharedTurnActive} from "./stateCore";
import {getSessionState} from "../state/sessionStore";
import {getContextForSession} from "../sessionContextStore";

export function submitComposerGoal(objective:string):boolean {
  const state=getChatState();
  if (!objective) {notifyChat("Add an objective after /goal. Your draft is kept.");return false;}
  if (!state.sessionId || getChatContext()?.isOpen?.()===false || getSessionState().pendingAction) return false;
  if (state.attachments.length || state.attachmentsPreparing) {notifyChat("Send attachments separately before starting a goal. Your draft and files are kept.");return false;}
  if (state.turnActive || sharedTurnActive() || state.stopPending) {notifyChat("Wait for this chat turn to finish before starting a goal.");return false;}
  if (state.goal.pending || (state.goal.snapshot && !goalIsTerminal(state.goal.snapshot.goal))) {
    notifyChat("This chat already has an active or pending goal. Your draft is kept.");return false;
  }
  if(getContextForSession(state.sessionId).settingsPending)return false;
  const requestId=`goal-submit-${crypto.randomUUID()}`;
  patchChatState({goal:{...state.goal,error:undefined,refreshRequestId:undefined,refreshAgain:false,pending:{operation:"submit",requestId,draft:state.draft,draftRevision:getComposerRevision()}}});
  if(sendChat({type:"goal:submit",session_id:state.sessionId,request_id:requestId,objective}))return true;
  patchChatState({goal:{...getChatState().goal,pending:undefined,error:"Could not submit the goal. Your draft is kept."}});return false;
}

export function refreshComposerGoal(manual=false):boolean {
  const state=getChatState();
  if(!state.sessionId || (!manual && state.goal.refreshRequestId) || getChatContext()?.isOpen?.()===false)return false;
  const requestId=`goal-read-${crypto.randomUUID()}`;
  const pending=manual && state.goal.pending?{...state.goal.pending,uncertain:true}:state.goal.pending;
  patchChatState({goal:{...state.goal,pending,refreshRequestId:requestId}});
  if(sendChat({type:"goal:current:get",session_id:state.sessionId,request_id:requestId,
    ...(pending?.operation==="submit"?{submission_request_id:pending.requestId}:{})}))return true;
  patchChatState({goal:{...getChatState().goal,refreshRequestId:undefined,synced:false,error:"Could not refresh the goal. Reconnect and try again."}});return false;
}

export const MAX_GOAL_GUIDANCE=8000;
export function setGoalGuidance(text:string,sessionId:string|null,goalId:string,expectedVersion:number):void {
  const state=getChatState(),goal=state.goal;
  if(state.sessionId!==sessionId || goal.snapshot?.goal.goal_id!==goalId || goal.snapshot.goal.version!==expectedVersion || goal.pending || getSessionState().pendingAction)return;
  patchChatState({goal:{...goal,guidance:{goalId,text:text.slice(0,MAX_GOAL_GUIDANCE),revision:(goal.guidance?.revision || 0)+1}}});
}

export function requestGoalControl(operation:GoalControl,sessionId:string|null,goalId:string,expectedVersion?:number):boolean {
  const state=getChatState(),goal=state.goal,snapshot=goal.snapshot;
  if(!sessionId || state.sessionId!==sessionId || !snapshot || snapshot.goal.goal_id!==goalId
    || !state.connected || getChatContext()?.isOpen?.()===false || !goal.synced || goal.pending || getSessionState().pendingAction
    || !(operation==="pause"?snapshot.capabilities.pause_scheduling:operation==="cancel"?(snapshot.capabilities.cancel || snapshot.capabilities.retry_cleanup):snapshot.capabilities[operation]))return false;
  if(expectedVersion!==undefined && expectedVersion!==snapshot.goal.version)return false;
  if(operation==="resume" && snapshot.goal.status!=="paused")return false;
  const guidance=operation==="continue" && goal.guidance?.goalId===goalId?goal.guidance:undefined;
  const message=guidance?.text.trim() || "";
  const requestId=`goal-${operation}-${crypto.randomUUID()}`;
  patchChatState({goal:{...goal,error:undefined,refreshRequestId:undefined,refreshAgain:false,pending:{operation,requestId,goalId,
    ...(message?{guidanceRevision:guidance!.revision,guidanceText:guidance!.text}:{})}}});
  if(sendChat({type:`goal:${operation}`,session_id:sessionId,goal_id:goalId,request_id:requestId,expected_version:snapshot.goal.version,...(message?{message}:{})}))return true;
  patchChatState({goal:{...getChatState().goal,pending:undefined,error:"Could not send the goal control. Refresh before trying again.",synced:false}});return false;
}

function applySnapshot(snapshot:ComposerGoalSnapshot):boolean {
  const state=getChatState(),previous=state.goal.snapshot;
  if(snapshot.goal.owner_chat_id!==state.sessionId)return false;
  if(previous?.goal.goal_id===snapshot.goal.goal_id && snapshot.goal.version<previous.goal.version)return false;
  patchChatState({goal:{...state.goal,snapshot,synced:true,error:undefined,
    pending:state.goal.pending?.operation==="continue" && state.goal.pending.goalId!==snapshot.goal.goal_id?undefined:state.goal.pending,
    guidance:state.goal.guidance?.goalId===snapshot.goal.goal_id?state.goal.guidance:undefined}});return true;
}

function settleSubmission(snapshot:ComposerGoalSnapshot):void {
  const state=getChatState(),pending=state.goal.pending;
  if(pending?.operation!=="submit" || snapshot.submission_request_id!==pending.requestId)return;
  // An acknowledgment may arrive after the user has edited or sent a new draft.
  const ownsDraft=pending.draftRevision===getComposerRevision() && pending.draft===state.draft;
  patchChatState({...(ownsDraft?{draft:""}:{}),goal:{...state.goal,pending:undefined,error:undefined}});
}

export function ingestComposerGoal(message:GoalMessage):void {
  const state=getChatState(),current=state.goal,pending=current.pending;
  if(message.session_id!==state.sessionId)return;
  const isRead=!!current.refreshRequestId && message.request_id===current.refreshRequestId;
  const isAction=!!pending && pending.requestId===message.request_id && pending.operation===message.operation;
  if(!isRead && !isAction)return;
  if(message.type==="goal:rejected") {
    patchChatState({goal:{...current,refreshRequestId:isRead?undefined:current.refreshRequestId,
      pending:isAction?undefined:pending,error:message.error || "The goal request was rejected.",synced:false}});
    return;
  }
  if(message.type==="goal:current" && isRead && message.result===null) {
    // A lost submit acknowledgment cannot be resolved by matching objective text.
    patchChatState({goal:{...current,refreshRequestId:undefined,
      snapshot:pending?current.snapshot:null,guidance:pending?current.guidance:undefined,synced:!pending,
      error:pending?"The goal request is still unconfirmed. Refresh to check; it has not been resent.":undefined}});
    if(current.refreshAgain){patchChatState({goal:{...getChatState().goal,refreshAgain:false}});refreshComposerGoal();}return;
  }
  const snapshot=parseComposerGoalSnapshot(message.result);
  if(!snapshot || snapshot.goal.owner_chat_id!==state.sessionId
    || (pending?.operation==="submit" && snapshot.submission_request_id!==pending.requestId)
    || (isAction && pending?.goalId && pending.goalId!==snapshot.goal.goal_id)) {
    patchChatState({goal:{...current,refreshRequestId:isRead?undefined:current.refreshRequestId,
      pending:pending?{...pending,uncertain:true}:undefined,synced:false,error:"The goal response could not be verified. Refresh to check its saved state."}});return;
  }
  applySnapshot(snapshot);
  // Any in-flight read predating a settled mutation may contain pre-mutation null.
  if(isAction)patchChatState({goal:{...getChatState().goal,refreshRequestId:undefined}});
  if(isRead)patchChatState({goal:{...getChatState().goal,refreshRequestId:undefined}});
  settleSubmission(snapshot);
  // Broad execution progress and staged intent cannot establish committed admission.
  const admission=snapshot.admittedContinuation;
  const admissionConfirmed=isRead && message.type==="goal:current" && pending?.operation==="continue"
    && snapshot.goal.goal_id===pending.goalId && !!admission && admission.requestId===pending.requestId
    && admission.message===(pending.guidanceText?.trim() || "");
  const guidanceConfirmed=!pending?.guidanceText || isAction || admissionConfirmed;
  if(pending && pending.operation!=="submit" && (isAction || (isRead && (pending.uncertain || admissionConfirmed) && snapshot.goal.goal_id===pending.goalId && guidanceConfirmed))) {
    const latest=getChatState().goal,guidance=latest.guidance;
    const retire=!!guidance && pending.operation==="continue" && pending.guidanceRevision!==undefined && guidance.goalId===pending.goalId
      && guidance.revision===pending.guidanceRevision && guidance.text===pending.guidanceText;
    patchChatState({goal:{...latest,pending:undefined,error:undefined,...(retire?{guidance:undefined}:{})}});
  }
  if(snapshot.goal.status==="archived")patchChatState({goal:{...getChatState().goal,snapshot:null,guidance:undefined}});
  if((isRead && getChatState().goal.refreshAgain) || (isAction && !!current.refreshRequestId)) {
    patchChatState({goal:{...getChatState().goal,refreshAgain:false}});refreshComposerGoal();
  }
}

export function invalidateComposerGoal():void {
  const goal=getChatState().goal;
  if(goal.refreshRequestId)patchChatState({goal:{...goal,refreshAgain:true}});
  else refreshComposerGoal();
}
