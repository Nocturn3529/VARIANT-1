import {useEffect,useState} from "react";
import {createPortal} from "react-dom";
import {Overlay,OverlayHeader} from "../ui/Overlay";
import {useSurfaceDocument} from "../ui/SurfaceDocument";
import {GrokControl} from "./GrokControl";
import {PeerDialog} from "./PeerControl";
import {peerRequestKey,requestPeer,usePeers} from "./peerStore";
import {grokDeliveryLabel} from "./grokSessions";

function ConnectorDialog({chatId,onClose}:{chatId:string;onClose:()=>void}) {
  const state=usePeers(),[messages,setMessages]=useState(false);
  const request=state.requests[peerRequestKey(chatId,{operation:"list"})];
  useEffect(()=>{if(state.connected)requestPeer(chatId,{operation:"list"});},[chatId,state.connected]);
  const peers=(state.scopes[chatId]?.peerIds || []).map(id=>state.peers[id]).filter(peer=>peer?.kind==="external_harness");
  if(messages)return <PeerDialog chatId={chatId} onClose={onClose}/>;
  return <Overlay className="grok-peer-dialog" labelledBy="connector-title" onClose={onClose}>
    <OverlayHeader title="Agent connectors" id="connector-title" onClose={onClose}/>
    <div className="grok-peer-dialog__body">
      <p>Connected terminal agents appear here using their registered session identity.</p>
      <div className="grok-peer-dialog__actions"><button disabled={!state.connected || request?.phase==="pending"} onClick={()=>requestPeer(chatId,{operation:"list"})}>{request?.phase==="pending" ? "Refreshing…" : "Refresh"}</button><button onClick={()=>setMessages(true)}>Peer messages</button></div>
      {request?.error ? <p role="status">{request.error.message}</p>:null}
      {peers.map(peer=><article className="grok-peer-binding" key={peer.peer_id}><header><strong>{peer.display_name}</strong><span>{peer.status}</span></header><p>{grokDeliveryLabel(peer.delivery_mode,peer.capabilities.live_ingress).label}</p></article>)}
      {request?.phase==="accepted" && !peers.length ? <p>No terminal agents are registered yet.</p>:null}
      <h2 className="grok-connections-title">Available adapters</h2>
      <div className="grok-peer-binding"><header><strong>Grok Build</strong><GrokControl chatId={chatId} label="Manage adapter"/></header><p>Set up the bridge, launch or resume a session, and manage its connection.</p></div>
    </div>
  </Overlay>;
}
export function ConnectorControl({chatId,terminalId}:{chatId:string;terminalId?:string}) {
  const owner=useSurfaceDocument(),[open,setOpen]=useState(false),state=usePeers();
  const peer=terminalId ? Object.values(state.peers).find(item=>item.kind==="external_harness" && item.terminal_id===terminalId) : undefined;
  return <><button type="button" title={peer ? `${peer.display_name} · ${peer.status}` : "Terminal agent connections"} disabled={!chatId} onClick={()=>setOpen(true)}>Connectors</button>
    {open ? createPortal(<ConnectorDialog key={chatId} chatId={chatId} onClose={()=>setOpen(false)}/>,owner.body):null}</>;
}
