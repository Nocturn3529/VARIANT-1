import {useEffect, useMemo, useRef, useState} from "react";
import {notifyToast} from "./state/toastStore";
import {selectSettingsCategory} from "./state/appStore";
import {
  beginOAuthFlow,
  clearCustomEndpointFeedback,
  cancelOAuthFlow,
  send,
  usePlatformState,
} from "./store";
import type {
  CustomEndpoint,
  InferencePlatform,
  InstallJob,
  LocalRuntimeStatus,
  ProviderInfo,
} from "./types";
import {ProviderKeyRow} from "./ProviderCredentialPool";
import {Button} from "./ui/Button";
import {Overlay} from "./ui/Overlay";


function Status({ok}: {ok: boolean}) {
  return <i className={ok ? "provider-status provider-status--ready" : "provider-status"} aria-hidden="true"/>;
}


function TrashIcon() {
  return <svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    <path d="M4 7h16M9 7V4h6v3M7 7l1 13h8l1-13M10 11v5M14 11v5"/>
  </svg>;
}


function providerMethods(provider: ProviderInfo): string[] {
  if (Array.isArray(provider.auth_methods)) {
    return provider.auth_methods;
  }
  if (provider.name === "openai-codex") return ["oauth"];
  if (provider.name === "xai") return ["oauth", "api_key"];
  if (provider.custom || provider.name.startsWith("custom-")) return ["custom"];
  if (provider.auth_style === "optional") return ["external"];
  return ["api_key"];
}

function providerOAuthStatus(provider: ProviderInfo, config: ReturnType<typeof usePlatformState>["config"]) {
  if (config.oauth_by_provider?.[provider.name]) return config.oauth_by_provider[provider.name];
  if (provider.name === "xai") return config.oauth?.xai_detail;
  if (provider.name === "openai-codex") return config.oauth?.openai_codex_detail;
  return undefined;
}


function OAuthDialog({providers}: {providers: ProviderInfo[]}) {
  const {oauthFlow} = usePlatformState();
  const [copied, setCopied] = useState(false);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const provider = providers.find(item => item.name === oauthFlow.provider);
  const title = oauthFlow.provider === "openai-codex"
    ? "ChatGPT or Codex Subscription"
    : provider?.display_name || oauthFlow.provider || "provider";

  function close() { cancelOAuthFlow(); }

  useEffect(() => setCopied(false), [oauthFlow.userCode]);
  useEffect(() => {
    if (oauthFlow.phase === "idle") return;
    closeButtonRef.current?.focus({preventScroll: true});
  }, [oauthFlow.phase]);
  if (oauthFlow.phase === "idle") return null;

  async function copyCode() {
    if (!oauthFlow.userCode) return;
    try {
      await navigator.clipboard.writeText(oauthFlow.userCode);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    } catch {
      setCopied(false);
    }
  }

  return <Overlay className="provider-oauth-overlay" labelledBy="provider-oauth-title" onClose={close} closeOnSurfaceClick>
    <section className="provider-oauth-dialog">
      <button ref={closeButtonRef} className="provider-oauth-dialog__close" type="button" aria-label="Close" onClick={close}>×</button>
      <h3 id="provider-oauth-title">Sign in with {title}</h3>
      {oauthFlow.phase === "starting" ? <div className="provider-oauth-dialog__status" role="status">
        <i className="provider-spinner"/> Starting sign-in…
      </div> : null}
      {oauthFlow.phase === "pending" ? <>
        <p>We opened {title} in your browser. {oauthFlow.userCode ? "Enter this code there:" : "Finish authorization there."}</p>
        {oauthFlow.userCode ? <button className={copied ? "provider-device-code provider-device-code--copied" : "provider-device-code"} type="button" aria-label="Copy device code" onClick={() => void copyCode()}>
          {[...oauthFlow.userCode].map((character, index) => character === "-" || character === " "
            ? <span className="provider-device-code__separator" key={index}>–</span>
            : <span className="provider-device-code__cell" key={index}>{character}</span>)}
        </button> : null}
        <footer className="provider-oauth-dialog__footer">
          {oauthFlow.verificationUrl ? <a href={oauthFlow.verificationUrl} target="_blank" rel="noreferrer">↗ Re-open verification page</a> : <span/>}
          <span className="provider-oauth-dialog__waiting"><i className="provider-spinner"/> Waiting for you to authorize…</span>
          <Button tone="quiet" onClick={close}>Cancel</Button>
        </footer>
      </> : null}
      {oauthFlow.phase === "complete" ? <div className="provider-oauth-dialog__result" role="status">
        <Status ok/> <span>{title} is connected.</span><Button tone="primary" onClick={close}>Done</Button>
      </div> : null}
      {oauthFlow.phase === "error" ? <div className="provider-oauth-dialog__error" role="alert">
        <p>{oauthFlow.error || "Sign-in failed."}</p><Button tone="quiet" onClick={close}>Close</Button>
      </div> : null}
      {oauthFlow.userCode ? <small className="provider-oauth-dialog__hint">Click the code to copy it.</small> : null}
    </section>
  </Overlay>;
}


function ProviderAccountRow({
  provider,
  connected,
  external,
  onSelect,
  onRemove,
}: {
  provider: ProviderInfo;
  connected: boolean;
  external: boolean;
  onSelect: () => void;
  onRemove?: () => void;
}) {
  return <article className="provider-account-row">
    <button className="provider-account-row__main" type="button" onClick={onSelect}>
      <span className="provider-account-row__copy">
        <span className="provider-account-row__title"><strong>{provider.display_name}</strong>{connected ? <em>✓ Connected</em> : null}</span>
        <small>{provider.description || (external ? "Uses an already authenticated desktop service." : "Connect your account in the browser.")}</small>
      </span>
      <span className="provider-account-row__trail" aria-hidden="true">{external ? "↗" : "›"}</span>
    </button>
    {connected && onRemove ? <button
      className="provider-account-row__remove"
      type="button"
      title={`Remove ${provider.display_name}`}
      aria-label={`Remove ${provider.display_name}`}
      onClick={onRemove}
    ><TrashIcon/></button> : null}
  </article>;
}


export function ProviderAccounts() {
  const {config} = usePlatformState();
  const [showAll, setShowAll] = useState(false);
  const providers = config.providers || [];
  const priority: Record<string, number> = {"openai-codex": 0, xai: 1, nous: 2, hermes: 3, ollama: 4, lmstudio: 5};
  const accountProviders = providers.filter(provider => (
    providerMethods(provider).includes("oauth") || providerMethods(provider).includes("external")
  )).sort((left, right) => (priority[left.name] ?? 50) - (priority[right.name] ?? 50) || left.display_name.localeCompare(right.display_name));
  const connected = accountProviders.filter(provider => {
    const methods = providerMethods(provider);
    if (methods.includes("oauth")) {
      return !!providerOAuthStatus(provider, config)?.connected;
    }
    return methods.includes("external") && provider.configured;
  });
  const others = accountProviders.filter(provider => !connected.includes(provider));
  const showOthers = others.length > 0 && (showAll || !connected.length);
  const collapsible = connected.length > 0 && others.length > 0;

  function select(provider: ProviderInfo) {
    const external = providerMethods(provider).includes("external");
    if (external) {
      notifyToast(`${provider.display_name} is managed by its desktop service. Choose its model in the chat composer.`);
      return;
    }
    beginOAuthFlow(provider.name);
  }

  function remove(provider: ProviderInfo) {
    if (!window.confirm(`Remove the stored ${provider.display_name} account from VARIANT-1?`)) return;
    if (!send({type: "cloud:oauth:disconnect", provider: provider.name})) {
      notifyToast("Could not remove the account — backend offline");
    }
  }

  return <section className="provider-simple-page">
    <header className="provider-section-heading">
      <div><span className="deck-instrument__eyebrow">ACCOUNTS</span><h3>Connect an account</h3></div>
      <button className="provider-inline-link" type="button" onClick={() => selectSettingsCategory("provider-keys")}>I have an API key</button>
    </header>
    <p className="provider-section-intro">Choose a subscription account or an already authenticated desktop inference service.</p>
    <div className="provider-account-list">
      <button className="provider-account-row provider-account-row--local" type="button" onClick={() => selectSettingsCategory("local-models")}>
        <span className="provider-account-row__copy"><span className="provider-account-row__title"><strong>Local models</strong></span><small>Run a GGUF model supplied on this device.</small></span>
        <span className="provider-account-row__trail" aria-hidden="true">›</span>
      </button>
      {connected.length ? <p className="provider-group-label">Connected</p> : null}
      {connected.map(provider => <ProviderAccountRow
        key={provider.name}
        provider={provider}
        connected
        external={providerMethods(provider).includes("external")}
        onSelect={() => select(provider)}
        onRemove={providerMethods(provider).includes("oauth")
          ? () => remove(provider)
          : undefined}
      />)}
      {showOthers ? <>
        {connected.length ? <p className="provider-group-label">Other providers</p> : null}
        {others.map(provider => <ProviderAccountRow
          key={provider.name}
          provider={provider}
          connected={false}
          external={providerMethods(provider).includes("external")}
          onSelect={() => select(provider)}
        />)}
      </> : null}
      {collapsible ? <button className="provider-disclosure" type="button" aria-expanded={showAll} onClick={() => setShowAll(value => !value)}>
        {showAll ? "Collapse" : "Connect another provider"} <span aria-hidden="true">⌄</span>
      </button> : null}
    </div>
    <OAuthDialog providers={accountProviders}/>
  </section>;
}


export function ProviderKeys() {
  const {config} = usePlatformState();
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState("");
  const providers = useMemo(() => (config.providers || []).filter(provider => (
    providerMethods(provider).includes("api_key") && !provider.custom
  )), [config.providers]);
  const filtered = providers.filter(provider => {
    const needle = query.trim().toLowerCase();
    if (!needle) return true;
    return [provider.name, provider.display_name, provider.description, ...(provider.credential_env_vars || [])]
      .some(value => String(value || "").toLowerCase().includes(needle));
  });

  return <section className="provider-simple-page">
    <button className="provider-local-endpoint-row" type="button" onClick={() => selectSettingsCategory("custom-endpoints")}>
      <span><strong>Local / custom endpoint</strong><small>Connect any OpenAI-compatible server.</small></span><b aria-hidden="true">›</b>
    </button>
    <label className="provider-search">
      <span aria-hidden="true">⌕</span>
      <input value={query} onChange={event => setQuery(event.target.value)} placeholder="Search API keys" aria-label="Search API-key providers"/>
    </label>
    <div className="provider-key-list">
      {filtered.map(provider => <ProviderKeyRow
        key={provider.name}
        provider={provider}
        credentials={config.credentials_by_provider?.[provider.name] || []}
        expanded={expanded === provider.name}
        onToggle={() => setExpanded(current => current === provider.name ? "" : provider.name)}
      />)}
      {!filtered.length ? <p className="provider-empty">No providers match “{query}”.</p> : null}
    </div>
  </section>;
}


type EndpointForm = {
  id: string;
  name: string;
  baseUrl: string;
  model: string;
  apiKey: string;
  contextLength: string;
  discoverModels: boolean;
  makeDefault: boolean;
  models: string[];
};

const EMPTY_ENDPOINT: EndpointForm = {
  id: "", name: "", baseUrl: "", model: "", apiKey: "",
  contextLength: "", discoverModels: true, makeDefault: true, models: [],
};

type EndpointOperation = "validate" | "save" | "activate" | "remove";
type PendingEndpointOperation = Readonly<{
  requestId: string;
  operation: EndpointOperation;
  fingerprint: string;
  targetId: string;
}>;

function endpointPayload(form: EndpointForm) {
  return {
    id: form.id || undefined,
    name: form.name.trim(),
    base_url: form.baseUrl.trim(),
    model: form.model.trim(),
    api_key: form.apiKey.trim() || undefined,
    context_length: Number(form.contextLength) || undefined,
    discover_models: form.discoverModels,
    make_default: form.makeDefault,
    models: form.models,
  };
}

function endpointFingerprint(form: EndpointForm): string {
  return JSON.stringify(endpointPayload(form));
}

function endpointForm(endpoint: CustomEndpoint): EndpointForm {
  return {
    id: endpoint.id,
    name: endpoint.name,
    baseUrl: endpoint.base_url,
    model: endpoint.model,
    apiKey: "",
    contextLength: endpoint.context_length ? String(endpoint.context_length) : "",
    discoverModels: endpoint.discover_models,
    makeDefault: endpoint.is_current,
    models: endpoint.models || [],
  };
}

export function CustomEndpointsPanel() {
  const {
    config,
    customEndpointReceipt,
  } = usePlatformState();
  const endpoints = config.custom_endpoints || [];
  const [form, setForm] = useState<EndpointForm>(EMPTY_ENDPOINT);
  const [persistedId, setPersistedId] = useState("");
  const [pending, setPending] = useState<PendingEndpointOperation | null>(null);
  const [validation, setValidation] = useState<NonNullable<typeof customEndpointReceipt>["validation"]>(null);
  const [operationError, setOperationError] = useState("");

  useEffect(() => {
    if (!pending || customEndpointReceipt?.requestId !== pending.requestId) return;
    const unchanged = pending.fingerprint === endpointFingerprint(form);
    if (!customEndpointReceipt.accepted) {
      setOperationError(customEndpointReceipt.error || "Custom endpoint operation failed");
    } else if (pending.operation === "validate" && unchanged && customEndpointReceipt.validation) {
      const result = customEndpointReceipt.validation;
      setValidation(result);
      if (result.ok) setForm(current => ({
        ...current,
        models: result.models || [],
        model: current.model || result.models?.[0] || "",
      }));
    } else if (pending.operation === "save" && customEndpointReceipt.endpoint) {
      const saved = customEndpointReceipt.endpoint;
      setPersistedId(saved.id);
      setForm(current => unchanged ? endpointForm(saved) : {...current, id: saved.id});
      notifyToast("Custom endpoint saved");
    } else if (pending.operation === "activate") {
      notifyToast("Custom endpoint selected for new cloud chats");
    } else if (pending.operation === "remove" && customEndpointReceipt.removed) {
      if (unchanged && form.id === pending.targetId) { setForm(EMPTY_ENDPOINT); setPersistedId(""); }
      notifyToast(customEndpointReceipt.fallbackMode === "local"
        ? "Endpoint deleted; new chats now default to Local"
        : "Custom endpoint deleted");
    }
    setPending(null);
  }, [customEndpointReceipt, form, pending]);

  function edit(next: EndpointForm) {
    setValidation(null);
    setOperationError("");
    setForm(next);
  }

  function begin(operation: EndpointOperation, message: {type: string; [key: string]: unknown}, targetId = "") {
    const requestId = `custom-endpoint-${operation}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    clearCustomEndpointFeedback();
    setValidation(null);
    setOperationError("");
    if (!send({...message, request_id: requestId})) {
      notifyToast(`Could not ${operation} custom endpoint — backend offline`);
      return false;
    }
    setPending({requestId, operation, fingerprint: endpointFingerprint(form), targetId});
    return true;
  }

  return <div className="custom-endpoints-page">
    <section className="custom-endpoints-section">
      <header className="provider-section-heading">
        <div><span className="deck-instrument__eyebrow">ENDPOINTS</span><h3>Custom Endpoints</h3></div>
        <span className="platform-badge">{endpoints.length}</span>
      </header>
      <div className="custom-endpoint-list">
        {endpoints.map(endpoint => <article className="custom-endpoint-row" key={endpoint.id}>
          <button type="button" onClick={() => {
            clearCustomEndpointFeedback();
            setPersistedId(endpoint.id);
            edit(endpointForm(endpoint));
          }} disabled={!!pending}>
            <span className="custom-endpoint-row__title"><strong>{endpoint.name}</strong>{endpoint.is_current ? <em>✓ Active</em> : null}</span>
            <code>{endpoint.base_url}</code>
            <small>{endpoint.model}{endpoint.has_api_key ? " · API key set" : ""}</small>
          </button>
          <div className="custom-endpoint-row__actions">
            <Button tone="quiet" disabled={endpoint.is_current || !!pending} onClick={() => begin("activate", {type: "cloud:custom-endpoint:activate", id: endpoint.id}, endpoint.id)}>Use</Button>
            <button type="button" disabled={!!pending} title="Delete endpoint" aria-label={`Delete ${endpoint.name}`} onClick={() => {
              if (window.confirm(`Delete ${endpoint.name}?`)) {
                begin("remove", {type: "cloud:custom-endpoint:remove", id: endpoint.id}, endpoint.id);
              }
            }}>×</button>
          </div>
        </article>)}
        {!endpoints.length ? <div className="custom-endpoint-empty"><strong>No custom endpoints</strong><span>Add an OpenAI-compatible endpoint below.</span></div> : null}
      </div>
    </section>

    <section className="custom-endpoints-section">
      <header className="provider-section-heading">
        <div><span className="deck-instrument__eyebrow">{persistedId ? "EDIT" : "ADD"}</span><h3>{persistedId ? "Edit Endpoint" : "Add Endpoint"}</h3></div>
      </header>
      <div className="custom-endpoint-editor">
        <div className="provider-editor-grid">
          <label className="platform-field"><span>Name</span><input value={form.name} onChange={event => edit({...form, name: event.target.value})} placeholder="Axet Proxy"/></label>
          <label className="platform-field"><span>Provider ID</span><input value={form.id} disabled={!!persistedId || !!pending} onChange={event => edit({...form, id: event.target.value})} placeholder="axet-proxy"/></label>
        </div>
        <label className="platform-field"><span>Endpoint URL</span><input value={form.baseUrl} onChange={event => edit({...form, baseUrl: event.target.value})} placeholder="http://127.0.0.1:8081/v1"/></label>
        <div className="custom-endpoint-model-grid">
          <label className="platform-field"><span>Default Model</span><input list="custom-endpoint-models" value={form.model} onChange={event => edit({...form, model: event.target.value})} placeholder="gpt-5.4"/>
            <datalist id="custom-endpoint-models">{form.models.map(model => <option key={model} value={model}/>)}</datalist>
          </label>
          <label className="platform-field"><span>Context</span><input inputMode="numeric" value={form.contextLength} onChange={event => edit({...form, contextLength: event.target.value})} placeholder="Auto"/></label>
        </div>
        <label className="platform-field"><span>API Key</span><input type="password" value={form.apiKey} onChange={event => edit({...form, apiKey: event.target.value})} placeholder={persistedId ? "Leave blank to keep current key" : "Optional"}/></label>
        <div className="custom-endpoint-options">
          <label><input type="checkbox" checked={form.makeDefault} onChange={event => edit({...form, makeDefault: event.target.checked})}/> Use for new chats</label>
          <label><input type="checkbox" checked={form.discoverModels} onChange={event => edit({...form, discoverModels: event.target.checked})}/> Discover models</label>
        </div>
        {validation ? <p className={validation.ok ? "provider-validation provider-validation--ok" : "provider-validation"}>{validation.message}</p> : null}
        {operationError ? <p className="runtime-error">{operationError}</p> : null}
        <div className="provider-form-actions">
          <Button tone="quiet" disabled={!!pending || !form.name.trim() || !form.baseUrl.trim()} onClick={() => begin("validate", {type: "cloud:custom-endpoint:validate", endpoint: endpointPayload(form)})}>{pending?.operation === "validate" ? "Testing…" : "Test"}</Button>
          <Button tone="primary" disabled={!!pending || !form.name.trim() || !form.baseUrl.trim() || !form.model.trim()} onClick={() => begin("save", {type: "cloud:custom-endpoint:save", endpoint: endpointPayload(form)})}>{pending?.operation === "save" ? "Saving…" : "Save"}</Button>
          {form.id ? <Button tone="quiet" disabled={!!pending} onClick={() => {
            clearCustomEndpointFeedback();
            setPersistedId("");
            edit(EMPTY_ENDPOINT);
          }}>New endpoint</Button> : null}
        </div>
      </div>
    </section>
  </div>;
}


function humanBytes(value: number): string {
  if (!value) return "";
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(1)} GB`;
  return `${Math.max(1, Math.round(value / 1024 ** 2))} MB`;
}


export function LocalRuntimePanel() {
  const {config} = usePlatformState();
  const platform = (config.inference_platform || {}) as unknown as InferencePlatform;
  const status = (platform.local_runtime || {}) as Partial<LocalRuntimeStatus>;
  const jobs = (platform.install_jobs || []) as InstallJob[];
  const activeJob = jobs.find(item => item.runtime_id === "llamacpp" && !["done", "error", "cancelled"].includes(item.status));
  const latestJob = jobs.find(item => item.runtime_id === "llamacpp");
  const failedJob = latestJob?.status === "error" ? latestJob : undefined;
  const [backend, setBackend] = useState("auto");
  const installed = !!status.installed;
  const managedInstalled = !!status.managed_installed;
  const bundledActive = status.install_source === "bundled" || !!status.bundled_active;
  const customActive = status.install_source === "custom" || !!status.custom_active;
  const pendingRestart = !!status.pending_restart;
  const runningBinary = status.running_binary || "";
  const selectedBinary = status.configured_binary || status.active_binary || "";
  const operation = managedInstalled ? "update" : "install";
  const statusLabel = pendingRestart ? "Restart pending" : status.managed_active ? "Managed" : bundledActive ? "Bundled" : customActive ? "Custom" : managedInstalled ? "Installed" : "Not downloaded";

  return <section className="provider-runtime deck-instrument">
    <header className="deck-instrument__header">
      <div><span className="deck-instrument__eyebrow">LOCAL RUNTIME</span><h3 className="deck-instrument__title">llama.cpp engine</h3></div>
      <span className={installed ? "platform-badge platform-badge--ready" : "platform-badge"}>{statusLabel}</span>
    </header>
    <p className="platform-note">Install or update the llama.cpp runtime here. Manage model files and downloads below.</p>
    <div className="provider-runtime__facts deck-data-list">
      <div className="deck-data-row"><span><strong>Release</strong><small>{bundledActive ? "Packaged llama.cpp fallback" : customActive ? "Custom llama.cpp executable" : status.version || status.tag || "Pinned by this VARIANT-1 build"}</small></span><em>{status.backend || status.recommended_backend || "auto"}</em></div>
      <div className="deck-data-row"><span><strong>Runtime binary</strong><small><code>{runningBinary || selectedBinary || status.binary || status.runtime_root || "runtime\\llamacpp"}</code></small></span><em>{runningBinary ? "Running" : selectedBinary ? "Selected" : managedInstalled ? "Installed" : "Empty"}</em></div>
      {pendingRestart && selectedBinary && selectedBinary !== runningBinary ? <div className="deck-data-row"><span><strong>Next binary</strong><small><code>{selectedBinary}</code></small></span><em>Restart pending</em></div> : null}
    </div>
    {activeJob ? <div className="provider-runtime__progress" role="status">
      <div><strong>{activeJob.step || "Installing llama.cpp"}</strong><span>{activeJob.progress || 0}%</span></div>
      <progress max="100" value={activeJob.progress || 0}/>
      <small>{activeJob.done_bytes ? `${humanBytes(activeJob.done_bytes)}${activeJob.total_bytes ? ` of ${humanBytes(activeJob.total_bytes)}` : ""}` : activeJob.target_label}</small>
      <Button tone="quiet" onClick={() => send({type: "inference:install:cancel", id: activeJob.id})}>Cancel</Button>
    </div> : <div className="provider-runtime__actions">
      {failedJob ? <p className="runtime-error" role="alert">{failedJob.error || failedJob.step || "The last llama.cpp installation failed."}</p> : null}
      <label className="platform-field"><span>Compute backend</span><select value={backend} onChange={event => setBackend(event.target.value)}>
        <option value="auto">Auto ({status.recommended_backend || "best available"})</option>
        <option value="cuda">NVIDIA CUDA</option>
        <option value="vulkan">Vulkan</option>
        <option value="cpu">CPU</option>
      </select></label>
      <Button tone="primary" disabled={status.supported === false} onClick={() => send({
        type: "inference:install",
        runtime_id: "llamacpp",
        target_id: "managed-binary:llamacpp",
        operation,
        backend,
      })}>{managedInstalled ? "Repair or update runtime" : bundledActive || customActive ? "Replace with managed build" : "Download llama.cpp"}</Button>
      <Button tone="quiet" onClick={() => send({type: "inference:platform:get"})}>Refresh status</Button>
    </div>}
  </section>;
}
