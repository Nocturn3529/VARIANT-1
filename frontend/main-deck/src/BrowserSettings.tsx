import {useEffect, useId, useState} from "react";
import {useChatState} from "./chatStore";
import {
  browserSelectionKey, browserSelectionRequestKey, refreshBrowserChat, refreshBrowserSettings, saveBrowserSelection,
  useBrowserSettings, type BrowserCatalog, type BrowserSelection,
  refreshBrowserRecordings,
} from "./browserSettingsStore";
import {BROWSER_STATE_LABELS, BrowserReadinessActions} from "./chat/ChatBrowserStatus";
import {Button} from "./ui/Button";
import {SettingsSection} from "./ui/Settings";
import {notifyToast} from "./state/toastStore";

const MODES: readonly {id: BrowserSelection["mode"]; title: string; description: string}[] = [
  {id: "embedded", title: "Inside VARIANT-1", description: "Browse in the chat workspace with VARIANT-1’s saved browser session."},
  {id: "managed", title: "Managed browser", description: "Use VARIANT-1’s own persistent Chromium profile."},
  {id: "personal", title: "My browser profile", description: "Use a managed copy of a browser profile you select."},
  {id: "cdp", title: "Connect an existing browser", description: "Attach to a browser’s debugging endpoint. Disconnecting leaves its existing browser context open."},
  {id: "cloud", title: "Cloud browser", description: "Use a browser session from a configured cloud provider."},
];

const FIELD_LABELS: Record<string, string> = {
  cdp_url: "Browser debugging URL", cloud_provider: "Cloud provider", project_id: "Browserbase project ID",
  headed: "Show the browser window", executable_path: "Custom Chromium executable", command_timeout_s: "Command timeout (seconds)",
  click_timeout_s: "Click timeout (seconds)", navigation_timeout_s: "Navigation timeout (seconds)", dialog_policy: "Page dialogs",
  record_sessions: "Record browser sessions", allow_private_urls: "Allow local and private websites", evaluate_enabled: "Allow page JavaScript execution",
};
const OPTION_LABELS: Record<string, string> = {browserbase: "Browserbase", "browser-use": "Browser Use", firecrawl: "Firecrawl", auto_dismiss: "Dismiss automatically", auto_accept: "Accept automatically"};

function BrowserSelectionForm({catalog, selection, revision, chatId}: {
  catalog: BrowserCatalog; selection: BrowserSelection; revision: number; chatId?: string;
}) {
  const settings = useBrowserSettings();
  const prefix = useId();
  const [draft, setDraft] = useState(selection);
  const [baseRevision, setBaseRevision] = useState(revision);
  const [baseSelection, setBaseSelection] = useState(browserSelectionKey(selection));
  const [edited, setEdited] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const key = browserSelectionRequestKey(chatId);
  const saving = !!settings.pending[key];
  const error = settings.errors[key];
  const connecting = !!chatId && settings.chats[chatId]?.state === "connecting";
  const resolving = !!chatId && !!settings.pending[`resolve:${chatId}`];
  const busy = saving || connecting || resolving;
  const changedElsewhere = edited && baseSelection !== browserSelectionKey(selection);
  useEffect(() => {
    if (!edited || browserSelectionKey(draft) === browserSelectionKey(selection)) {
      setDraft(selection); setBaseRevision(revision); setBaseSelection(browserSelectionKey(selection)); setEdited(false);
    } else if (baseSelection === browserSelectionKey(selection)) {
      // Readiness also advances the chat revision. Preserve the draft while
      // accepting that progress if its underlying saved choice did not change.
      setBaseRevision(revision);
    }
  }, [selection, revision, edited, draft, baseSelection]);
  const choose = (value: BrowserSelection) => { setDraft(value); setEdited(true); setSubmitted(false); };
  const browser = catalog.browsers.find(item => item.id === draft.browser_id);
  const profile = browser?.profiles.find(item => item.id === draft.profile_id);
  const fields = (catalog.options?.fields || []).filter(field => field.modes.includes(draft.mode) && (field.key !== "project_id" || draft.cloud_provider === "browserbase"));
  const valid = (draft.mode !== "personal" || !!profile)
    && (draft.mode !== "cdp" || /^https?:\/\/|^wss?:\/\//.test(String(draft.cdp_url || "")))
    && (draft.mode !== "cloud" || !!draft.cloud_provider && (draft.cloud_provider !== "browserbase" || !!String(draft.project_id || "").trim()))
    && fields.every(field => field.type !== "number" || draft[field.key] === undefined || Number.isInteger(draft[field.key]) && Number(draft[field.key]) >= (field.min ?? 1) && Number(draft[field.key]) <= (field.max ?? 300));
  const missing = draft.mode === "personal" && (draft.browser_id && !browser || draft.profile_id && !profile);
  const changed = browserSelectionKey(draft) !== browserSelectionKey(selection);
  return <form className="browser-selection-form" aria-label={chatId ? "Browser choice for this chat" : "Default browser choice"} aria-busy={saving}
    onSubmit={event => { event.preventDefault(); if (valid && !busy && !changedElsewhere && saveBrowserSelection(draft, baseRevision, chatId)) setSubmitted(true); }}>
    <fieldset disabled={!settings.connected || busy}>
      <legend>Browser mode</legend>
      {MODES.filter(mode => (catalog.options?.modes || ["embedded", "managed", "personal"]).includes(mode.id)).map(mode => <label className="browser-mode-option" key={mode.id}>
        <input type="radio" name={`${prefix}-mode`} value={mode.id} checked={draft.mode === mode.id} onChange={() => choose({mode: mode.id})}/>
        <span><strong>{mode.title}</strong><small>{mode.description}</small></span>
      </label>)}
      {draft.mode === "personal" ? <div className="browser-profile-fields">
        <label htmlFor={`${prefix}-browser`}>Browser</label>
        <select id={`${prefix}-browser`} value={draft.browser_id || ""} onChange={event => choose({...draft, browser_id: event.target.value, profile_id: undefined})}>
          <option value="">Choose a browser</option>
          {draft.browser_id && !browser ? <option value={draft.browser_id} disabled>Saved browser unavailable</option> : null}
          {catalog.browsers.map(item => <option value={item.id} key={item.id}>{item.label}</option>)}
        </select>
        <label htmlFor={`${prefix}-profile`}>Profile</label>
        <select id={`${prefix}-profile`} value={draft.profile_id || ""} disabled={!browser || !browser.profiles.length} onChange={event => choose({...draft, profile_id: event.target.value})}>
          <option value="">{browser && !browser.profiles.length ? "No profiles found" : "Choose a profile"}</option>
          {draft.profile_id && !profile ? <option value={draft.profile_id} disabled>Saved profile unavailable</option> : null}
          {browser?.profiles.map(item => <option value={item.id} key={item.id}>{item.label}{item.directory_name ? ` · ${item.directory_name}` : ""}</option>)}
        </select>
        <p>VARIANT-1 uses a copy of the selected profile. On Windows, you may need to close that browser before retrying a locked profile.</p>
        {missing ? <p role="alert">The saved browser or profile is unavailable. Choose another profile explicitly.</p> : null}
        {!catalog.browsers.length ? <p>No supported browser profiles were found. Refresh after setting up a browser profile.</p> : null}
      </div> : null}
      {fields.length ? <div className="browser-options-fields">{fields.map(field => {
        const value = draft[field.key] ?? catalog.options?.defaults[field.key] ?? "";
        const label = FIELD_LABELS[field.key] || field.key;
        return <label key={field.key}>
          <span>{label}</span>
          {field.type === "boolean" ? <input type="checkbox" checked={value === true} onChange={event => choose({...draft, [field.key]: event.target.checked})}/>
            : field.type === "select" ? <select value={String(value)} onChange={event => choose({...draft, [field.key]: event.target.value})}>
              {!value ? <option value="">Choose…</option> : null}
              {(field.options || []).map(option => <option value={option} key={option}>{OPTION_LABELS[option] || option}</option>)}
            </select>
            : <input type={field.type === "number" ? "number" : "text"} value={String(value)} min={field.min} max={field.max} step={field.type === "number" ? 1 : undefined}
              placeholder={field.key === "cdp_url" ? "http://localhost:9222" : field.key === "executable_path" ? "Use the installed Chromium by default" : undefined}
              onChange={event => choose({...draft, [field.key]: field.type === "number" && event.target.value !== "" ? event.target.valueAsNumber : event.target.value})}/>
          }
        </label>;
      })}</div> : null}
    </fieldset>
    <div className="browser-selection-actions">
      <Button tone="primary" type="submit" disabled={!settings.connected || busy || !valid || changedElsewhere || (!changed && !chatId)}>{saving ? "Saving…" : chatId ? "Use for this chat" : "Save default"}</Button>
      {edited ? <Button tone="quiet" disabled={busy} onClick={() => { setDraft(selection); setBaseRevision(revision); setBaseSelection(browserSelectionKey(selection)); setEdited(false); setSubmitted(false); }}>Reset to saved</Button> : null}
      {submitted && !saving && !error && !changed ? <span role="status">Browser choice saved.</span> : null}
    </div>
    {changedElsewhere ? <div className="browser-settings-error" role="status">The saved browser choice changed while you were editing.
      <Button tone="quiet" disabled={busy} onClick={() => { setBaseRevision(revision); setBaseSelection(browserSelectionKey(selection)); }}>Keep my choice</Button>
    </div> : null}
    {connecting ? <p role="status">Wait for the browser connection, or cancel its request before changing this chat’s choice.</p> : null}
    {error ? <p role="alert" className="browser-settings-error">{error}</p> : null}
  </form>;
}

export function BrowserSettings() {
  const settings = useBrowserSettings();
  const chatId = useChatState().sessionId;
  const chat = chatId ? settings.chats[chatId] : undefined;
  const refresh = () => { refreshBrowserSettings(); if (chatId) refreshBrowserChat(chatId); };
  useEffect(() => { if (settings.connected) refresh(); }, [settings.connected, chatId]);
  return <div className="browser-settings">
    <div className="browser-settings-toolbar"><span>{settings.connected ? "Browser profiles are discovered when you open these settings." : "Backend disconnected."}</span>
      <Button tone="quiet" disabled={!settings.connected || !!settings.pending.catalog} onClick={refresh}>{settings.pending.catalog ? "Refreshing…" : "Refresh"}</Button>
    </div>
    {settings.errors.catalog ? <p role="alert" className="browser-settings-error">{settings.errors.catalog}</p> : null}
    {!settings.catalog ? <p role="status">{settings.pending.catalog ? "Reading browser settings…" : "Browser settings are unavailable. Refresh when the backend is connected."}</p> : <>
      {chatId ? <SettingsSection title="This chat" description="The browser choice stays with this chat. Changing the default below does not change an active browser session.">
        {chat ? <>
          <div className="browser-readiness" role="status"><strong>{settings.connected ? BROWSER_STATE_LABELS[chat.state] : "Browser disconnected"}</strong>
            <p>{chat.message}</p><small>{chat.selection_source === "chat" ? "Saved for this chat" : "Using the default"}{chat.browser_session_id ? " · Browser session retained" : ""}</small>
          </div>
          <BrowserReadinessActions chatId={chatId} showSelection={false}/>
          <BrowserSelectionForm key={chatId} catalog={settings.catalog} chatId={chatId} selection={chat.selection} revision={chat.revision}/>
        </> : <p role="status">{settings.pending[`state:${chatId}`] ? "Reading this chat’s browser…" : "Refresh to read this chat’s browser choice."}</p>}
        {settings.errors[`state:${chatId}`] ? <p role="alert">{settings.errors[`state:${chatId}`]}</p> : null}
      </SettingsSection> : null}
      <SettingsSection title="Default for new sessions" description="Choose how future browser sessions start. Personal profiles are used only after you select and save one.">
        <BrowserSelectionForm catalog={settings.catalog} selection={settings.catalog.default.selection} revision={settings.catalog.default.revision}/>
      </SettingsSection>
      {chatId && settings.catalog.options?.fields.some(field => field.key === "record_sessions") ? <SettingsSection title="Session recordings" description="Saved recordings belong to this chat. Recording is available for managed browsers and personal profiles."
        action={<Button tone="quiet" disabled={!settings.connected || !!settings.pending[`recordings:${chatId}`]} onClick={() => refreshBrowserRecordings(chatId)}>Refresh recordings</Button>}>
        {settings.errors[`recordings:${chatId}`] ? <p role="alert">{settings.errors[`recordings:${chatId}`]}</p> : null}
        {(settings.recordings[chatId] || []).map(recording => <div className="browser-recording" key={recording.path}>
          <span>{recording.name}<small>{recording.complete ? "Saved" : "Recording"} · {Math.ceil(recording.bytes / 1024 ** 2)} MB</small></span>
          <Button disabled={!recording.complete} onClick={() => {
            const open = window.variant1Deck?.openLocalPath;
            if (!open) {notifyToast("Open recordings from the VARIANT-1 desktop app."); return;}
            void open(recording.path).then(result => {if (result?.ok === false) notifyToast(result.reason || "Could not open recording.");}).catch(() => notifyToast("Could not open recording."));
          }}>Open recording</Button>
        </div>)}
        {settings.recordings[chatId]?.length === 0 ? <p>No saved recordings for this chat.</p> : null}
      </SettingsSection> : null}
    </>}
  </div>;
}
