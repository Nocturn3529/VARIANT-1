/**
 * About Settings panel.
 *
 * Build info, update check, system health / doctor findings, and local paths.
 */
import {Button} from "./ui/Button";
import {KernelGlyph} from "./motion/KernelGlyph";
import {useChatState} from "./chatStore";
import {
  checkForUpdates,
  openAboutPath,
  refreshAbout,
  runHealthCheck,
  useAboutState,
} from "./aboutStore";

const PATH_ROWS: Array<{key: string; title: string; openLabel: string}> = [
  {key: "config", title: "Configuration", openLabel: "Open folder"},
  {key: "dataDir", title: "Application data", openLabel: "Open folder"},
  {key: "logs", title: "Logs", openLabel: "Open folder"},
];

export function AboutSettings() {
  const {version, packaged, updateLabel, updateChecking, paths, health, findings, connected} = useAboutState();
  const mutation = !!useChatState().runtime?.mutationEffectiveEnabled;

  const healthCards = [
    {key: "backend", label: "BACKEND", title: health.backend, detail: health.backendDetail},
    {key: "model", label: "MODEL RUNTIME", title: health.model, detail: health.modelDetail},
    {key: "memory", label: "MEMORY", title: health.memory, detail: health.memoryDetail},
    {key: "scheduler", label: "WORK", title: health.scheduler, detail: health.schedulerDetail},
  ];

  return <div className="about-shell">
    <div className="about-status-bar settings-status-bar">
      <span className="about-status">
        <i className={connected ? "about-dot about-dot--ok" : "about-dot"} />
        {connected ? "Local session" : "Waiting for backend"}
      </span>
      <Button tone="quiet" disabled={updateChecking} onClick={() => { void checkForUpdates(); }}>
        {updateChecking ? "Checking…" : "Check for updates"}
      </Button>
    </div>

    <div className="about-hero deck-instrument">
      <span className="about-hero__mark" aria-hidden="true"><KernelGlyph seed="variant-1" size={28} phase="idle" mutation={mutation}/></span>
      <div>
        <h3>VARIANT-1</h3>
        <p>Adaptive AI Host · Any model. One persistent host. Tools that adapt.</p>
        <small>Main Deck build {version || "…"}</small>
      </div>
      <em>{updateLabel || (packaged ? "Installed build" : "Development build")}</em>
    </div>

    <section className="about-card about-card--health deck-instrument">
      <header className="deck-instrument__header">
        <div>
          <span className="about-kicker deck-instrument__eyebrow">SYSTEM HEALTH</span>
          <h3 className="deck-instrument__title">Core services for this session</h3>
        </div>
        <Button tone="quiet" onClick={runHealthCheck}>Run full health check</Button>
      </header>
      <div className="about-health deck-metric-rail">
        {healthCards.map(card => <article className="deck-metric" key={card.key}>
          <span className="deck-metric__label"><i />{card.label}</span>
          <strong className="deck-metric__value">{card.title}</strong>
          <small className="deck-metric__detail">{card.detail}</small>
        </article>)}
      </div>
      {findings !== null ? <div className="about-findings deck-data-list" aria-label="Health-check findings">
        {findings.length ? findings.map((item, index) => <article className="deck-data-row" key={`${item.title || "finding"}-${index}`}>
          <span><strong>{item.title || "Check"}</strong><small>{item.detail || ""}</small></span>
          <em>{String(item.level || "info").toUpperCase()}</em>
          {item.fix ? <p>{item.fix}</p> : null}
        </article>) : <article className="deck-data-row">
          <span><strong>All checks passed</strong><small>No findings were returned.</small></span>
        </article>}
      </div> : null}
    </section>

    <section className="about-card about-card--storage deck-instrument">
      <header className="deck-instrument__header">
        <div>
          <span className="about-kicker deck-instrument__eyebrow">LOCAL STORAGE</span>
          <h3 className="deck-instrument__title">Inspect or back up VARIANT-1 data</h3>
        </div>
        <Button tone="quiet" onClick={refreshAbout}>Refresh</Button>
      </header>
      <div className="about-paths deck-data-list">
        {PATH_ROWS.map(row => {
          const path = paths[row.key] || "";
          return <button
            key={row.key}
            type="button"
            className="about-path deck-data-row"
            disabled={!path}
            onClick={() => { void openAboutPath(row.key); }}
          >
            <span>
              <strong>{row.title}</strong>
              <small>{path || "Unavailable"}</small>
            </span>
            <em>{path ? row.openLabel : "Unavailable"}</em>
          </button>;
        })}
      </div>
    </section>
  </div>;
}
