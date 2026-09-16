import {useEffect, useMemo, useRef, useState, type FormEvent} from "react";
import {
  refresh,
  send,
  setMessagingAdapterEnabled,
  setMessagingGatewayEnabled,
  usePlatformState,
} from "./store";
import type {GatewayAdapter, MessagingPairingUser} from "./types";
import {selectSettingsCategory} from "./state/appStore";
import {Button} from "./ui/Button";
import {Switch} from "./ui/Switch";

function pairingLabel(user: MessagingPairingUser): string {
  return user.user_name || user.user_id;
}

function PlatformRow({adapter, active, pending, onClick}: {
  adapter: GatewayAdapter;
  active: boolean;
  pending: number;
  onClick: () => void;
}) {
  return <button type="button" className={active ? "messaging-platform-row active" : "messaging-platform-row"}
    onClick={onClick}>
    <span className="messaging-platform-avatar" aria-hidden="true">{adapter.display_name.slice(0, 1)}</span>
    <span><strong>{adapter.display_name}</strong><small>{adapter.connected ? "Connected"
      : adapter.config?.enabled ? adapter.last_error || "Starting" : "Off"}</small></span>
    {pending ? <b aria-label={`${pending} pending requests`}>{pending}</b> : null}
    <i data-state={adapter.connected ? "connected" : adapter.config?.enabled ? "warning" : "idle"}/>
  </button>;
}

function PairingRows({title, rows, pending}: {
  title: string;
  rows: MessagingPairingUser[];
  pending?: boolean;
}) {
  if (!rows.length) return null;
  return <section className="messaging-pairing">
    <h4>{title}</h4>
    {rows.map(user => <div className="messaging-pairing-row" key={`${user.platform}:${user.user_id}`}>
      <span><strong>{pairingLabel(user)}</strong><small>{user.user_id}</small></span>
      <Button tone="quiet" onClick={() => send(pending
        ? {type: "messaging:pairing:approve", platform: user.platform, request_id: user.request_id}
        : {type: "messaging:pairing:revoke", platform: user.platform, user_id: user.user_id})}>
        {pending ? "Approve" : "Revoke"}
      </Button>
    </div>)}
  </section>;
}

function PlatformDetail({adapter}: {adapter: GatewayAdapter}) {
  const gateway = usePlatformState().gateway;
  const values = adapter.values || {};
  const [edits, setEdits] = useState<Record<string, string>>(() => Object.fromEntries(
    adapter.fields.filter(field => !field.secret)
      .map(field => [field.key, String(values[field.key] ?? "")]),
  ));
  const [showAdvanced, setShowAdvanced] = useState(false);
  const dirty = useRef(new Set<string>());
  useEffect(() => {
    setEdits(current => {
      const next = {...current};
      for (const field of adapter.fields.filter(field => !field.secret)) {
        const value = String(adapter.values?.[field.key] ?? "");
        if (value === current[field.key]) dirty.current.delete(field.key);
        if (!dirty.current.has(field.key)) next[field.key] = value;
      }
      return next;
    });
  }, [adapter]);
  const edit = (key:string,value:string) => {dirty.current.add(key);setEdits(current=>({...current,[key]:value}));};
  const pending = (gateway?.pairing?.pending || []).filter(row => row.platform === adapter.id);
  const approved = (gateway?.pairing?.approved || []).filter(row => row.platform === adapter.id);
  const visibleFields = adapter.fields.filter(field => !field.advanced || showAdvanced);
  const advancedCount = adapter.fields.filter(field => field.advanced).length;

  function save(event: FormEvent) {
    event.preventDefault();
    const nonSecret = Object.fromEntries(adapter.fields.filter(field => !field.secret)
      .map(field => [field.key, String(edits[field.key] || "").trim()]));
    send({type: "messaging:set", adapter: adapter.id,
      config: {enabled: !!adapter.config?.enabled, fields: nonSecret}});
    for (const field of adapter.fields.filter(field => field.secret)) {
      const value = String(edits[field.key] || "").trim();
      if (value) send({type: "messaging:credential:set", adapter: adapter.id,
        field: field.key, value});
    }
    setEdits(current => ({...current, ...Object.fromEntries(
      adapter.fields.filter(field => field.secret).map(field => [field.key, ""]))}));
  }

  return <form className="messaging-platform-detail" onSubmit={save}>
    <header>
      <span><strong>{adapter.display_name}</strong><small>{adapter.description}</small></span>
      <Switch checked={!!adapter.config?.enabled} disabled={!gateway?.enabled || !adapter.runtime_available}
        aria-label={`${adapter.display_name} ${adapter.config?.enabled ? "on" : "off"}`}
        onChange={enabled => setMessagingAdapterEnabled(adapter.id, enabled)}/>
    </header>
    {!adapter.runtime_available ? <div className="messaging-runtime-needed">
      <span><strong>Platform runtime not installed</strong><small>
        Install a messaging plugin that contributes the {adapter.display_name} adapter. Credentials can be prepared now.
      </small></span>
      <Button tone="quiet" onClick={() => selectSettingsCategory("plugins")}>Open Plugins</Button>
    </div> : null}
    {adapter.last_error ? <p className="messaging-platform-error">{adapter.last_error}</p> : null}
    <PairingRows title="Pending requests" rows={pending} pending/>
    <PairingRows title="Approved users" rows={approved}/>
    <section className="messaging-fields">
      <div><h4>Connection</h4>{adapter.docs_url ? <a href={adapter.docs_url}
        target="_blank" rel="noreferrer">Open setup guide ↗</a> : null}</div>
      {visibleFields.map(field => <label key={field.key}>
        <span>{field.label}{field.required ? " *" : ""}</span>
        <div>{field.value_type === "boolean" && !field.secret ? <Switch
          checked={edits[field.key] === "true"}
          aria-label={field.label}
          onChange={value => edit(field.key, String(value))}
        /> : <input type={field.secret ? "password" : "text"}
          value={edits[field.key] || ""}
          onChange={event => edit(field.key, event.target.value)}
          placeholder={field.secret && adapter.configured_fields.includes(field.key)
            ? "Stored securely — enter to replace"
            : field.placeholder || field.key}/>} 
          {field.secret && adapter.configured_fields.includes(field.key) ? <Button tone="quiet"
            onClick={() => send({type: "messaging:credential:clear", adapter: adapter.id,
              field: field.key})}>Remove</Button> : null}</div>
      </label>)}
      {advancedCount ? <button type="button" className="messaging-advanced"
        onClick={() => setShowAdvanced(value => !value)}>
        {showAdvanced ? "Hide advanced fields" : `Show ${advancedCount} advanced fields`}
      </button> : null}
    </section>
    <section className="messaging-access">
      <span><strong>Accept anyone</strong><small>When off, new senders appear above for approval.</small></span>
      <Switch checked={!!adapter.config?.allow_all} aria-label="Accept anyone"
        onChange={allow_all => send({type: "messaging:set", adapter: adapter.id,
          config: {...adapter.config, allow_all}})}/>
    </section>
    <footer><Button type="submit" tone="primary">Save changes</Button></footer>
  </form>;
}

export function MessagingSettings() {
  const {gateway, connected} = usePlatformState();
  const adapters = gateway?.adapters || [];
  const [selectedId, setSelectedId] = useState("");
  useEffect(() => {
    if (!adapters.some(adapter => adapter.id === selectedId)) {
      setSelectedId(adapters[0]?.id || "");
    }
  }, [adapters, selectedId]);
  const selected = useMemo(() => adapters.find(adapter => adapter.id === selectedId) || adapters[0],
    [adapters, selectedId]);
  const pendingCounts = useMemo(() => Object.fromEntries(adapters.map(adapter => [adapter.id,
    (gateway?.pairing?.pending || []).filter(row => row.platform === adapter.id).length])),
    [adapters, gateway?.pairing?.pending]);

  return <div className="messaging-settings">
    <div className="messaging-gateway-bar">
      <span><strong>Messaging gateway</strong><small>{connected
        ? `${adapters.length} platforms · ${gateway?.session_count || 0} sessions`
        : "Waiting for backend"}</small></span>
      <Button tone="quiet" onClick={refresh}>Refresh</Button>
      <Switch checked={!!gateway?.enabled} aria-label="Messaging gateway"
        onChange={setMessagingGatewayEnabled}/>
    </div>
    <div className="messaging-platform-browser">
      <nav aria-label="Messaging platforms">{adapters.map(adapter => <PlatformRow
        key={adapter.id} adapter={adapter} active={selected?.id === adapter.id}
        pending={pendingCounts[adapter.id] || 0} onClick={() => setSelectedId(adapter.id)}/>)}</nav>
      {selected ? <PlatformDetail key={selected.id} adapter={selected}/> : <p>No messaging platforms available.</p>}
    </div>
  </div>;
}
