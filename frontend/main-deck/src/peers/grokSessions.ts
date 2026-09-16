import {record} from "./peerModels";

export type GrokSavedSession=Readonly<{
  session_id:string;title:string;cwd:string;updated_at:unknown;
}>;

export function parseGrokSavedSession(value:unknown):GrokSavedSession|null {
  const row=record(value);
  if(typeof row.session_id!=="string" || !row.session_id)return null;
  return {session_id:row.session_id,title:typeof row.title==="string" && row.title.trim() ? row.title : row.session_id,
    cwd:typeof row.cwd==="string" ? row.cwd : "",updated_at:row.updated_at};
}

/** Connection presence alone never proves the harness can wake its model. */
export function grokDeliveryLabel(mode:string,liveIngress:unknown):{label:string;detail:string} {
  if(mode==="inbox")return {label:"Inbox only",detail:"Messages wait in the shared inbox. Grok must check its inbox; receiving a message does not wake its model."};
  if(mode==="agent_context_prompt")return liveIngress===true
    ? {label:"Automatic inbox wake",detail:"New requests can wake Grok to check its inbox. Grok displays the notification as user input; this is not native agent-origin delivery. Notices and results stay in the inbox."}
    : {label:"Automatic wake unavailable",detail:"This connection cannot currently wake Grok. Messages remain available in its inbox."};
  return {label:"Delivery capability unconfirmed",detail:"The connection has not declared a supported delivery mode."};
}
