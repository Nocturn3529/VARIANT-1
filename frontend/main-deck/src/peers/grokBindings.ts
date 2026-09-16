export type GrokBinding=Readonly<{
  binding_id:string;request_id:string;viewer_chat_id:string;owner_chat_id:string;terminal_chat_id:string;peer_id:string;
  session_id:string;terminal_id:string;status:string;error:unknown;
  delivery_mode:string;session_activity:string;
  preferred_delivery_mode:"inbox"|"agent_context_prompt";
  automatic_wake_available:boolean;native_agent_origin:boolean;
  capabilities:Record<string,unknown>;pending_permissions:readonly unknown[];
}>;
export function parseGrokBinding(value:unknown,chatId:string):GrokBinding|null {
  if(!value || typeof value!=="object" || Array.isArray(value))return null;
  const row=value as Record<string,unknown>;
  if(row.viewer_chat_id!==chatId || typeof row.binding_id!=="string" || !row.binding_id)return null;
  return {binding_id:row.binding_id,request_id:String(row.request_id || ""),viewer_chat_id:chatId,owner_chat_id:typeof row.owner_chat_id==="string" ? row.owner_chat_id : "",
    terminal_chat_id:typeof row.terminal_chat_id==="string" ? row.terminal_chat_id : "",
    peer_id:String(row.peer_id || ""),session_id:String(row.session_id || ""),terminal_id:String(row.terminal_id || ""),
    status:typeof row.status==="string" ? row.status : "unknown",error:row.error,
    delivery_mode:typeof row.delivery_mode==="string" ? row.delivery_mode : "",session_activity:typeof row.session_activity==="string" ? row.session_activity : "unknown",
    preferred_delivery_mode:row.preferred_delivery_mode==="agent_context_prompt" ? "agent_context_prompt" : "inbox",
    automatic_wake_available:row.automatic_wake_available===true,native_agent_origin:row.native_agent_origin===true,
    capabilities:row.capabilities && typeof row.capabilities==="object" && !Array.isArray(row.capabilities) ? row.capabilities as Record<string,unknown> : {},
    pending_permissions:Array.isArray(row.pending_permissions) ? row.pending_permissions : []};
}
export function grokBindingError(value:unknown):string {
  if(typeof value==="string")return value;
  return value && typeof value==="object" && "message" in value ? String(value.message || "") : "";
}
