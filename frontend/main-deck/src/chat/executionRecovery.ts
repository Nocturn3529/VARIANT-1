/** Read-only reconciliation for missed stream/result frames. */
import {cachedChatStates,getChatState,patchChatState,sendChat,turnApi,withCachedChatState} from "./stateCore";
import {parseInputQueueSnapshot} from "../protocol/chatQueue";
import {applyInputQueue} from "./inputQueue";
import {normalizeActivityStatus} from "./activityModel";

const pending = new Map<string,{requestId:string;runId:string;admissionId:string;sentAt:number}>();
let timer:ReturnType<typeof setInterval>|null=null;

export function refreshExecutionStates(): void {
  const chats=new Map([getChatState(),...cachedChatStates()].map(chat=>[chat.sessionId,chat]));
  for(const id of pending.keys())if(!chats.has(id))pending.delete(id);
  for(const chat of chats.values()) if(chat.sessionId) withCachedChatState(chat.sessionId,()=>{
    const turn=turnApi().snapshot();
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
    settleMissedTurn("interrupted");
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
