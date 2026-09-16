import {useEffect, useRef, useState} from "react";
import {useServiceSettings, refreshServiceSettings, changeServiceCredential, type ServiceSetting} from "./serviceSettingsStore";
import {selectSettingsCategory} from "./state/appStore";
import {SETTINGS_PAGES} from "./state/settingsCatalog";
import {Button} from "./ui/Button";
import {SettingsSection} from "./ui/Settings";
import {useSurfaceDocument} from "./ui/SurfaceDocument";

function ServiceRow({row}: {row: ServiceSetting}) {
  const state = useServiceSettings(), doc = useSurfaceDocument();
  const [draft, setDraft] = useState("");
  const [baseRevision, setBaseRevision] = useState(row.revision);
  const attempt = useRef<string | false>(false);
  const receipt = state.receipts[row.id];
  useEffect(() => {
    if (!attempt.current || receipt?.id !== attempt.current) return;
    attempt.current = false;
    if (receipt.ok) {setDraft(""); setBaseRevision(row.revision);}
  }, [receipt, row.revision]);
  const busy = !state.connected || !!state.pending[row.id];
  function change(value: string | null) {
    if (attempt.current) return;
    attempt.current = changeServiceCredential(row, value, value === null ? row.revision : baseRevision);
  }
  const category = SETTINGS_PAGES.find(page => page.id === row.settings_category);
  return <div className="service-key-row">
    <div><strong>{row.name}</strong><p>{row.description}</p><small>{row.configured ? row.source === "account" ? "Uses connected account" : row.source === "environment" ? "Uses environment key" : "Configured" : row.editable ? "No usable key configured" : row.service === "messaging" ? "Configure in Messaging" : "No key required"}</small></div>
    {row.editable ? <form onSubmit={event => {event.preventDefault(); if (draft.trim()) change(draft.trim());}}>
      <input type="password" autoComplete="off" aria-label={`${row.group}: ${row.name} key`} value={draft} disabled={busy} placeholder={row.stored ? "Replace stored key" : "Paste API key"}
        onChange={event => {if (!draft) setBaseRevision(row.revision); setDraft(event.target.value);}}/>
      <Button type="submit" disabled={busy || !draft.trim()}>{state.pending[row.id]?.operation === "set" ? "Saving…" : "Save"}</Button>
      {row.stored ? <Button tone="quiet" disabled={busy} onClick={() => {
        if (doc.defaultView?.confirm(`Remove the stored ${row.name} key for ${row.group.toLowerCase()}? A connected account or environment key may still be used.`)) change(null);
      }}>Remove</Button> : null}
    </form> : null}
    {category ? <Button tone="quiet" onClick={() => selectSettingsCategory(category.id)}>Configure {category.label.toLowerCase()}</Button> : null}
    {draft && row.revision !== baseRevision ? <Button tone="quiet" disabled={busy} onClick={() => setBaseRevision(row.revision)}>Use refreshed setting</Button> : null}
    {receipt?.error ? <p className="runtime-error" role="alert">{receipt.error}</p> : receipt?.ok ? <small role="status">{receipt.operation === "clear" ? "Stored key removed." : "Saved."}</small> : null}
  </div>;
}

export function ToolsAndKeysSettings() {
  const state = useServiceSettings(), [query, setQuery] = useState("");
  useEffect(() => {if (state.connected) refreshServiceSettings();}, [state.connected]);
  const rows = state.items.filter(row => `${row.name} ${row.group}`.toLowerCase().includes(query.trim().toLowerCase()));
  const groups = [...new Set(rows.map(row => row.group))];
  return <div className="tools-keys-settings">
    <div className="local-model-search"><input aria-label="Search tools and keys" placeholder="Search services" value={query} onChange={event => setQuery(event.target.value)}/>
      <Button disabled={!state.connected || !!state.pending.catalog} onClick={refreshServiceSettings}>{state.pending.catalog ? "Refreshing…" : "Refresh"}</Button></div>
    <p className="platform-note">Keys are stored securely on this device. Inference accounts and API keys are managed under Providers.</p>
    <Button tone="quiet" onClick={() => selectSettingsCategory("provider-keys")}>Manage provider API keys</Button>
    {!state.connected ? <p role="status">Backend offline.</p> : null}
    {state.receipts.catalog?.error ? <p className="runtime-error" role="alert">{state.receipts.catalog.error}</p> : null}
    {groups.map(group => <SettingsSection key={group} title={group}>{rows.filter(row => row.group === group).map(row => <ServiceRow key={row.id} row={row}/>)}</SettingsSection>)}
    {!rows.length && !state.pending.catalog ? <p>No matching services.</p> : null}
  </div>;
}
