import type {InputQueueItem,InputQueueResult,InputQueueSnapshot} from "../protocol/chatQueue";
import type {StreamRouting} from "../protocol/chatEvents";
import {getChatState,getChatContext,patchChatState,sendChat,sharedTurnActive,notifyChat} from "./stateCore";
import {getSessionState} from "../state/sessionStore";
import {getContextForSession} from "../sessionContextStore";

export function queueAdmissionPending():boolean {
  const item=getChatState().inputQueue.continuation;
  return !!item && !item.started && !item.finished && !sharedTurnActive() && !getChatState().turnActive;
}

export function refreshInputQueue():boolean {
  const state=getChatState(),queue=state.inputQueue;
  if(!state.sessionId || queue.refreshRequestId || getChatContext()?.isOpen?.()===false)return false;
  const requestId=`queue-read-${crypto.randomUUID()}`;
  patchChatState({inputQueue:{...queue,refreshRequestId:requestId}});
  if(sendChat({type:"chat:queue:get",session_id:state.sessionId,request_id:requestId}))return true;
  patchChatState({inputQueue:{...getChatState().inputQueue,refreshRequestId:undefined,synced:false,error:"Queue could not be refreshed. Reconnect and refresh."}});return false;
}

export function queueItem(ticketId:string):InputQueueItem|undefined {
  const queue=getChatState().inputQueue;
  return queue.snapshot?.items.find(item=>item.ticket_id===ticketId) || queue.recent.find(item=>item.ticket_id===ticketId)
    || (queue.action?.ticket.ticket_id===ticketId?queue.action.ticket:undefined)
    || (queue.continuation?.ticket.ticket_id===ticketId?queue.continuation.ticket:undefined);
}

export function applyInputQueue(snapshot:InputQueueSnapshot):boolean {
  const state=getChatState(),queue=state.inputQueue,previous=queue.snapshot;
  if(snapshot.session_id!==state.sessionId || snapshot.revision<(previous?.revision ?? -1))return false;
  const current=previous && snapshot.revision===previous.revision ? previous : snapshot;
  const ids=new Set(current.items.map(item=>item.ticket_id));
  const known=new Set([...ids,...(previous?.items.map(item=>item.ticket_id) || [])]);
  const recent=[...(previous?.items.filter(item=>!ids.has(item.ticket_id)) || []),...queue.recent].filter((item,index,items)=>!ids.has(item.ticket_id)&&items.findIndex(other=>other.ticket_id===item.ticket_id)===index).slice(0,128);
  const refreshed=!!snapshot.request_id && snapshot.request_id===queue.refreshRequestId;
  const uncertain=queue.action?.uncertain || !queue.synced;
  // Snapshots own undelivered tickets. Keep them in the queue, not as duplicate
  // transcript rows or draft text. Actual delivery/Continue creates its row.
  const messages=state.messages.filter(message=>{
    const id=message.ticketId || message.optimisticTurnId || "";
    const continuing=message.optimisticTurnId===queue.continuation?.groupId;
    if(current.items.some(item=>item.ticket_id===id && item.state==="parked") && message.optimisticTurnId
      && (!continuing || current.revision>queue.continuation!.baseRevision))return false;
    return !known.has(id) || message.activeInputState==="delivered" || message.optimisticTurnId===queue.continuation?.groupId;
  });
  patchChatState({inputQueue:{...queue,snapshot:current,recent,synced:true,refreshRequestId:refreshed?undefined:queue.refreshRequestId,
      action:refreshed&&uncertain?undefined:queue.action,error:refreshed?undefined:queue.error,
      continuation:queue.continuation?.started && current.revision>queue.continuation.baseRevision && current.items.some(item=>item.ticket_id===queue.continuation!.ticket.ticket_id && item.state==="parked")
        ? {...queue.continuation,finished:true} : queue.continuation},messages,
    pendingActiveInputs:state.pendingActiveInputs.filter(item=>!ids.has(item.optimisticTurnId))});
  // After uncertain delivery, a fresh parked snapshot permits an explicit
  // retry. Never automatically resubmit the old Continue/Remove command.
  if(refreshed && uncertain && queue.continuation && !queue.continuation.started
    && current.items.some(item=>item.ticket_id===queue.continuation!.ticket.ticket_id && item.state==="parked")) {
    discardContinuation(queue.continuation.requestId);
  }
  return true;
}

function discardContinuation(requestId:string) {
  const state=getChatState(),continuation=state.inputQueue.continuation;
  if(!continuation || continuation.requestId!==requestId || continuation.started)return;
  patchChatState({inputQueue:{...state.inputQueue,continuation:undefined},
    activeTurnId:state.activeTurnId===continuation.groupId?null:state.activeTurnId,
    messages:state.messages.filter(message=>message.optimisticTurnId!==continuation.groupId)});
}

export function mutateInputQueue(operation:"continue"|"remove",ticketId:string,expectedSessionId:string|null):boolean {
  const state=getChatState(),queue=state.inputQueue,snapshot=queue.snapshot;
  const ticket=snapshot?.items.find(item=>item.ticket_id===ticketId);
  if(!state.sessionId || state.sessionId!==expectedSessionId || !state.connected || getChatContext()?.isOpen?.()===false
    || !queue.synced || !snapshot || !ticket || queue.action || queueAdmissionPending() || getSessionState().pendingAction)return false;
  if(operation==="continue" ? sharedTurnActive()||state.turnActive||state.stopPending||ticket.state!=="parked"||getContextForSession(state.sessionId).settingsPending
    : !["queued","resume_queued","parked"].includes(ticket.state))return false;
  const requestId=`queue-${operation}-${crypto.randomUUID()}`;
  patchChatState({inputQueue:{...queue,error:undefined,action:{operation,requestId,ticket,revision:snapshot.revision},
    continuation:operation==="continue"?{ticket,requestId,groupId:`continue-${requestId}`,baseRevision:snapshot.revision,started:false,finished:false}:queue.continuation}});
  const ok=sendChat({type:operation==="continue"?"chat:queue:continue":"chat:queue:remove",session_id:state.sessionId,ticket_id:ticketId,expected_revision:snapshot.revision,request_id:requestId});
  if(!ok){discardContinuation(requestId);patchChatState({inputQueue:{...getChatState().inputQueue,action:undefined,error:"Queue action was not sent. Reconnect and try again."}});}
  return ok;
}

function ensureContinuationPrompt() {
  const state=getChatState(),item=state.inputQueue.continuation;
  if(!item || item.finished)return;
  if(state.messages.some(message=>message.role==="user" && message.ticketId===item.ticket.ticket_id
    && (!message.optimisticTurnId || message.optimisticTurnId===item.groupId)))return;
  const messages=state.messages.filter(message=>message.ticketId!==item.ticket.ticket_id);
  messages.push({role:"user",text:item.ticket.text,ticketId:item.ticket.ticket_id,localId:item.groupId,
    optimisticTurnId:item.groupId,optimisticOwnsComposer:false,activeInputAccepted:true,
    activeInputState:item.started?"delivered":"queued",delivery:item.ticket.delivery});
  patchChatState({messages,activeTurnId:item.groupId});
}

export function ingestInputQueueResult(result:InputQueueResult):void {
  const state=getChatState();if(result.session_id!==state.sessionId)return;
  const action=state.inputQueue.action;
  const matchingRead=result.operation==="get" && state.inputQueue.refreshRequestId===result.request_id;
  const validSnapshot=!!result.queue && result.queue.session_id===result.session_id;
  if(validSnapshot)applyInputQueue(result.queue!);
  if(result.operation==="get") {
    if(matchingRead)patchChatState({inputQueue:{...getChatState().inputQueue,refreshRequestId:undefined,
      synced:validSnapshot && getChatState().inputQueue.synced,error:result.error || "Queue could not be refreshed."}});
    return;
  }
  if(!action || action.requestId!==result.request_id || action.operation!==result.operation)return;
  const current=getChatState();
  patchChatState({inputQueue:{...current.inputQueue,action:undefined,error:result.accepted?undefined:result.error || "Queue action was rejected."}});
  if(!validSnapshot)patchChatState({inputQueue:{...getChatState().inputQueue,synced:false}});
  if(!result.accepted){discardContinuation(result.request_id);notifyChat(result.error || "Queue action was rejected.");return;}
  // Only the correlated ordinary start creates the continued prompt. A late
  // accepted result must not resurrect a turn that has already finished.
}

export function isQueueContinueStart(message:StreamRouting):boolean {
  return message.source==="queue_continue" && !!message.ticket_id && !!(message.request_id || message.client_id) && !!message.admission_id;
}

export function canAdoptQueueStart(message:StreamRouting):boolean {
  const state=getChatState(),queue=state.inputQueue,requestId=message.request_id || message.client_id;
  if(!isQueueContinueStart(message) || state.sessionId!==message.session_id)return false;
  if(queue.continuation && queue.continuation.requestId===requestId && queue.continuation.ticket.ticket_id===message.ticket_id)return !queue.continuation.finished;
  return !!queue.snapshot?.items.some(item=>item.ticket_id===message.ticket_id && ["selected","preparing"].includes(item.state));
}

export function startQueueContinuation(message:StreamRouting):void {
  if(!isQueueContinueStart(message) || message.session_id!==getChatState().sessionId)return;
  const state=getChatState(),queue=state.inputQueue,requestId=message.request_id || message.client_id!;
  if(queue.continuation && (queue.continuation.ticket.ticket_id!==message.ticket_id || queue.continuation.requestId!==requestId))return;
  const ticket=queueItem(message.ticket_id!);if(!ticket)return;
  patchChatState({inputQueue:{...queue,continuation:{ticket,requestId,groupId:queue.continuation?.groupId || `continue-${requestId}`,baseRevision:queue.continuation?.baseRevision ?? queue.snapshot?.revision ?? -1,started:true,finished:false}}});
  ensureContinuationPrompt();
}

export function finishQueueContinuation(message?:StreamRouting):void {
  const state=getChatState(),continuation=state.inputQueue.continuation;
  const matched=message?.source==="queue_continue" && message.ticket_id===continuation?.ticket.ticket_id
    && (message.request_id || message.client_id)===continuation?.requestId;
  if(continuation && (continuation.started || matched))patchChatState({inputQueue:{...state.inputQueue,continuation:{...continuation,finished:true}}});
}

export function markQueuedPromptDelivered(ticketId:string,item?:InputQueueItem):void {
  const state=getChatState();
  if(state.inputQueue.snapshot?.items.some(row=>row.ticket_id===ticketId))return;
  if(state.messages.some(message=>message.role==="user" && message.ticketId===ticketId)){
    patchChatState({messages:state.messages.map(message=>message.role==="user"&&message.ticketId===ticketId?{...message,activeInputAccepted:true,activeInputState:"delivered"}:message),
      pendingActiveInputs:state.pendingActiveInputs.filter(item=>item.optimisticTurnId!==ticketId)});return;
  }
  if(!item)return;
  patchChatState({messages:[...state.messages,{role:"user",text:item.text,ticketId,optimisticTurnId:ticketId,activeInputAccepted:true,activeInputState:"delivered",delivery:item.delivery}]});
}

export function reconcileContinuedHistory():void {
  const state=getChatState(),continuation=state.inputQueue.continuation;
  if(continuation && !sharedTurnActive() && state.messages.some(message=>message.role==="user" && message.ticketId===continuation.ticket.ticket_id && !message.optimisticTurnId)) {
    patchChatState({inputQueue:{...state.inputQueue,continuation:{...continuation,finished:true}}});
  }
}
