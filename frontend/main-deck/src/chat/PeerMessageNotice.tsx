import type {Icon as IconType} from "../ui/Icon";
import type {ChatTurnStep} from "./types";

/** Peer trace markup is needed only after a peer message reaches the timeline. */
export default function PeerMessageNotice({step, Icon, deliveryLabels, incoming}: {
  step: ChatTurnStep; Icon: typeof IconType; deliveryLabels?: Readonly<Record<string, {label: string}>>;
  incoming?: {content?: string; connected: boolean};
}) {
  if (step.peerInbound) {
    const name = step.peerInbound.display_name || "another agent";
    return <details className="peer-send-trace peer-receive-trace" open data-conversation-scaffold="" data-trace-id={step.id}>
      <summary aria-label={`Message from ${name}, steered into this task`}><Icon name="peers"/><span>Message from <strong>{name}</strong></span><small>Steered in</small><Icon name="chevron"/></summary>
      <p>{incoming?.content ?? (incoming?.connected ? "Loading message…" : "Message unavailable while offline.")}</p>
    </details>;
  }
  const peer = step.peerMessage!;
  const name = peer.target_display_name || "another agent";
  const delivery = deliveryLabels?.[peer.state]?.label || peer.state;
  return <details className={`peer-send-trace${peer.state === "failed" ? " is-error" : ""}`} data-conversation-scaffold="" data-trace-id={step.id}>
    <summary aria-label={`Sent a message to ${name}, ${delivery}`}><Icon name="send"/><span>Sent a message to <strong>{name}</strong></span><small>{delivery}</small><Icon name="chevron"/></summary>
    <p>{peer.content}</p>
  </details>;
}
