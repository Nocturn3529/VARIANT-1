export type InputQueueItem = Readonly<{
  ticket_id:string; chat_id:string; text:string; delivery:"steer"|"follow_up";
  state:"queued"|"resume_queued"|"parked"|"selected"|"preparing";
  client_id:string; source:string; created_at?:number|string; updated_at?:number|string;
}>;
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
      updated_at:typeof item.updated_at==="number"||typeof item.updated_at==="string"?item.updated_at:undefined});
  }
  return {type:"chat:queue_snapshot",schema:"variant1.input-queue.v1",session_id:row.session_id,revision:row.revision,items,
    request_id:typeof row.request_id==="string"?row.request_id:undefined};
}
