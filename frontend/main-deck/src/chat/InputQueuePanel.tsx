import {useChatState} from "../chatStore";
import {useSessionState} from "../state/sessionStore";
import {useSessionContextState,getContextForSession} from "../sessionContextStore";
import {Icon} from "../ui/Icon";
import {mutateInputQueue,queueAdmissionPending,refreshInputQueue} from "./inputQueue";

export function InputQueuePanel() {
  const state=useChatState(),queue=state.inputQueue,items=queue.snapshot?.items || [];
  const navigating=!!useSessionState().pendingAction;useSessionContextState();
  if(!items.length && !queue.action && !queue.error && !queueAdmissionPending())return null;
  const blocked=!state.connected || !queue.synced || !!queue.action || navigating;
  return <section className="composer-input-queue composer-input-queue--canonical" aria-label="Queued messages">
    <header><strong><Icon name="queue"/>Queued messages <span>{items.length}</span></strong>
      <button type="button" aria-label="Refresh queued messages" disabled={!state.connected || !!queue.refreshRequestId} onClick={refreshInputQueue}><Icon name="refresh"/></button></header>
    {!queue.synced ? <p role="status">Checking saved queue…</p> : null}
    {queue.error ? <p role="status">{queue.error}</p> : null}
    {queueAdmissionPending() ? <p role="status">Waiting for the selected message to start…</p> : null}
    {items.map(item=>{
      const pending=queue.action?.ticket.ticket_id===item.ticket_id ? queue.action.operation : null;
      const removable=["queued","resume_queued","parked"].includes(item.state);
      const label=item.state==="parked" ? "Parked · continue when ready" : item.state==="selected" ? "Selected" : item.state==="preparing" ? "Preparing" : item.delivery==="steer" ? "Steering queued" : "Queued next";
      return <div className="composer-input-queue__item" key={item.ticket_id} data-queue-ticket={item.ticket_id}>
        <div className="composer-input-queue__text"><span>{item.text}</span><small>{pending ? pending==="continue" ? "Requesting continuation…" : "Removing…" : label}</small></div>
        <div className="composer-input-queue__actions">
          {item.state==="parked" ? <button type="button" disabled={blocked || state.turnActive || state.stopPending || queueAdmissionPending() || !!getContextForSession(state.sessionId || "").settingsPending}
            onClick={()=>mutateInputQueue("continue",item.ticket_id,state.sessionId)}>Continue</button> : null}
          <button type="button" disabled={blocked || !removable || queueAdmissionPending()} title={removable ? "Remove this queued message" : "This message has already been selected for execution"}
            onClick={()=>mutateInputQueue("remove",item.ticket_id,state.sessionId)}>Remove</button>
        </div>
      </div>;
    })}
  </section>;
}
