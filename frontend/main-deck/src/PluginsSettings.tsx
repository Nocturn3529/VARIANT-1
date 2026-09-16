import {useMemo, useState} from "react";
import {
  openPluginsFolder,
  rescanPlugins,
  setPluginEnabled,
  usePluginsState,
  type PluginRow,
} from "./pluginsStore";
import {Button} from "./ui/Button";
import {SettingsSection} from "./ui/Settings";
import {Switch} from "./ui/Switch";

const KIND_LABELS: Readonly<Record<string, string>> = {
  capabilities: "Tools",
  skills: "Skills",
  mcp_server: "MCP",
  commands: "Commands",
  model_providers: "Models",
  messaging_adapters: "Messaging",
};

function kindsFor(plugin: PluginRow): string[] {
  const labels = plugin.contribution_kinds.map(kind => KIND_LABELS[kind] || kind);
  return labels.length ? labels : ["Resource"];
}

export function PluginsSettings() {
  const {connected, plugins, pending, loading, error, scanSummary} = usePluginsState();
  const [query, setQuery] = useState("");
  const scanning = Object.values(pending).includes("rescan");
  const shown = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    if (!needle) return plugins;
    return plugins.filter(plugin => [
      plugin.name,
      plugin.package_id,
      plugin.description,
      plugin.error,
      ...kindsFor(plugin),
    ].some(value => value.toLocaleLowerCase().includes(needle)));
  }, [plugins, query]);

  return <div className="plugins-settings">
    <SettingsSection
      eyebrow="LIBRARY"
      title="Installed plugins"
      description="Add capabilities by dropping a manifest-backed plugin into the folder."
      action={<div className="plugins-settings__actions">
        <Button tone="quiet" onClick={() => { void openPluginsFolder(); }}>Open folder</Button>
        <Button tone="quiet" disabled={!connected || scanning} onClick={rescanPlugins}>
          {scanning ? "Scanning…" : "Rescan"}
        </Button>
      </div>}
    >
      <div className="plugins-settings__summary">
        <span className={connected ? "is-online" : ""} aria-hidden="true" />
        <strong>{connected ? `${plugins.length} plugin${plugins.length === 1 ? "" : "s"}` : "Backend offline"}</strong>
        {scanSummary ? <small>{scanSummary}</small> : null}
      </div>
      <input
        className="plugins-settings__search"
        type="search"
        value={query}
        onChange={event => setQuery(event.target.value)}
        placeholder="Search plugins"
        spellCheck={false}
        aria-label="Search plugins"
      />
      {error ? <p className="plugins-settings__error" role="status">{error}</p> : null}
      <div className="plugins-settings__list" role="list">
        {shown.map(plugin => {
          const failed = plugin.status === "error" || !!plugin.error;
          return <div className={`plugins-settings__row${failed ? " is-error" : ""}`} role="listitem" key={plugin.package_id}>
            <div className="plugins-settings__copy">
              <div className="plugins-settings__name">
                <strong>{plugin.name}</strong>
                {plugin.version ? <span>v{plugin.version}</span> : null}
                {kindsFor(plugin).map(kind => <span key={kind}>{kind}</span>)}
              </div>
              <p>{failed ? plugin.error : (plugin.description || plugin.package_id)}</p>
            </div>
            {failed ? <span className="plugins-settings__failed">Failed</span> : <Switch
              checked={plugin.active}
              disabled={loading}
              onChange={enabled => setPluginEnabled(plugin.package_id, enabled)}
              aria-label={`${plugin.active ? "Disable" : "Enable"} ${plugin.name}`}
            />}
          </div>;
        })}
        {!shown.length ? <div className="plugins-settings__empty">
          {query ? "No matching plugins." : "No plugins installed. Add one to the plugins folder and rescan."}
        </div> : null}
      </div>
    </SettingsSection>
  </div>;
}
