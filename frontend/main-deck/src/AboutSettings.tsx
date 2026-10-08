/**
 * About Settings panel.
 *
 * Build info, update check, system health / doctor findings, and local paths.
 */
import {Button} from "./ui/Button";
import {KernelGlyph} from "./motion/KernelGlyph";
import {useChatState} from "./chatStore";
import {
  cancelUpdateDownload,
  checkForUpdates,
  downloadUpdate,
  installUpdate,
  openAboutPath,
  openUpdateRelease,
  refreshAbout,
  runHealthCheck,
  useAboutState,
} from "./aboutStore";
import type {ReactNode} from "react";
import type {UpdateState} from "./types";

function megabytes(bytes: number): string {
  return `${(bytes / 1048576).toFixed(bytes >= 104857600 ? 0 : 1)} MB`;
}

/** How to install a release this build can't apply in place. */
function manualInstallNote(update: UpdateState): string {
  if (update.platform === "darwin") return "This preview isn't signed by Apple, so it can't replace itself. Download the new version from its release page and replace VARIANT-1 in Applications. If macOS blocks the first launch, allow it under System Settings › Privacy & Security.";
  if (update.platform === "linux") return "Download the new AppImage or .deb from its release page and install it the way you installed this one.";
  return "Download the new installer from its release page.";
}

/** Updates are found automatically; downloading and installing wait for the user. */
function UpdateCard({update}: {update: UpdateState}) {
  if (update.status === "unavailable" || update.status === "idle") return null;
  const busy = update.status === "checking" || update.status === "installing";
  const lastChecked = update.checkedAt ? new Date(update.checkedAt).toLocaleString([], {dateStyle: "medium", timeStyle: "short"}) : "";
  let title = "", detail = "";
  let actions: ReactNode = null;
  if (update.status === "up-to-date" || update.status === "checking") {
    title = update.status === "checking" ? "Checking for updates…" : `VARIANT-1 ${update.currentVersion} is up to date`;
    detail = lastChecked ? `Last checked ${lastChecked}. VARIANT-1 checks again every 24 hours.` : "VARIANT-1 checks for updates every 24 hours.";
  } else if (update.status === "available") {
    title = `VARIANT-1 ${update.version} is available`;
    detail = update.installMode === "in-app"
      ? `You have ${update.currentVersion}. Nothing is downloaded until you choose to.`
      : manualInstallNote(update);
    actions = update.installMode === "in-app"
      ? <><Button tone="primary" onClick={() => { void downloadUpdate(); }}>Download update</Button>
        <Button tone="quiet" onClick={openUpdateRelease}>Release notes</Button></>
      : <Button tone="primary" onClick={openUpdateRelease}>Open release page</Button>;
  } else if (update.status === "downloading") {
    title = `Downloading VARIANT-1 ${update.version}`;
    detail = update.total ? `${megabytes(update.transferred)} of ${megabytes(update.total)}` : "Starting download…";
    actions = <Button tone="quiet" onClick={cancelUpdateDownload}>Cancel</Button>;
  } else if (update.status === "downloaded" || update.status === "installing") {
    title = update.status === "installing" ? "Restarting to install…" : `VARIANT-1 ${update.version} is ready to install`;
    detail = "VARIANT-1 closes, installs the update and opens again. Your chats, tabs and settings are kept.";
    actions = <Button tone="primary" disabled={busy} onClick={() => { void installUpdate(); }}>Restart and install</Button>;
  } else if (update.status === "error") {
    title = "The last update check didn't finish";
    detail = update.error || "VARIANT-1 couldn't reach GitHub releases.";
    actions = <><Button tone="quiet" onClick={() => { void checkForUpdates(); }}>Try again</Button>
      <Button tone="quiet" onClick={openUpdateRelease}>Open releases</Button></>;
  }
  return <section className="about-card about-card--update deck-instrument" data-update-status={update.status} aria-label="Updates">
    <header className="deck-instrument__header">
      <div>
        <span className="about-kicker deck-instrument__eyebrow">UPDATES</span>
        <h3 className="deck-instrument__title" role="status">{title}</h3>
      </div>
    </header>
    <p className="about-update__detail">{detail}</p>
    {update.status === "downloading" ? <progress className="about-update__progress" max={100} value={update.percent} aria-label="Update download progress"/> : null}
    {actions ? <div className="about-update__actions">{actions}</div> : null}
  </section>;
}

const PATH_ROWS: Array<{key: string; title: string; openLabel: string}> = [
  {key: "config", title: "Configuration", openLabel: "Open folder"},
  {key: "dataDir", title: "Application data", openLabel: "Open folder"},
  {key: "logs", title: "Logs", openLabel: "Open folder"},
];

export function AboutSettings() {
  const {version, packaged, updateLabel, updateChecking, update, paths, health, findings, connected} = useAboutState();
  const mutation = !!useChatState().runtime?.mutationEffectiveEnabled;

  const healthCards = [
    {key: "backend", label: "BACKEND", title: health.backend, detail: health.backendDetail},
    {key: "model", label: "MODEL RUNTIME", title: health.model, detail: health.modelDetail},
    {key: "context", label: "SESSION CONTEXT", title: health.sessionContext, detail: health.sessionContextDetail},
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

    {update ? <UpdateCard update={update}/> : null}

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
