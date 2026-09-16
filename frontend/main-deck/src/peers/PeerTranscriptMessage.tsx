import {useEffect,useState} from "react";
import type {ChatMessage} from "../chat/types";
import {useChatState} from "../chatStore";
import {peerRequestKey,requestPeer,usePeers} from "./peerStore";
import {Icon} from "../ui/Icon";

/** Model-facing envelopes and receipt diagnostics never become chat content. */
export function PeerTranscriptMessage({message}:{message:ChatMessage & {origin:NonNullable<ChatMessage["origin"]>}}) {
  const {sessionId}=useChatState(),state=usePeers(),origin=message.origin;
  const [expanded,setExpanded]=useState(false);
  const receipt=state.messages[origin.message_id];
  const matched=receipt?.sender_peer_id===origin.peer_id ? receipt : undefined;
  const content=message.peerDisplay?.content ?? matched?.content;
  const name=message.peerDisplay?.display_name || matched?.sender_display_name || state.peers[origin.peer_id]?.display_name || "Peer agent";
  const request={operation:"inspect" as const,message_id:origin.message_id};
  const failed=sessionId && state.requests[peerRequestKey(sessionId,request)]?.phase==="rejected";
  useEffect(()=>{
    if(!state.connected || !sessionId || message.peerDisplay || matched)return;
    requestPeer(sessionId,{operation:"inspect",message_id:origin.message_id});
    requestPeer(sessionId,{operation:"get",peer_id:origin.peer_id});
  },[state.connected,sessionId,origin.message_id,origin.peer_id,message.peerDisplay,matched]);
  const long=!!content && (content.length>280 || content.split("\n").length>4);
  return <article className="message message--peer" aria-label={`Message from ${name}`}>
    <div className="peer-chat"><header><Icon name="peers"/><span>Sent by {name}</span></header>
      <div className="peer-chat__bubble"><p className={long && !expanded ? "is-collapsed" : undefined}>{content ?? (!state.connected ? "Peer message unavailable while offline." : failed ? "Could not load this peer message." : "Loading peer message…")}</p>
        {content===undefined && failed && state.connected && sessionId ? <button type="button" onClick={()=>requestPeer(sessionId,request)}>Retry</button>:null}
        {long ? <button type="button" aria-expanded={expanded} onClick={()=>setExpanded(value=>!value)}>{expanded ? "Show less" : "Show more"}<Icon name="down"/></button>:null}
      </div>
    </div>
  </article>;
}
