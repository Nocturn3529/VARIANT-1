import {useEffect,useId} from "react";
import type {GrokBinding} from "./grokBindings";
import {peerRequestKey,requestPeer,usePeers} from "./peerStore";

export function GrokDeliveryControl({chatId,binding}:{chatId:string;binding:GrokBinding}) {
  const state=usePeers(),name=useId();
  const preferred=binding.preferred_delivery_mode;
  const entry=state.requests[peerRequestKey(chatId,{operation:"grok:delivery",peer_id:binding.peer_id,delivery_mode:preferred,expected_delivery_mode:preferred})];
  const pending=entry?.phase==="pending" || entry?.phase==="unconfirmed";
  useEffect(()=>{if(entry?.phase==="accepted"){requestPeer(chatId,{operation:"grok:status"});requestPeer(chatId,{operation:"list"});}},[entry?.requestId,entry?.phase,chatId]);
  const choose=(delivery_mode:GrokBinding["preferred_delivery_mode"])=>{
    if(delivery_mode===preferred || pending || !state.connected || !binding.peer_id || (delivery_mode==="agent_context_prompt" && !binding.automatic_wake_available))return;
    requestPeer(chatId,{operation:"grok:delivery",peer_id:binding.peer_id,delivery_mode,expected_delivery_mode:preferred});
  };
  return <fieldset className="grok-delivery-choice" disabled={!state.connected || pending || !binding.peer_id}>
    <legend>Message delivery</legend>
    <label><input type="radio" name={name} value="inbox" checked={preferred==="inbox"} onChange={()=>choose("inbox")}/><span><strong>Inbox only</strong><small>No synthetic user turn. Grok checks its inbox when asked.</small></span></label>
    <label><input type="radio" name={name} value="agent_context_prompt" checked={preferred==="agent_context_prompt"} disabled={!binding.automatic_wake_available} onChange={()=>choose("agent_context_prompt")}/><span><strong>Automatic inbox wake</strong><small>Grok displays a user-input notification for new requests.</small></span></label>
    {preferred==="agent_context_prompt" && !binding.automatic_wake_available ? <p>Automatic wake is saved, but unavailable on this connection. Delivery is currently inbox only.</p>:null}
    <p>Notices and results stay in the inbox. Switching to inbox only does not cancel work already admitted.</p>
    {entry?.phase==="pending" ? <p role="status">Saving preference…</p>:null}
    {entry?.error ? <p role="status">{entry.error.message}</p>:null}
  </fieldset>;
}
