import {PEER_DELIVERY,record,type PeerEndpoint,type PeerMessage} from "./peerModels";

export function PeerMessageCard({message,peers={},onReply,onInspect}:{
  message:PeerMessage;peers?:Record<string,PeerEndpoint>;onReply?:()=>void;onInspect?:()=>void;
}) {
  const status=PEER_DELIVERY[message.state] || {label:message.state,meaning:"Status reported by the peer service."};
  const sender=peers[message.sender_peer_id],target=peers[message.target_peer_id];
  const error=typeof message.error==="string" ? message.error : record(message.error).message;
  const timestamp=typeof message.created_at==="string" ? Date.parse(message.created_at) : typeof message.created_at==="number" ? message.created_at*1000 : NaN;
  return <article className="peer-message" data-message-id={message.message_id}>
    <header><span className="peer-message__kind">{message.message_kind ? message.message_kind[0].toUpperCase()+message.message_kind.slice(1) : "Independent peer"}</span><span title={status.meaning} className="peer-message__state">{status.label}</span></header>
    <div className="peer-message__route"><strong title={message.sender_peer_id}>{sender?.display_name || message.sender_peer_id}</strong><span aria-label="to">→</span><strong title={message.target_peer_id}>{target?.display_name || message.target_peer_id}</strong></div>
    <p className="peer-message__content">{message.content}</p>
    {error ? <p role="status">{String(error)}</p>:null}
    <footer>{Number.isFinite(timestamp) ? <time dateTime={new Date(timestamp).toISOString()}>{new Date(timestamp).toLocaleString()}</time>:null}
      {onReply ? <button type="button" onClick={onReply}>Reply</button>:null}
      {onInspect ? <button type="button" onClick={onInspect}>Refresh receipt</button>:null}
    </footer>
    <details><summary>Delivery details</summary><p>{status.meaning}</p><dl>
      <dt>Message</dt><dd>{message.message_id}</dd><dt>Exchange</dt><dd>{message.exchange_id}</dd>
      <dt>Sender</dt><dd>{message.sender_peer_id}</dd><dt>Recipient</dt><dd>{message.target_peer_id}</dd>
      {message.in_reply_to ? <><dt>Reply to</dt><dd>{message.in_reply_to}</dd></>:null}
      {message.target_run_id ? <><dt>Recipient run</dt><dd>{message.target_run_id}</dd></>:null}
    </dl></details>
  </article>;
}
