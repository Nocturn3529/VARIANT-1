export type PeerMessageKind="request"|"notice"|"result";
export type PeerEndpoint=Readonly<{
  peer_id:string;kind:"variant_chat"|"external_harness";display_name:string;status:string;revision:number;
  chat_id?:string;terminal_id?:string;external_session_id?:string;
  project:unknown;model_route:unknown;capabilities:Record<string,unknown>;delivery_mode:string;
}>;
export type PeerMessage=Readonly<{
  message_id:string;exchange_id:string;sender_peer_id:string;target_peer_id:string;in_reply_to:string;
  content:string;delivery:string;state:string;request_id:string;sequence:number;revision:number;
  created_at:unknown;updated_at:unknown;replied_at:unknown;error:unknown;evidence:unknown;
  delivery_ticket_id:string;target_run_id:string;
  sender_display_name?:string;target_display_name?:string;
  message_kind?:PeerMessageKind;
}>;
export const record=(value:unknown):Record<string,unknown>=>value && typeof value==="object" && !Array.isArray(value) ? value as Record<string,unknown> : {};
const version=(value:unknown)=>typeof value==="number" && Number.isSafeInteger(value) && value>=0 ? value : 0;
export function parsePeer(value:unknown):PeerEndpoint|null {
  const row=record(value);
  if(typeof row.peer_id!=="string" || !row.peer_id || !["variant_chat","external_harness"].includes(String(row.kind)))return null;
  return {peer_id:row.peer_id,kind:row.kind as PeerEndpoint["kind"],display_name:String(row.display_name || row.peer_id),status:String(row.status || "unknown"),revision:version(row.revision),
    ...(typeof row.chat_id==="string" ? {chat_id:row.chat_id} : {}),...(typeof row.terminal_id==="string" ? {terminal_id:row.terminal_id} : {}),
    ...(typeof row.external_session_id==="string" ? {external_session_id:row.external_session_id} : {}),
    project:row.project,model_route:row.model_route,capabilities:record(row.capabilities),delivery_mode:typeof record(row.metadata).delivery_mode==="string" ? String(record(row.metadata).delivery_mode) : ""};
}
export function parsePeerMessage(value:unknown):PeerMessage|null {
  const row=record(value);
  for(const key of ["message_id","exchange_id","sender_peer_id","target_peer_id"])if(typeof row[key]!=="string" || !row[key])return null;
  return {message_id:String(row.message_id),exchange_id:String(row.exchange_id),sender_peer_id:String(row.sender_peer_id),target_peer_id:String(row.target_peer_id),
    in_reply_to:String(row.in_reply_to || ""),content:typeof row.content==="string" ? row.content : "",delivery:String(row.delivery || ""),state:String(row.state || "unknown"),
    request_id:String(row.request_id || ""),sequence:version(row.sequence),revision:version(row.revision),created_at:row.created_at,updated_at:row.updated_at,replied_at:row.replied_at,
    error:row.error,evidence:row.evidence,delivery_ticket_id:String(row.delivery_ticket_id || ""),target_run_id:String(row.target_run_id || ""),sender_display_name:typeof row.sender_display_name==="string" ? row.sender_display_name : undefined,target_display_name:typeof row.target_display_name==="string" ? row.target_display_name : undefined,message_kind:["request","notice","result"].includes(String(row.message_kind)) ? row.message_kind as PeerMessageKind : undefined};
}
export const PEER_DELIVERY:Record<string,{label:string;meaning:string}>={
  persisted:{label:"Saved",meaning:"VARIANT-1 saved this message."},
  queued:{label:"Queued",meaning:"The recipient or adapter accepted responsibility for delivery."},
  transport_written:{label:"Transport written",meaning:"The transport was written. Model receipt is not yet proven."},
  observed:{label:"Observed",meaning:"A supported receipt confirms inclusion in a recipient model step."},
  replied:{label:"Reply received",meaning:"An explicit correlated reply arrived. This does not verify the work."},
  parked:{label:"Parked",meaning:"Delivery is retained without starting more work."},
  failed:{label:"Failed",meaning:"The service recorded a delivery failure."},
  unknown:{label:"Delivery unknown",meaning:"The available evidence does not establish the external effect."},
};
