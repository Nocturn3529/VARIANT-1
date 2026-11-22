/** Read-only reconciliation for missed stream/result frames. */
import {cachedChatStates,getChatContext,getChatState,getDisplayedChatState,notifyChat,patchChatState,sendChat,turnApi,withCachedChatState} from "./stateCore";
import {parseInputQueueSnapshot} from "../protocol/chatQueue";
import {applyInputQueue} from "./inputQueue";
import {normalizeActivityStatus} from "./activityModel";
import {confirmInitialAdmission,rejectOptimisticTurn} from "./composer";

const pending = new Map<string,{requestId:string;runId:string;admissionId:string;sentAt:number}>();
let timer:ReturnType<typeof setInterval>|null=null;

/** Only the admission lookup or an exact terminal receipt can retire the optimistic submission. */
export function reconcileInitialSubmission(status:unknown, runId:string, admissionId:string, delivery:unknown="initial"):void {
  const state=getChatState(), initial=state.initialSubmission, id=state.sessionId;
  if(!id || !initial)return;
  if(status==="admitted" && admissionId) {
    if(!runId) {
      // Reservation precedes run binding. Keep its ticket until the start,
      // a later lookup with both IDs, or an explicit terminal outcome.
      patchChatState({initialSubmission:{...initial,uncertain:true,checkFailed:false,readId:undefined}});
      return;
    }
    if(!turnApi().isActive())turnApi().begin({sessionId:id,clientId:state.clientId});
    turnApi().bind(admissionId,runId);
    confirmInitialAdmission();
    if(delivery==="steer" || delivery==="follow_up") {
      // A concurrent turn accepted this submission at an input boundary.
      patchChatState({activeTurnId:null,messages:getChatState().messages.filter(row=>row.optimisticTurnId!==initial.id)});
    }
    patchChatState({turnActive:true,streaming:true});
  } else if(status==="absent" || status==="rejected") {
    turnApi().end({status:"not_admitted"});
    const detail=state.goal.composing ? "Your message was not admitted. Its draft will return when you leave Goal mode." :
      status==="absent" ? "Your message was not admitted. Review the restored draft and send when ready."
      : "Delivery was rejected. Your draft is back in the composer.";
    rejectOptimisticTurn(detail,status==="absent");
    notifyChat(detail);
  } else if(status==="queued" || status==="settled") {
    turnApi().end({status});
    if(status==="settled")confirmInitialAdmission();
    patchChatState({initialSubmission:undefined,activeTurnId:null,turnActive:false,streaming:false,pause:null,
      pendingTurnCommit:undefined,streamText:"",streamSegment:undefined,
      messages:status==="queued" ? state.messages.filter(row=>row.optimisticTurnId!==initial.id) : getChatState().messages});
  } else {
    patchChatState({initialSubmission:{...initial,uncertain:true,readId:undefined,checkFailed:true}});
    return;
  }
  sendChat({type:id===getDisplayedChatState().sessionId ? "chat:session:get" : "chat:runtime:get",id});
}

export function checkInitialSubmission(retry=false):void {
  const state=getChatState(), initial=state.initialSubmission;
  if(!state.sessionId || !initial?.uncertain || getChatContext()?.isOpen?.()===false)return;
  if(initial.readId) {
    if(Date.now()-(initial.readAt || 0)>=15_000)reconcileInitialSubmission("unknown","","");
    return;
  }
  if(initial.checkFailed && !retry)return;
  const readId=crypto.randomUUID();
  patchChatState({initialSubmission:{...initial,checkFailed:false,readId,readAt:Date.now()}});
  if(!sendChat({type:"chat:execution:get",session_id:state.sessionId,request_id:readId,ticket_id:initial.id})) {
    reconcileInitialSubmission("unknown","","");
  }
}

export function refreshExecutionStates(): void {
  const chats=new Map([getChatState(),...cachedChatStates()].map(chat=>[chat.sessionId,chat]));
  for(const id of pending.keys())if(!chats.has(id))pending.delete(id);
  for(const chat of chats.values()) if(chat.sessionId) withCachedChatState(chat.sessionId,()=>{
    const turn=turnApi().snapshot();
    const initial=getChatState().initialSubmission;
    if(initial) {
      if(!initial.uncertain && turn.startedAt && Date.now()-turn.startedAt>=15_000) {
        patchChatState({initialSubmission:{...initial,uncertain:true}});
      }
      checkInitialSubmission();
      return;
    }
    if(!turn.active || !turn.runId || !turn.admissionId){pending.delete(chat.sessionId!);return;}
    const prior=pending.get(chat.sessionId!);
    if(prior && Date.now()-prior.sentAt<10_000)return;
    const requestId=`execution-${crypto.randomUUID()}`;
    pending.set(chat.sessionId!,{requestId,runId:turn.runId,admissionId:turn.admissionId,sentAt:Date.now()});
    if(!sendChat({type:"chat:execution:get",session_id:chat.sessionId!,request_id:requestId,
      run_id:turn.runId,admission_id:turn.admissionId,
      call_ids:getChatState().turnSteps.map(step=>step.callId || "").filter(Boolean).slice(-256)}))pending.delete(chat.sessionId!);
  });
}

export function executionConnection(connected:boolean): void {
  if(timer)clearInterval(timer);
  timer=null;pending.clear();
  if(!connected)return;
  refreshExecutionStates();
  timer=setInterval(refreshExecutionStates,5_000);
  // Node-based UI regressions must not be kept alive by a browser poll.
  (timer as unknown as {unref?:()=>void}).unref?.();
}

/** A terminal run is not proof that every tool succeeded. */
export function settleMissedTurn(status:string): void {
  const state=getChatState();
  patchChatState({turnSteps:state.turnSteps.map(step=>step.status==="running" ? {
    ...step,status:"interrupted",rawStatus:"result_unobserved",
    detail:[step.detail,"The turn settled without an observed result for this step."].filter(Boolean).join("\n"),
  } : step),turnActive:false,streaming:false,stopPending:false,pause:null});
  turnApi().end({status});
}

export function ingestExecutionState(message:Record<string,unknown>): void {
  const id=String(message.session_id || ""),request=pending.get(id);
  if(!id)return;
  withCachedChatState(id,()=>{
    const initial=getChatState().initialSubmission;
    if(!initial || typeof message.request_id!=="string" || !message.request_id) return;
    const lookup=message.request_id===initial.readId;
    if(!lookup && message.request_id!==initial.requestId)return;
    if(lookup && message.error && (!message.ticket_id || message.ticket_id===initial.id)) {reconcileInitialSubmission("unknown","","");return;}
    const row=message.submission as Record<string,unknown>|null;
    if(message.ticket_id!==initial.id || !row || row.ticket_id!==initial.id)return;
    const queue=parseInputQueueSnapshot(message.queue);
    if(queue)applyInputQueue(queue);
    reconcileInitialSubmission(row.status,typeof row.run_id==="string"?row.run_id:"",
      typeof row.admission_id==="string"?row.admission_id:"",row.delivery);
    if(!getChatState().initialSubmission && !turnApi().isActive() && message.busy===true &&
      typeof message.active_admission_id==="string" && message.active_admission_id &&
      typeof message.active_run_id==="string" && message.active_run_id) {
      turnApi().begin({sessionId:id,admissionId:message.active_admission_id,runId:message.active_run_id});
      patchChatState({turnActive:true});
    }
  });
  if(!request || message.request_id!==request.requestId)return;
  pending.delete(id);
  if(message.error || message.observed_run_id!==request.runId || message.observed_admission_id!==request.admissionId
    || typeof message.busy!=="boolean")return;
  const busy=message.busy;
  withCachedChatState(id,()=>{
    const turn=turnApi().snapshot();
    if(!turn.active || turn.runId!==request.runId || turn.admissionId!==request.admissionId)return;
    const rows=Array.isArray(message.calls)?message.calls as Record<string,unknown>[]:[];
    const calls=new Map(rows.filter(row=>row.state!=="dispatched").map(row=>[String(row.call_id),row]));
    patchChatState({turnSteps:getChatState().turnSteps.map(step=>{
      const call=step.callId?calls.get(step.callId):undefined;
      if(!call || step.status!=="running")return step;
      const raw=String(call.status || call.state),completedAt=Number(call.updated_at)*1000;
      return {...step,status:normalizeActivityStatus(raw,"tool:result"),rawStatus:raw,
        completedAt:Number.isFinite(completedAt)?completedAt:undefined,
        durationMs:typeof call.duration_ms==="number"?call.duration_ms:step.durationMs};
    })});
    const queue=parseInputQueueSnapshot(message.queue);if(queue)applyInputQueue(queue);
    const nextAdmission=String(message.active_admission_id || ""),nextRun=String(message.active_run_id || "");
    if(busy && nextAdmission===turn.admissionId && nextRun===turn.runId)return;
    const settlement=message.settlement && typeof message.settlement==='object'
      ? message.settlement as Record<string,unknown> : null;
    const receipt=settlement?.receipt && typeof settlement.receipt==='object'
      ? settlement.receipt as Record<string,unknown> : null;
    const exactSettlement=settlement?.chat_id===id && settlement?.run_id===request.runId
      && settlement?.admission_id===request.admissionId && receipt?.run_id===request.runId && receipt?.settled===true;
    const terminal=String(receipt?.status || '');
    settleMissedTurn(exactSettlement && ['ok','error','failed','cancelled','interrupted'].includes(terminal) ? terminal : 'interrupted');
    const runtime=getChatState().runtime;
    patchChatState({runtime:runtime?{...runtime,busy,activeAdmissionId:nextAdmission,activeRunId:nextRun}:runtime});
    if(busy && nextAdmission) {
      turnApi().begin({sessionId:id,admissionId:nextAdmission,runId:nextRun});
      patchChatState({turnActive:true});
    }
    // A snapshot provides the exact committed reply, rather than fabricating
    // a successful completion from the partial stream cached by the UI.
    sendChat({type:"chat:session:get",id});
  });
}
