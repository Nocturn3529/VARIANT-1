/** Registry-driven web-search Settings surface. */
import {useEffect, useMemo, useState, type FormEvent} from "react";
import {
  notifyAgentTools,
  refreshAgentTools,
  sendAgentTools,
  useAgentToolsState,
} from "./agentToolsStore";
import type {WebSearchProvider, WebSearchProviderInfo} from "./types";
import {Button} from "./ui/Button";
import {Switch} from "./ui/Switch";

function providerStatus(provider: WebSearchProviderInfo): string {
  if (provider.active) return "Active";
  if (provider.configured) return "Ready";
  if (provider.keyless || provider.auth === "none" || provider.auth === "endpoint") return "Available";
  return "Needs setup";
}

function ProviderListRow({
  provider,
  selected,
  onSelect,
}: {
  provider: WebSearchProviderInfo;
  selected: boolean;
  onSelect: () => void;
}) {
  return <button
    type="button"
    className={selected ? "search-provider-row selected" : "search-provider-row"}
    onClick={onSelect}
  >
    <span><strong>{provider.name}</strong><small>{provider.description}</small></span>
    <em data-state={provider.active ? "active" : provider.available ? "ready" : "setup"}>
      {providerStatus(provider)}
    </em>
  </button>;
}

function CredentialEditor({provider}: {provider: WebSearchProviderInfo}) {
  const {toolsReceipt} = useAgentToolsState();
  const [draft, setDraft] = useState("");
  const [requestId, setRequestId] = useState("");

  useEffect(() => {
    if (!requestId || toolsReceipt?.requestId !== requestId) return;
    if (toolsReceipt.status === "accepted") {
      setDraft("");
      notifyAgentTools(`${provider.name} credential saved`);
    } else {
      notifyAgentTools(toolsReceipt.error || `Could not save ${provider.name} credential`);
    }
    setRequestId("");
  }, [provider.name, requestId, toolsReceipt]);

  function save(event: FormEvent) {
    event.preventDefault();
    const key = draft.trim();
    if (!key) return;
    const id = `web-key-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    if (!sendAgentTools({
      type: "web_search:credential:set", request_id: id,
      provider: provider.id, key,
    })) {
      notifyAgentTools("Could not save credential — backend offline");
      return;
    }
    setRequestId(id);
  }

  function clear() {
    const id = `web-key-clear-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    if (!sendAgentTools({
      type: "web_search:credential:clear", request_id: id,
      provider: provider.id,
    })) {
      notifyAgentTools("Could not remove credential — backend offline");
      return;
    }
    setRequestId(id);
  }

  return <section className="search-provider-section">
    <div className="search-provider-section__heading">
      <span><strong>API credential</strong><small>
        {provider.auth === "shared"
          ? "Uses the connected xAI account by default. A dedicated key may override it."
          : provider.keyless
            ? "Optional. Without a key, this provider uses its anonymous free tier."
            : "Required before this provider can be used."}
      </small></span>
      {provider.configured ? <em>Stored securely</em> : null}
    </div>
    <form className="search-provider-credential" onSubmit={save}>
      <input
        aria-label={`${provider.name} API key`}
        type="password"
        value={draft}
        onChange={event => setDraft(event.target.value)}
        placeholder={provider.configured ? "Enter a replacement key" : "Paste API key"}
        autoComplete="off"
        spellCheck={false}
      />
      <Button type="submit" tone="primary" disabled={!draft.trim() || !!requestId}>Save</Button>
      {provider.configured ? <Button tone="quiet" disabled={!!requestId} onClick={clear}>Remove</Button> : null}
    </form>
    {provider.signup_url ? <a href={provider.signup_url} target="_blank" rel="noreferrer">Get a key ↗</a> : null}
  </section>;
}

function SearxngEditor() {
  const state = useAgentToolsState();
  const [baseUrl, setBaseUrl] = useState(state.searxngBaseUrl);
  useEffect(() => setBaseUrl(state.searxngBaseUrl), [state.searxngBaseUrl]);

  function save(event: FormEvent) {
    event.preventDefault();
    sendAgentTools({
      type: "web_search:set",
      searxng: {base_url: baseUrl.trim() || "http://127.0.0.1:8888"},
    });
  }

  return <section className="search-provider-section">
    <div className="search-provider-section__heading">
      <span><strong>SearXNG server</strong><small>
        {state.searxngReady
          ? `Running at ${state.searxngBaseUrl}`
          : state.searxngDockerAvailable
            ? "Use an existing URL or let VARIANT-1 manage a Docker container."
            : "Enter an existing SearXNG URL. Docker was not detected."}
      </small></span>
      <em>{state.searxngRunning ? "Running" : state.searxngReady ? "Reachable" : "Stopped"}</em>
    </div>
    <form className="search-provider-credential" onSubmit={save}>
      <input aria-label="SearXNG base URL" value={baseUrl}
        onChange={event => setBaseUrl(event.target.value)} spellCheck={false}/>
      <Button type="submit" tone="quiet">Save</Button>
      <Button tone="quiet" disabled={!state.searxngDockerAvailable || !state.searxngManaged || state.searxngRunning}
        onClick={() => sendAgentTools({type: "searxng:start"})}>Start</Button>
      <Button tone="quiet" disabled={!state.searxngManaged || !state.searxngRunning || !state.searxngOwned}
        onClick={() => sendAgentTools({type: "searxng:stop"})}>Stop</Button>
    </form>
    {state.searxngError ? <p className="runtime-error" role="alert">{state.searxngError}</p> : null}
    <div className="search-provider-toggle">
      <span><strong>Start managed server with VARIANT-1</strong><small>Only applies to the managed Docker runtime.</small></span>
      <Switch checked={state.searxngAutostart} disabled={!state.searxngManaged}
        aria-label="Auto-start SearXNG"
        onChange={value => sendAgentTools({
          type: "web_search:set", searxng: {autostart: value},
        })}/>
    </div>
  </section>;
}

function VariantSearchStatus() {
  const state = useAgentToolsState();
  return <section className="search-provider-section">
    <div className="search-provider-section__heading">
      <span><strong>Built-in engines</strong><small>{state.variant1Engines.join(", ") || "DuckDuckGo and Bing"}</small></span>
      <em>{state.variant1SearchChecked
        ? `${state.variant1HealthyEngines}/${state.variant1EngineCount} healthy`
        : "Ready"}</em>
    </div>
    {state.variant1SearchChecked ? <p>
      Last search: {state.variant1SearchLatencyMs || 0} ms
      {state.variant1SearchCacheHit ? " · cache hit" : ""}
      {state.variant1SearchError ? ` · ${state.variant1SearchError}` : ""}
    </p> : null}
  </section>;
}

function ProviderDetail({provider}: {provider: WebSearchProviderInfo}) {
  const activate = () => sendAgentTools({type: "web_search:set", provider: provider.id});
  const needsCredential = ["api_key", "optional", "shared"].includes(provider.auth);
  return <article className="search-provider-detail">
    <header>
      <span><strong>{provider.name}</strong><small>{provider.description}</small></span>
      {provider.active
        ? <em className="search-provider-active">Active search provider</em>
        : <Button tone="primary" disabled={!provider.available} onClick={activate}>Use for search</Button>}
    </header>
    {provider.keyless ? <p className="search-provider-callout">A keyless route is available. Add a key only when you want the provider's paid or higher-limit service.</p> : null}
    {provider.id === "variant1" ? <VariantSearchStatus/> : null}
    {provider.id === "searxng" ? <SearxngEditor/> : null}
    {needsCredential ? <CredentialEditor provider={provider}/> : null}
  </article>;
}

export function AgentToolsSettings() {
  const state = useAgentToolsState();
  const providers = state.webSearchProviders;
  const [selectedId, setSelectedId] = useState<WebSearchProvider>(state.webSearchProvider);
  useEffect(() => {
    if (!providers.some(provider => provider.id === selectedId)) {
      setSelectedId(state.webSearchProvider);
    }
  }, [providers, selectedId, state.webSearchProvider]);
  const selected = useMemo(
    () => providers.find(provider => provider.id === selectedId)
      || providers.find(provider => provider.id === state.webSearchProvider)
      || providers[0],
    [providers, selectedId, state.webSearchProvider],
  );

  return <div className="search-settings">
    <div className="settings-status-bar">
      <span className={state.connected ? "tools-status tools-status--ready" : "tools-status tools-status--idle"}>
        <i/>{state.connected ? `${providers.length} providers` : "Waiting for backend"}
      </span>
      <Button tone="quiet" onClick={refreshAgentTools}>Refresh</Button>
    </div>
    <div className="search-provider-browser">
      <nav aria-label="Web-search providers">
        {providers.map(provider => <ProviderListRow key={provider.id}
          provider={provider} selected={selected?.id === provider.id}
          onSelect={() => setSelectedId(provider.id)}/>)}
      </nav>
      {selected ? <ProviderDetail provider={selected}/> : <p className="provider-empty">No search providers available.</p>}
    </div>
  </div>;
}
