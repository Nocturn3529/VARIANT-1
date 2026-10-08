export type InputQueueItem = Readonly<{
  ticket_id:string; chat_id:string; text:string; delivery:"steer"|"follow_up";
  state:"queued"|"resume_queued"|"parked"|"selected"|"preparing";
  client_id:string; source:string; created_at?:number|string; updated_at?:number|string;
  /** Set when another agent sent this input. */
  origin?:InputQueueOrigin;
}>;
export type InputQueueOrigin = Readonly<{kind:"peer"; peer_id:string; message_id:string; display_name:string; content?:string}>;
/** A delivered ticket is a receipt, independent of the mutable queue revision. */
export type DeliveredInput = Omit<InputQueueItem,"state"> & Readonly<{
  state:"running"; run_id:string; admission_id:string; delivered_at:number;
}>;
export function parseDeliveredInput(value:unknown,sessionId:string,ticketId:string):DeliveredInput|undefined {
  if(!value || typeof value!=="object")return;
  const row=value as Record<string,unknown>;
  if(row.state!=="running" || row.chat_id!==sessionId || row.ticket_id!==ticketId
    || typeof row.run_id!=="string" || !row.run_id || typeof row.admission_id!=="string" || !row.admission_id
    || typeof row.delivered_at!=="number" || !Number.isFinite(row.delivered_at) || row.delivered_at<=0)return;
  const queue=parseInputQueueSnapshot({type:"chat:queue_snapshot",schema:"variant1.input-queue.v1",session_id:sessionId,
    revision:0,items:[{...row,state:"preparing"}]});
  if(!queue)return;
  return {...queue.items[0],state:"running",run_id:row.run_id,admission_id:row.admission_id,delivered_at:row.delivered_at};
}
function parseOrigin(value:unknown):InputQueueOrigin|undefined {
  if(!value || typeof value!=="object")return undefined;
  const row=value as Record<string,unknown>;
  if(row.kind!=="peer" || typeof row.peer_id!=="string" || !row.peer_id || typeof row.message_id!=="string" || !row.message_id)return undefined;
  return {kind:"peer",peer_id:row.peer_id,message_id:row.message_id,display_name:typeof row.display_name==="string"?row.display_name:"",
    ...(typeof row.content==="string" ? {content:row.content} : {})};
}
export type InputQueueSnapshot = Readonly<{
  type:"chat:queue_snapshot"; schema:"variant1.input-queue.v1"; session_id:string;
  revision:number; items:readonly InputQueueItem[]; request_id?:string;
}>;
export type InputQueueResult = Readonly<{
  type:"chat:queue_result"; operation:"continue"|"remove"|"get"; session_id:string;
  request_id:string; accepted:boolean; error?:string; queue:InputQueueSnapshot|null;
}>;

export function parseInputQueueSnapshot(value:unknown):InputQueueSnapshot|null {
  if (!value || typeof value!=="object") return null;
  const row=value as Record<string,unknown>;
  if(row.type!=="chat:queue_snapshot" || row.schema!=="variant1.input-queue.v1" || typeof row.session_id!=="string" || !row.session_id
    || typeof row.revision!=="number" || !Number.isSafeInteger(row.revision) || row.revision<0 || !Array.isArray(row.items)) return null;
  const ids=new Set<string>(),items:InputQueueItem[]=[];
  for(const value of row.items) {
    if(!value || typeof value!=="object")return null;
    const item=value as Record<string,unknown>;
    if(typeof item.ticket_id!=="string" || !item.ticket_id || ids.has(item.ticket_id) || item.chat_id!==row.session_id || typeof item.text!=="string"
      || !["steer","follow_up"].includes(String(item.delivery)) || !["queued","resume_queued","parked","selected","preparing"].includes(String(item.state)))return null;
    ids.add(item.ticket_id);
    items.push({ticket_id:item.ticket_id,chat_id:row.session_id,text:item.text,delivery:item.delivery as InputQueueItem["delivery"],state:item.state as InputQueueItem["state"],
      client_id:typeof item.client_id==="string"?item.client_id:"",source:typeof item.source==="string"?item.source:"",
      created_at:typeof item.created_at==="number"||typeof item.created_at==="string"?item.created_at:undefined,
      updated_at:typeof item.updated_at==="number"||typeof item.updated_at==="string"?item.updated_at:undefined,
      ...(parseOrigin(item.origin) ? {origin:parseOrigin(item.origin)} : {})});
  }
  return {type:"chat:queue_snapshot",schema:"variant1.input-queue.v1",session_id:row.session_id,revision:row.revision,items,
    request_id:typeof row.request_id==="string"?row.request_id:undefined};
}
