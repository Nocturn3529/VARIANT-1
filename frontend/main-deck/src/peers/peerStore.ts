import {createModuleStore} from "../state/createModuleStore";
import type {RuntimeContext} from "../types";
import {parsePeer,parsePeerMessage,record,type PeerEndpoint,type PeerMessage,type PeerMessageKind} from "./peerModels";
import {parseGrokSavedSession,type GrokSavedSession} from "./grokSessions";

export type PeerRequest =
  | {operation:"grok:status"}
  | {operation:"grok:setup";replace_profile_id?:string}
  | {operation:"grok:sessions";cursor?:string}
  | {operation:"grok:launch";session_id?:string;cwd?:string}
  | {operation:"grok:connect";binding_id:string}
  | {operation:"grok:permission";binding_id:string;permission_id:string;option_id:string}
  | {operation:"grok:delivery";peer_id:string;delivery_mode:"inbox"|"agent_context_prompt";expected_delivery_mode:"inbox"|"agent_context_prompt"}
  | {operation:"list";kind?:string;status?:string;limit?:number}
  | {operation:"get";peer_id:string}
  | {operation:"inbox";after?:number;limit?:number;direction?:"incoming"|"outgoing"|"all"}
  | {operation:"send";peer_id:string;text:string;delivery?:"follow_up"|"steer";in_reply_to?:string;message_kind?:PeerMessageKind}
  | {operation:"reply";message_id:string;text:string;message_kind?:PeerMessageKind}
  | ({operation:"inspect"} & (
    {message_id:string;lookup_request_id?:never} | {lookup_request_id:string;message_id?:never}
  ));

export type PeerRequestState = Readonly<{
  requestId:string;chatId:string;operation:PeerRequest["operation"];
  request:Readonly<PeerRequest>;attempt:number;
  listGeneration?:string;
  profileId?:string;
  phase:"pending"|"unconfirmed"|"accepted"|"rejected";
  submittedText?:string;
  result?:Record<string,unknown>;
  error?:{code:string;message:string;commitState?:"not_committed"|"unknown"};
}>;
type PeerScope={peerId:string;peerIds:string[];messageIds:string[];cursor:number;hasMore:boolean;revision:number;changed:number;changedIds:string[];grokChanged:number;grokVersions:Record<string,number>};
type Draft={text:string;revision:number;messageKind?:PeerMessageKind};
const emptyScope=():PeerScope=>({peerId:"",peerIds:[],messageIds:[],cursor:0,hasMore:false,revision:0,changed:0,changedIds:[],grokChanged:0,grokVersions:{}});
const initialState=()=>({connected:false,requests:{} as Record<string,PeerRequestState>,peers:{} as Record<string,PeerEndpoint>,messages:{} as Record<string,PeerMessage>,scopes:{} as Record<string,PeerScope>,drafts:{} as Record<string,Draft>,
  grokSessions:{} as Record<string,{items:GrokSavedSession[];cursor:string|null}>,grokLaunchDrafts:{} as Record<string,{sessionId:string;cwd:string}>});
const store=createModuleStore({initialState:initialState()});
const draftSubmissions=new Map<string,{key:string;revision:number}>();
const timers=new Map<string,ReturnType<typeof setTimeout>>();
export const usePeers=store.useStore;
export const getPeerState=store.getState;
export const setPeerContext=(context:RuntimeContext)=>store.setContext(context);
const messageMutation=(operation:PeerRequest["operation"])=>operation==="send"||operation==="reply";
const mutation=(operation:PeerRequest["operation"])=>messageMutation(operation)||["grok:setup","grok:launch","grok:connect","grok:permission","grok:delivery"].includes(operation);
const confirmationMessage=(operation:PeerRequest["operation"])=>messageMutation(operation)
  ? "Send confirmation is unavailable. Check the sent messages before sending again."
  : "Operation confirmation is unavailable. Refresh Grok bindings before trying again.";

export function peerRequestKey(chatId:string,request:PeerRequest):string {
  const {operation,...fields}=request;
  const target=request.operation==="grok:setup" ? "setup" : request.operation==="grok:permission" ? [request.binding_id,request.permission_id] : request.operation==="grok:launch" ? "launch" : "binding_id" in request ? request.binding_id : "peer_id" in request ? request.peer_id : request.operation==="inspect" && request.lookup_request_id
    ? {lookup_request_id:request.lookup_request_id} : "message_id" in request ? request.message_id : fields;
  return JSON.stringify([chatId,operation,target]);
}
function clearTimer(key:string) {clearTimeout(timers.get(key));timers.delete(key);}
function update(key:string,value:Partial<PeerRequestState>) {
  const state=store.getState(),previous=state.requests[key];
  if(previous)store.setState({requests:{...state.requests,[key]:{...previous,...value}}});
}
function uncertain(key:string,code:string,message:string) {
  const entry=store.getState().requests[key];if(!entry)return;
  clearTimer(key);
  update(key,{phase:mutation(entry.operation)?"unconfirmed":"rejected",error:{code,message}});
}
export function setPeerConnection(status:string):void {
  const connected=status==="connected";
  if(store.getState().connected===connected)return;
  store.setConnected(connected);
  if(!connected)for(const [key,entry] of Object.entries(store.getState().requests)) {
    if(entry.phase==="pending")uncertain(key,"connection_lost",mutation(entry.operation)
      ? confirmationMessage(entry.operation)
      : "Connection interrupted. Refresh when the backend reconnects.");
  }
  // Reconnection never replays an uncertain mutation.
}

/** Correlation only: an accepted operation is not evidence of model observation. */
export function requestPeer(chatId:string,request:PeerRequest):string|null {
  if(!chatId || !store.getState().connected)return null;
  const key=peerRequestKey(chatId,request),prior=store.getState().requests[key];
  if(prior?.phase==="pending" || prior?.phase==="unconfirmed")return null;
  if(("text" in request && !request.text.trim()) || ("peer_id" in request && !request.peer_id) || ("binding_id" in request && !request.binding_id)
    || (request.operation==="grok:permission" && (!request.permission_id || !request.option_id))
    || (request.operation==="inspect" ? !request.message_id && !request.lookup_request_id : "message_id" in request && !request.message_id))return null;
  const requestId=globalThis.crypto.randomUUID();
  const entry:PeerRequestState={requestId,chatId,operation:request.operation,phase:"pending",request:Object.freeze({...request}),attempt:1,
    ...(request.operation==="grok:setup" ? {profileId:typeof store.getState().requests[peerRequestKey(chatId,{operation:"grok:status"})]?.result?.profile_id==="string" ? String(store.getState().requests[peerRequestKey(chatId,{operation:"grok:status"})]?.result?.profile_id) : undefined} : {}),
    ...(request.operation==="grok:sessions" ? {listGeneration:request.cursor ? store.getState().requests[peerRequestKey(chatId,{operation:"grok:sessions"})]?.requestId : requestId} : {}),
    ...(request.operation==="grok:status" && prior?.result ? {result:prior.result} : {}),
    ...("text" in request ? {submittedText:request.text} : {})};
  return dispatchPeer(key,entry) ? requestId : null;
}

function dispatchPeer(key:string,entry:PeerRequestState):boolean {
  const {operation,...payload}=entry.request;
  const {requestId,chatId}=entry;
  clearTimer(key);
  store.setState({requests:{...store.getState().requests,[key]:entry}});
  const timer=setTimeout(()=>{
    if(timers.get(key)===timer && store.getState().requests[key]?.requestId===requestId)uncertain(key,"confirmation_timeout",mutation(operation)
      ? confirmationMessage(operation)
      : "No response received. Refresh to try again.");
  },15000);
  timers.set(key,timer);
  if(!store.send({type:`peers:${operation}`,chat_id:chatId,request_id:requestId,...payload})) {
    clearTimer(key);update(key,{phase:entry.attempt>1 && mutation(operation)?"unconfirmed":"rejected",
      error:{code:"not_sent",message:"The request could not be sent.",commitState:entry.attempt>1?"unknown":"not_committed"}});return false;
  }
  return true;
}

export function inspectPeerRequest(chatId:string,requestId:string):string|null {
  const original=Object.values(store.getState().requests).find(entry=>entry.chatId===chatId && entry.requestId===requestId && messageMutation(entry.operation));
  return original ? requestPeer(chatId,{operation:"inspect",lookup_request_id:original.requestId}) : null;
}

/** Explicit user retry only; transport identity and submitted content cannot change. */
export function retryPeerRequest(chatId:string,requestId:string):boolean {
  if(!store.getState().connected)return false;
  const match=Object.entries(store.getState().requests).find(([,entry])=>entry.chatId===chatId && entry.requestId===requestId && messageMutation(entry.operation) && entry.phase==="unconfirmed");
  if(!match)return false;
  const [key,entry]=match;
  return dispatchPeer(key,{...entry,phase:"pending",error:undefined,attempt:entry.attempt+1});
}

export function ingestPeers(message:Record<string,unknown>):void {
  if(message.type==="peers:grok:changed") {
    if(typeof message.chat_id!=="string" || !message.chat_id || typeof message.binding_id!=="string" || !message.binding_id || typeof message.revision!=="number" || !Number.isSafeInteger(message.revision))return;
    const state=store.getState(),scope=state.scopes[message.chat_id] || emptyScope();
    if(message.revision<=(scope.grokVersions[message.binding_id] || 0))return;
    store.setState({scopes:{...state.scopes,[message.chat_id]:{...scope,grokChanged:scope.grokChanged+1,grokVersions:{...scope.grokVersions,[message.binding_id]:message.revision}}}});return;
  }
  if(message.type==="peer:changed") {
    if(typeof message.chat_id!=="string" || !message.chat_id.trim() || typeof message.revision!=="number" || !Number.isSafeInteger(message.revision) || message.revision<0)return;
    const state=store.getState(),scope=state.scopes[message.chat_id] || emptyScope();
    if(message.revision<=scope.changed)return;
    const id=typeof message.message_id==="string" ? message.message_id : "";
    const changedIds=id ? [...scope.changedIds.filter(item=>item!==id),id].slice(-100) : scope.changedIds;
    store.setState({scopes:{...state.scopes,[message.chat_id]:{...scope,changed:message.revision,changedIds}}});return;
  }
  if(message.type!=="peers:result")return;
  const match=Object.entries(store.getState().requests).find(([,entry])=>entry.requestId===message.request_id);
  if(!match)return;
  const [key,entry]=match;
  if(entry.chatId!==message.chat_id || entry.operation!==message.operation || !["pending","unconfirmed"].includes(entry.phase))return;
  if(typeof message.ok!=="boolean")return;
  if(entry.operation==="grok:sessions" && entry.listGeneration!==store.getState().requests[peerRequestKey(entry.chatId,{operation:"grok:sessions"})]?.requestId) {
    clearTimer(key);update(key,{phase:"rejected",error:{code:"superseded",message:"Saved sessions were refreshed. Load the current page instead."}});return;
  }
  clearTimer(key);
  const rawError=message.error as {code?:unknown;message?:unknown;commit_state?:unknown}|undefined;
  const result=message.result && typeof message.result==="object" && !Array.isArray(message.result) ? message.result as Record<string,unknown> : undefined;
  if(message.ok && messageMutation(entry.operation)) {
    const row=parsePeerMessage(result);
    if(!row || !matchesOriginal(entry,row)) {
      uncertain(key,"invalid_receipt","The returned message did not match this send. Check the saved message before sending again.");return;
    }
  }
  if(message.ok && (entry.operation==="grok:launch" || entry.operation==="grok:connect")
    && (result?.viewer_chat_id!==entry.chatId || typeof result.binding_id!=="string" || !result.binding_id)) {
    uncertain(key,"invalid_binding","The returned binding could not be attributed to this chat. Refresh bindings to confirm the operation.");return;
  }
  if(message.ok && entry.request.operation==="grok:permission" && (result?.viewer_chat_id!==entry.chatId || result?.permission_id!==entry.request.permission_id || result?.accepted!==true)) {
    uncertain(key,"invalid_permission","Permission confirmation is unavailable. Refresh bindings; the answer will not be replayed.");return;
  }
  if(message.ok && entry.request.operation==="grok:delivery" && (result?.viewer_chat_id!==entry.chatId || result?.peer_id!==entry.request.peer_id || result?.preferred_delivery_mode!==entry.request.delivery_mode)) {
    uncertain(key,"invalid_delivery","Delivery preference confirmation is unavailable. Refresh its status before changing it again.");return;
  }
  if(message.ok && entry.operation==="grok:setup" && (result?.viewer_chat_id!==entry.chatId || result?.adapter!=="grok-peer-bridge" || result?.installed!==true || (entry.profileId && result?.profile_id!==entry.profileId))) {
    uncertain(key,"invalid_setup","Bridge setup confirmation is unavailable. Refresh its status before trying again.");return;
  }
  if(message.ok && entry.operation==="grok:sessions" && (result?.viewer_chat_id!==entry.chatId || !Array.isArray(result?.items))) {
    uncertain(key,"invalid_sessions","The saved-session list could not be attributed to this chat. Refresh to try again.");return;
  }
  // A late pre-commit failure for one same-ID attempt cannot exclude another
  // overlapping attempt's commit. Only a positive persisted result resolves it.
  const notCommitted=rawError?.commit_state==="not_committed" && entry.attempt===1;
  update(key,{phase:message.ok?"accepted":mutation(entry.operation)&&!notCommitted?"unconfirmed":"rejected",result:!message.ok && entry.operation==="grok:status" ? entry.result : result,error:message.ok ? undefined : {
    code:String(rawError?.code || "peer_request_failed"),message:String(rawError?.message || "The peer request failed."),
    commitState:notCommitted?"not_committed":"unknown",
  }});
  if(message.ok && result)applyResult(entry,result);
}

export const peerDraftKey=(chatId:string,target:string,replyTo="")=>JSON.stringify([chatId,target,replyTo]);
export function setGrokLaunchDraft(chatId:string,value:{sessionId:string;cwd:string}):void {
  store.setState({grokLaunchDrafts:{...store.getState().grokLaunchDrafts,[chatId]:value}});
}
export function setPeerDraft(key:string,text:string):void {
  const state=store.getState();store.setState({drafts:{...state.drafts,[key]:{...state.drafts[key],text,revision:(state.drafts[key]?.revision || 0)+1}}});
}
export function setPeerDraftKind(key:string,messageKind:PeerMessageKind):void {
  const state=store.getState(),draft=state.drafts[key];store.setState({drafts:{...state.drafts,[key]:{text:draft?.text || "",messageKind,revision:(draft?.revision || 0)+1}}});
}
export function submitPeerDraft(chatId:string,key:string,request:PeerRequest):string|null {
  const revision=store.getState().drafts[key]?.revision || 0;
  const id=requestPeer(chatId,request);
  if(id)draftSubmissions.set(id,{key,revision});return id;
}
function clearSubmittedDraft(requestId:string) {
  const submitted=draftSubmissions.get(requestId);if(!submitted)return;
  const state=store.getState(),draft=state.drafts[submitted.key];
  if(draft?.revision===submitted.revision)store.setState({drafts:{...state.drafts,[submitted.key]:{...draft,text:"",revision:draft.revision+1}}});
  draftSubmissions.delete(requestId);
}
function matchesOriginal(entry:PeerRequestState,message:PeerMessage):boolean {
  const request=entry.request;
  if(request.operation==="send" || request.operation==="reply") {
    const expected=request.message_kind || (request.operation==="reply" ? "result" : "request");
    if((request.message_kind || message.message_kind) && expected!==message.message_kind)return false;
  }
  return message.request_id===entry.requestId && message.content===entry.submittedText?.trim() &&
    (request.operation==="send" ? message.target_peer_id===request.peer_id && message.in_reply_to===(request.in_reply_to || "") :
      request.operation==="reply" && message.in_reply_to===request.message_id);
}
function applyResult(entry:PeerRequestState,result:Record<string,unknown>) {
  if(entry.operation==="grok:delivery") {
    const statusKey=peerRequestKey(entry.chatId,{operation:"grok:status"}),status=store.getState().requests[statusKey];
    if(Array.isArray(status?.result?.items))update(statusKey,{result:{...status.result,items:status.result.items.map(value=>{
      const row=record(value);return row.viewer_chat_id===entry.chatId && row.peer_id===result.peer_id ? {...row,delivery_mode:result.delivery_mode,preferred_delivery_mode:result.preferred_delivery_mode,automatic_wake_available:result.automatic_wake_available,native_agent_origin:result.native_agent_origin,...(result.capabilities ? {capabilities:result.capabilities} : {})} : value;
    })}});
  }
  if(entry.request.operation==="grok:sessions") {
    const state=store.getState(),prior=entry.request.cursor ? state.grokSessions[entry.chatId]?.items || [] : [];
    const rows=new Map(prior.map(row=>[row.session_id,row]));
    for(const value of result.items as unknown[]){const row=parseGrokSavedSession(value);if(row)rows.set(row.session_id,row);}
    store.setState({grokSessions:{...state.grokSessions,[entry.chatId]:{items:[...rows.values()],cursor:typeof result.cursor==="string" && result.cursor ? result.cursor : null}}});
  }
  const state=store.getState(),scope={...(state.scopes[entry.chatId] || emptyScope())};
  const peers={...state.peers},messages={...state.messages};
  if(entry.operation==="list" && Array.isArray(result.items)) {
    scope.peerIds=[];
    for(const value of result.items){const peer=parsePeer(value);if(!peer)continue;scope.peerIds.push(peer.peer_id);if(!peers[peer.peer_id] || peer.revision>=peers[peer.peer_id].revision)peers[peer.peer_id]=peer;}
  }
  if(entry.operation==="get") {const peer=parsePeer(result);if(peer && (!peers[peer.peer_id] || peer.revision>=peers[peer.peer_id].revision))peers[peer.peer_id]=peer;}
  const inbox=entry.operation==="inbox" && typeof result.peer_id==="string" && Array.isArray(result.messages);
  if(inbox) {
    scope.peerId=String(result.peer_id);
    if(Number(result.cursor)>=scope.cursor && entry.request.operation==="inbox")scope.hasMore=(result.messages as unknown[]).length>=(entry.request.limit || 50);
    scope.cursor=Math.max(scope.cursor,Number(result.cursor)||0);scope.revision=Math.max(scope.revision,Number(result.revision)||0);
  }
  const received=(inbox ? result.messages as unknown[] : ["send","reply","inspect"].includes(entry.operation) ? [result] : []).flatMap(value=>{
    const row=parsePeerMessage(value);
    return row && (!inbox || row.sender_peer_id===scope.peerId || row.target_peer_id===scope.peerId) ? [row] : [];
  });
  for(const row of received) {
    if(!messages[row.message_id] || row.revision>messages[row.message_id].revision)messages[row.message_id]=row;
    if(!scope.messageIds.includes(row.message_id))scope.messageIds=[...scope.messageIds,row.message_id];
  }
  store.setState({peers,messages,scopes:{...state.scopes,[entry.chatId]:scope}});
  for(const row of received)for(const [key,original] of Object.entries(store.getState().requests)) {
    if(original.chatId!==entry.chatId || !matchesOriginal(original,row))continue;
    // Inbox sender is canonical. Inspect-by-request is also sender-scoped by the service.
    if(inbox && row.sender_peer_id!==scope.peerId)continue;
    if(entry.operation==="inspect" && entry.request.operation==="inspect" && entry.request.lookup_request_id!==original.requestId)continue;
    clearTimer(key);update(key,{phase:"accepted",error:undefined,result:row as unknown as Record<string,unknown>});clearSubmittedDraft(original.requestId);
  }
  if(entry.operation==="grok:status" && Array.isArray(result.items))for(const [key,original] of Object.entries(store.getState().requests)) {
    if(original.chatId===entry.chatId && original.request.operation==="grok:delivery" && original.phase==="unconfirmed") {
      const request=original.request;
      const binding=result.items.map(record).find(row=>row.viewer_chat_id===entry.chatId && row.peer_id===request.peer_id && row.preferred_delivery_mode===request.delivery_mode);
      if(binding){clearTimer(key);update(key,{phase:"accepted",result:binding,error:undefined});}
    }
    if(original.chatId!==entry.chatId || original.operation!=="grok:launch" || original.phase!=="unconfirmed")continue;
    const binding=result.items.map(record).find(row=>row.viewer_chat_id===entry.chatId && row.request_id===original.requestId && typeof row.binding_id==="string" && row.binding_id);
    if(binding){clearTimer(key);update(key,{phase:"accepted",result:binding,error:undefined});}
  }
  if(entry.operation==="grok:status" && result.adapter==="grok-peer-bridge" && result.installed===true && typeof result.profile_id==="string" && result.profile_id && result.installation_profile_id===result.profile_id) {
    for(const [key,original] of Object.entries(store.getState().requests))if(original.chatId===entry.chatId && original.operation==="grok:setup" && original.phase==="unconfirmed" && (!original.profileId || original.profileId===result.profile_id)) {
      clearTimer(key);update(key,{phase:"accepted",result:{viewer_chat_id:entry.chatId,adapter:result.adapter,installed:true,profile_id:result.profile_id},error:undefined});
    }
  }
}

export function disposePeers():void {
  for(const key of timers.keys())clearTimer(key);
  draftSubmissions.clear();store.setContext(null);store.replaceState(initialState());
}
