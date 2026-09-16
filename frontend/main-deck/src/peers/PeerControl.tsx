import {useState} from "react";
import {createPortal} from "react-dom";
import {Overlay,OverlayHeader} from "../ui/Overlay";
import {useSurfaceDocument} from "../ui/SurfaceDocument";
import {Icon} from "../ui/Icon";
import {GrokControl} from "./GrokControl";
import {PeerMessageCard} from "./PeerMessageCard";
import {inspectPeerRequest,peerDraftKey,peerRequestKey,requestPeer,retryPeerRequest,setPeerDraft,setPeerDraftKind,submitPeerDraft,usePeers,type PeerRequest} from "./peerStore";
import type {PeerMessageKind} from "./peerModels";
import {usePeerRefresh} from "./usePeerRefresh";
import {grokDeliveryLabel} from "./grokSessions";

export function PeerDialog({chatId,onClose}:{chatId:string;onClose:()=>void}) {
  const state=usePeers();
  const [view,setView]=useState<"discover"|"inbox"|"sent">("discover");
  const [search,setSearch]=useState("");
  const [recipient,setRecipient]=useState("");
  const [replyTo,setReplyTo]=useState("");
  const [delivery,setDelivery]=useState<"follow_up"|"steer">("follow_up");
  usePeerRefresh(chatId,"messages");
  const scope=state.scopes[chatId];
  const ownPeer=scope?.peerId || Object.values(state.peers).find(peer=>peer.chat_id===chatId)?.peer_id;
  const endpoints=(scope?.peerIds || []).map(id=>state.peers[id]).filter(peer=>peer && peer.peer_id!==ownPeer);
  const messages=(scope?.messageIds || []).map(id=>state.messages[id]).filter(Boolean).sort((a,b)=>b.sequence-a.sequence);
  const draftKey=peerDraftKey(chatId,recipient,replyTo),text=state.drafts[draftKey]?.text || "";
  const kind=state.drafts[draftKey]?.messageKind || (replyTo ? "result" : "request");
  const request:PeerRequest=replyTo ? {operation:"reply",message_id:replyTo,text,message_kind:kind} : {operation:"send",peer_id:recipient,text,message_kind:kind,...(kind==="request" ? {delivery} : {})};
  const send=state.requests[peerRequestKey(chatId,request)];
  const guarded=send?.phase==="pending" || send?.phase==="unconfirmed";
  const list=state.requests[peerRequestKey(chatId,{operation:"list"})];
  const inbox=state.requests[peerRequestKey(chatId,{operation:"inbox",direction:"all",limit:100})];
  const more=scope?.cursor ? state.requests[peerRequestKey(chatId,{operation:"inbox",after:scope.cursor,direction:"all",limit:100})] : undefined;
  const pending=list?.phase==="pending" || inbox?.phase==="pending";
  const refresh=()=>{requestPeer(chatId,{operation:"list"});requestPeer(chatId,{operation:"inbox",direction:"all",limit:100});};
  const choose=(id:string,reply="")=>{setRecipient(id);setReplyTo(reply);};
  const unresolved=Object.values(state.requests).filter(entry=>entry.chatId===chatId && ["send","reply"].includes(entry.operation) && entry.phase==="unconfirmed");
  const shown=messages.filter(message=>view==="inbox" ? message.target_peer_id===ownPeer : message.sender_peer_id===ownPeer);
  return <Overlay className="peer-dialog" labelledBy="peer-dialog-title" onClose={onClose}>
    <OverlayHeader title="Peers" id="peer-dialog-title" onClose={onClose}/>
    <div className="peer-dialog__intro"><span>Message another chat or a connected harness. Each peer keeps its own work.</span><GrokControl chatId={chatId}/></div>
    <nav className="peer-dialog__tabs" aria-label="Peer views">{(["discover","inbox","sent"] as const).map(tab=><button key={tab} type="button" aria-pressed={view===tab} onClick={()=>setView(tab)}>{tab==="discover" ? "Discover" : tab==="inbox" ? "Inbox" : "Sent"}</button>)}<button type="button" disabled={!state.connected || pending} onClick={refresh}>{pending ? "Refreshing…" : "Refresh"}</button></nav>
    <div className="peer-dialog__body">
      {!state.connected ? <p role="status">Backend disconnected. Drafts are retained.</p>:null}
      {[list,inbox,more].map((entry,index)=>entry?.error ? <p key={index} role="alert">{entry.error.message}</p>:null)}
      {view==="discover" ? <><input aria-label="Find a peer" placeholder="Find a chat or harness…" value={search} onChange={event=>setSearch(event.target.value)}/>
        <div className="peer-directory">{endpoints.filter(peer=>`${peer.display_name} ${peer.peer_id}`.toLowerCase().includes(search.toLowerCase())).map(peer=><button type="button" key={peer.peer_id} aria-pressed={recipient===peer.peer_id} onClick={()=>choose(peer.peer_id)}>
          <strong>{peer.display_name}</strong><span>{peer.kind==="variant_chat" ? "Chat" : "External harness"} · {peer.status}</span>
          {peer.kind==="external_harness" ? <span title={grokDeliveryLabel(peer.delivery_mode,peer.capabilities.live_ingress).detail}>{grokDeliveryLabel(peer.delivery_mode,peer.capabilities.live_ingress).label}</span>:null}<small>{peer.peer_id}</small>
        </button>)}</div>{list?.phase==="accepted" && !endpoints.length ? <p>No other peers are available.</p>:null}</> : <>
        {view==="sent" ? unresolved.map(entry=><article className="peer-message" key={entry.requestId}><strong>Send confirmation unavailable</strong><p className="peer-message__content">{entry.submittedText}</p><p>{entry.error?.message}</p><div className="peer-message__actions"><button disabled={!state.connected} onClick={()=>inspectPeerRequest(chatId,entry.requestId)}>Check saved message</button><button disabled={!state.connected} onClick={()=>retryPeerRequest(chatId,entry.requestId)}>Retry same request</button></div><InspectionStatus chatId={chatId} request={{operation:"inspect",lookup_request_id:entry.requestId}}/></article>):null}
        {shown.map(message=><div key={message.message_id}><PeerMessageCard message={message} peers={state.peers}
          onReply={message.target_peer_id===ownPeer ? ()=>choose(message.sender_peer_id,message.message_id) : undefined}
          onInspect={()=>requestPeer(chatId,{operation:"inspect",message_id:message.message_id})}/><InspectionStatus chatId={chatId} request={{operation:"inspect",message_id:message.message_id}}/></div>)}
        {inbox?.phase==="accepted" && !shown.length ? <p>{view==="inbox" ? "No incoming peer messages." : "No sent peer messages."}</p>:null}
        {scope?.hasMore ? <button disabled={!state.connected || more?.phase==="pending"} onClick={()=>requestPeer(chatId,{operation:"inbox",after:scope.cursor,direction:"all",limit:100})}>{more?.phase==="pending" ? "Loading…" : "Load more messages"}</button>:null}
      </>}
    </div>
    {recipient ? <form className="peer-compose" onSubmit={event=>{event.preventDefault();submitPeerDraft(chatId,draftKey,request);}}>
      <div><strong>{replyTo ? "Reply to" : "Message"} {state.peers[recipient]?.display_name || recipient}</strong><button type="button" aria-label="Close peer draft" onClick={()=>choose("")}>×</button></div>
      <textarea aria-label="Peer message" value={text} maxLength={80000} placeholder="Send a message to this peer…" onChange={event=>setPeerDraft(draftKey,event.target.value)}/>
      <div className="peer-compose__options"><label>Kind <select aria-label="Peer message kind" value={kind} disabled={guarded} onChange={event=>setPeerDraftKind(draftKey,event.target.value as PeerMessageKind)}><option value="request">Request</option><option value="notice">Notice</option><option value="result">Result</option></select></label>
        {!replyTo && kind==="request" ? <label>Delivery <select value={delivery} disabled={guarded} onChange={event=>setDelivery(event.target.value as typeof delivery)}><option value="follow_up">Follow-up</option><option value="steer">Steer current work</option></select></label>:null}<button type="submit" disabled={!state.connected || !text.trim() || guarded}>{send?.phase==="pending" ? "Sending…" : "Send to peer"}</button></div>
      {kind!=="request" ? <small>Inbox only. This message does not start new work.</small>:null}
      {send?.error ? <p role="status">{send.error.message}</p>:null}
      {send?.phase==="unconfirmed" ? <button type="button" disabled={!state.connected} onClick={()=>inspectPeerRequest(chatId,send.requestId)}>Check saved message</button>:null}
      {send?.phase==="unconfirmed" ? <InspectionStatus chatId={chatId} request={{operation:"inspect",lookup_request_id:send.requestId}}/>:null}
    </form>:null}
  </Overlay>;
}

function InspectionStatus({chatId,request}:{chatId:string;request:Extract<PeerRequest,{operation:"inspect"}>}) {
  const state=usePeers(),entry=state.requests[peerRequestKey(chatId,request)];
  if(entry?.phase==="pending")return <p role="status">Checking the saved receipt…</p>;
  return entry?.error ? <p role="status">Receipt check: {entry.error.message}</p>:null;
}

export function PeerControl({chatId}:{chatId:string|null}) {
  const owner=useSurfaceDocument(),[open,setOpen]=useState(false);
  return <><button type="button" className="composer-icon-button" aria-label="Peers" title="Message an independent peer" disabled={!chatId} onClick={()=>setOpen(true)}><Icon name="peers"/></button>
    {open && chatId ? createPortal(<PeerDialog key={chatId} chatId={chatId} onClose={()=>setOpen(false)}/>,owner.body):null}</>;
}
