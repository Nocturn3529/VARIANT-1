export type PeerActivity={message_id:string;sender_peer_id:string;target_peer_id:string;target_display_name:string;content:string;state:string;sender_invocation?:Record<string,string>};
export function parsePeerActivity(value:unknown):PeerActivity|undefined {
  if(!value || typeof value!=="object")return;
  const row=value as Record<string,unknown>;
  if(!["message_id","sender_peer_id","target_peer_id"].every(key=>typeof row[key]==="string" && row[key]) || typeof row.content!=="string")return;
  const invocation=row.sender_invocation && typeof row.sender_invocation==="object" ? Object.fromEntries(Object.entries(row.sender_invocation).filter(([key,value])=>["chat_id","run_id","outer_tool_call_id","cell_execution_id","nested_call_id"].includes(key)&&typeof value==="string")) as Record<string,string> : undefined;
  return {message_id:String(row.message_id),sender_peer_id:String(row.sender_peer_id),target_peer_id:String(row.target_peer_id),target_display_name:typeof row.target_display_name==="string" ? row.target_display_name : "Peer agent",content:row.content,state:typeof row.state==="string" ? row.state : "unknown",sender_invocation:invocation};
}
