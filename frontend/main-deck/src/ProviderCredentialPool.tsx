import {useEffect, useRef, useState} from "react";
import {send, usePlatformState, refreshProviderCredentials} from "./store";
import {createRequestIdFactory} from "./state/storePrimitives";
import type {CredentialInfo, ProviderInfo} from "./types";
import {Button} from "./ui/Button";
import {useSurfaceDocument} from "./ui/SurfaceDocument";

const nextId = createRequestIdFactory("provider-keys");
type Operation = "add" | "remove" | "enable" | "priority" | "strategy";

export function ProviderKeyRow({provider, credentials, expanded, onToggle}: {
  provider: ProviderInfo; credentials: CredentialInfo[]; expanded: boolean; onToggle: () => void;
}) {
  const state = usePlatformState(), doc = useSurfaceDocument();
  const [draft, setDraft] = useState(""), [label, setLabel] = useState("");
  const [pending, setPending] = useState<{id: string; operation: Operation} | null>(null);
  const inFlight = useRef(false);
  const [feedback, setFeedback] = useState(""), [error, setError] = useState("");
  const configured = !!credentials.length || !!provider.api_key_configured;
  const strategy = state.config.credential_strategy_by_provider?.[provider.name] || "priority";
  const busy = !!pending || !state.connected;
  useEffect(() => {
    if (expanded && state.connected) refreshProviderCredentials(provider.name);
  }, [expanded, state.connected, provider.name]);
  useEffect(() => {
    if (!pending) return;
    const receipt = state.credentialReceipt;
    if (receipt?.requestId !== pending.id || receipt.provider !== provider.name || receipt.operation !== pending.operation) return;
    if (receipt.accepted) {
      if (pending.operation === "add") {setDraft(""); setLabel("");}
      setFeedback(pending.operation === "add" ? "Key added." : pending.operation === "remove" ? "Key removed." : "Updated.");
    } else setError(receipt.error || "The key could not be updated.");
    inFlight.current = false; setPending(null);
  }, [state.credentialReceipt, pending, provider.name]);
  useEffect(() => {
    if (!pending) return;
    if (!state.connected) {inFlight.current = false; setPending(null); setError("Connection interrupted. Refresh the keys before trying again."); return;}
    const timer = setTimeout(() => {inFlight.current = false; setPending(null); setError("No response received. Refresh the keys to check the result.");}, 15000);
    return () => clearTimeout(timer);
  }, [pending, state.connected]);
  function change(operation: Operation, payload: Record<string, unknown>) {
    if (!state.connected || inFlight.current) return;
    const id = nextId(operation);
    inFlight.current = true; setPending({id, operation}); setFeedback(""); setError("");
    const action = operation === "priority" || operation === "strategy" ? `${operation}:set` : operation;
    if (!send({...payload, type: `cloud:credential:${action}`, request_id: id, provider: provider.name})) {
      inFlight.current = false; setPending(null); setError("Request could not be sent.");
    }
  }
  return <article className={`provider-key-row${expanded ? " provider-key-row--expanded" : ""}`}>
    <div className="provider-key-row__grid">
      <button className="provider-key-row__label" type="button" aria-expanded={expanded} onClick={onToggle}>
        <i className={`provider-status${configured ? " provider-status--ready" : ""}`} aria-hidden="true"/><strong>{provider.display_name}</strong><span aria-hidden="true">⌄</span>
      </button>
      <form className="provider-key-row__field" onSubmit={event => {event.preventDefault(); if (draft.trim()) change("add", {key: draft.trim(), label: label.trim()});}}>
        <input aria-label={`${provider.display_name} API key`} type="password" autoComplete="off" value={draft} disabled={busy}
          placeholder={credentials.length ? "Add another API key" : "Paste API key"} onFocus={() => {if (!expanded) onToggle();}} onChange={event => setDraft(event.target.value)}
          onKeyDown={event => {if (event.key === "Escape") {event.preventDefault(); event.stopPropagation(); setDraft(""); event.currentTarget.blur();}}}/>
        <Button type="submit" tone="quiet" disabled={busy || !draft.trim()}>{pending?.operation === "add" ? "Adding…" : "Add key"}</Button>
      </form>
    </div>
    {error ? <p className="runtime-error" role="alert">{error}</p> : feedback ? <p role="status" className="platform-note">{feedback}</p> : null}
    {expanded ? <div className="provider-key-row__details">
      <p>{provider.description || "Keys are stored securely on this device. Adding a key keeps your existing keys."}</p>
      {provider.signup_url ? <a href={provider.signup_url} target="_blank" rel="noreferrer">Get API key ↗</a> : null}
      <label className="provider-pool-label">New key label <input value={label} disabled={busy} maxLength={80} placeholder="Optional" onChange={event => setLabel(event.target.value)}/></label>
      {credentials.length ? <>
        <label className="provider-pool-label">Key selection <select value={strategy} disabled={busy} onChange={event => change("strategy", {strategy: event.target.value})}>
          <option value="priority">Priority order</option><option value="round_robin">Rotate keys</option>
        </select></label>
        <small>Lower priority numbers are tried first.</small>
        <div className="provider-pool-list">{credentials.map(credential => <div className="provider-pool-item" key={credential.id}>
          <span><strong>{credential.label || "API key"}</strong><small>{credential.enabled ? credential.status || "Enabled" : "Disabled"}</small></span>
          <label>Priority <input key={`${credential.id}:${credential.priority}:${!!pending}`} aria-label={`Priority for ${credential.label || "API key"}`} type="number" step={1} defaultValue={credential.priority} disabled={busy}
            onBlur={event => {const priority = event.currentTarget.valueAsNumber; if (Number.isSafeInteger(priority) && priority !== credential.priority) change("priority", {credential_id: credential.id, priority});}}/></label>
          <Button tone="quiet" disabled={busy} onClick={() => change("enable", {credential_id: credential.id, enabled: !credential.enabled})}>{credential.enabled ? "Disable" : "Enable"}</Button>
          <Button tone="quiet" disabled={busy} onClick={() => {
            if (doc.defaultView?.confirm(`Remove ${credential.label || "this key"} from ${provider.display_name}? Other keys will be kept.`)) change("remove", {credential_id: credential.id});
          }}>Remove</Button>
        </div>)}</div>
      </> : configured ? <small>This key comes from the host environment and is managed there.</small> : <small>No stored keys.</small>}
      <Button tone="quiet" disabled={busy} onClick={() => {setError(""); refreshProviderCredentials(provider.name);}}>Refresh keys</Button>
    </div> : null}
  </article>;
}
