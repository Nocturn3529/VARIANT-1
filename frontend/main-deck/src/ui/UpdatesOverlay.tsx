import {useState, type CSSProperties, type ReactNode} from "react";
import {BrandMark} from "./BrandMark";
import {Button} from "./Button";
import {CopyButton} from "./CopyButton";
import {Icon} from "./Icon";
import {Overlay} from "./Overlay";
import {BrailleSpinner} from "../motion/BrailleSpinner";
import {useChatSelection} from "../chatStore";
import {
  cancelUpdateDownload,
  checkForUpdates,
  closeUpdatesModal,
  downloadUpdate,
  installUpdate,
  openUpdateRelease,
  useAboutState,
} from "../aboutStore";
import type {UpdateState} from "../types";

function megabytes(bytes: number): string {
  return `${(bytes / 1048576).toFixed(bytes >= 104857600 ? 0 : 1)} MB`;
}

/** How to install a release this build can't apply in place. */
export function manualInstallNote(update: UpdateState): string {
  if (update.platform === "darwin") return "This preview isn't signed by Apple, so it can't replace itself. Download the new version from its release page and replace VARIANT-1 in Applications. If macOS blocks the first launch, allow it under System Settings › Privacy & Security.";
  if (update.platform === "linux") return "Download the new AppImage or .deb from its release page and install it the way you installed this one.";
  return "Download the new installer from its release page.";
}

/** Split an update failure into a plain-English lead sentence and a monospace technical detail. */
export function splitUpdateError(raw: string | null | undefined, fallbackLead: string): {lead: string; detail: string} {
  const value = (raw || "").trim();
  if (!value) return {lead: fallbackLead, detail: ""};
  const marker = value.search(/\n?\s*Details:\s*/);
  if (marker >= 0) {
    const lead = value.slice(0, marker).trim() || fallbackLead;
    const detail = value.slice(marker).replace(/^\s*Details:\s*/, "").trim();
    return {lead, detail};
  }
  const technical = /net::|ECONN|ENOTFOUND|ETIMEDOUT|EACCES|EPERM|ERR_|HTTP\s+\d{3}|\bstatus\s+\d{3}\b|[A-Za-z]+Error:|[/\\]/i.test(value);
  if (technical) return {lead: fallbackLead, detail: value};
  return {lead: value, detail: ""};
}

/**
 * Shared brand + version + channel/build hero for Settings › About and the
 * compact updates modal.
 */
export function VersionHero({version, packaged, updateLabel, update, mutation = false, compact = false, actions, renderTitle}: {
  version: string;
  packaged: boolean;
  updateLabel: string;
  update?: UpdateState | null;
  mutation?: boolean;
  compact?: boolean;
  actions?: ReactNode;
  renderTitle?: (title: string) => ReactNode;
}) {
  const currentVersion = version || update?.currentVersion || "";
  const channelBadge = compact
    ? (packaged ? "Installed build" : version || update?.reason === "dev_mode" ? "Development build" : "Checking build…")
    : (updateLabel || (packaged ? "Installed build" : "Development build"));
  const phase = update?.status === "checking" || update?.status === "downloading" || update?.status === "installing" ? "running" : "idle";

  return <div className={`about-hero version-hero ${compact ? "version-hero--compact" : "deck-instrument"}`.trim()}>
    <span className="about-hero__mark version-hero__mark" aria-hidden="true">
      <BrandMark seed="variant-1" size={28} phase={phase} mutation={mutation}/>
    </span>
    <div className="version-hero__copy">
      <div className="version-hero__headline">
        {renderTitle ? renderTitle("VARIANT-1") : <h3>VARIANT-1</h3>}
        <span className="version-hero__channel">{channelBadge}</span>
      </div>
      {!compact ? <p>Adaptive AI Host · Any model. One persistent host. Tools that adapt.</p> : null}
      <small>Main Deck build {currentVersion || "…"}</small>
    </div>
    {!compact ? <em>{channelBadge}</em> : null}
    {actions ? <div className="version-hero__actions">{actions}</div> : null}
  </div>;
}

/**
 * Reusable update status card shared by Settings › About and the compact
 * updates dialog. Finding an update never downloads on its own; download,
 * cancel and restart-to-install happen only when the user clicks.
 */
export function UpdateStatusCard({update, logs = [], checking = false, inModal = false, onOpenModal}: {
  update: UpdateState;
  logs?: readonly string[];
  checking?: boolean;
  inModal?: boolean;
  onOpenModal?: () => void;
}) {
  const [logExpanded, setLogExpanded] = useState(false);
  if (!inModal && (update.status === "unavailable" || update.status === "idle")) return null;

  const busy = checking || update.status === "checking" || update.status === "installing";
  const lastChecked = update.checkedAt ? new Date(update.checkedAt).toLocaleString([], {dateStyle: "medium", timeStyle: "short"}) : "";
  const percent = Math.max(0, Math.min(100, Math.round(update.percent || 0)));

  let title = "";
  let detail = "";
  let technicalDetail = "";
  let actions: ReactNode = null;

  if (update.status === "unavailable") {
    title = update.reason === "dev_mode" ? "Development build"
      : update.reason === "unconfigured" ? "Update feed not configured" : "Updates unavailable";
    detail = update.reason === "dev_mode"
      ? "In-app updates run in packaged builds. Packaged releases check GitHub releases automatically."
      : "This build cannot check the update feed right now.";
    actions = update.releaseUrl ? <Button tone="quiet" onClick={openUpdateRelease}>Open releases</Button> : null;
  } else if (update.status === "up-to-date" || update.status === "checking" || update.status === "idle") {
    const isChecking = checking || update.status === "checking";
    title = isChecking ? "Checking for updates…" : `VARIANT-1 ${update.currentVersion || ""} is up to date`.replace(/\s+/g, " ").trim();
    detail = lastChecked ? `Last checked ${lastChecked}. VARIANT-1 checks again every 24 hours.` : "VARIANT-1 checks for updates every 24 hours.";
    actions = <Button tone="quiet" disabled={busy} onClick={() => { void checkForUpdates(); }}>
      {isChecking ? "Checking…" : "Check for updates"}
    </Button>;
  } else if (update.status === "available") {
    title = `VARIANT-1 ${update.version} is available`;
    detail = update.installMode === "in-app"
      ? `You have ${update.currentVersion}. Nothing is downloaded until you choose to.`
      : manualInstallNote(update);
    actions = update.installMode === "in-app"
      ? <>
          <Button tone="primary" onClick={() => { void downloadUpdate(); }}>Download update</Button>
          <Button tone="quiet" onClick={openUpdateRelease}>Release notes</Button>
        </>
      : <>
          <Button tone="primary" onClick={openUpdateRelease}>Open release page</Button>
          {update.releaseUrl ? <CopyButton className="about-update__url-pill" text={update.releaseUrl} label="Copy release URL" title={update.releaseUrl}/> : null}
        </>;
  } else if (update.status === "downloading") {
    title = `Downloading VARIANT-1 ${update.version}`;
    detail = update.total ? `${megabytes(update.transferred)} of ${megabytes(update.total)}` : "Starting download…";
    actions = <Button tone="quiet" onClick={cancelUpdateDownload}>Cancel</Button>;
  } else if (update.status === "downloaded" || update.status === "installing") {
    title = update.status === "installing" ? "Restarting to install…" : `VARIANT-1 ${update.version} is ready to install`;
    if (update.status === "downloaded" && update.error) {
      const split = splitUpdateError(update.error, "VARIANT-1 could not start the installer.");
      detail = `${split.lead} The downloaded update is kept, so you can try again.`;
      technicalDetail = split.detail;
    } else {
      detail = "VARIANT-1 closes, installs the update and opens again. Your chats, tabs and settings are kept.";
    }
    actions = <>
      <Button tone="primary" disabled={busy} onClick={() => { void installUpdate(); }}>
        {update.error && update.status === "downloaded" ? "Try installing again" : "Restart and install"}
      </Button>
      {update.releaseUrl ? <Button tone="quiet" disabled={busy} onClick={openUpdateRelease}>Release notes</Button> : null}
    </>;
  } else if (update.status === "error") {
    title = "The last update check didn't finish";
    const split = splitUpdateError(update.error, "VARIANT-1 couldn't reach GitHub releases.");
    detail = split.lead;
    technicalDetail = split.detail;
    actions = <>
      <Button tone="quiet" onClick={() => { void checkForUpdates(); }}>Try again</Button>
      <Button tone="quiet" onClick={openUpdateRelease}>Open releases</Button>
    </>;
  }

  const hasReleaseAction = Boolean(
    update.releaseUrl && (
      update.status === "available"
      || update.status === "downloaded"
      || update.status === "installing"
      || update.status === "error"
      || update.status === "unavailable"
    ),
  );
  const showVersionTag = Boolean(update.version && !title.includes(update.version));
  const showReleaseName = Boolean(update.releaseName && update.releaseName !== update.version && !title.includes(update.releaseName));
  const showNotesLink = Boolean(update.releaseUrl && !hasReleaseAction);
  const showReleaseMeta = showVersionTag || showReleaseName || showNotesLink;
  const fullLogText = logs.join("\n");

  return <section className={`about-card about-card--update update-status-card ${inModal ? "update-status-card--modal" : "deck-instrument"}`.trim()}
    data-update-status={update.status} aria-label="Updates">
    <header className="deck-instrument__header update-status-card__header">
      <div className="update-status-card__heading">
        <span className="about-kicker deck-instrument__eyebrow">UPDATES</span>
        <h3 className="deck-instrument__title" role="status">
          {(checking || update.status === "checking" || update.status === "installing") ? <BrailleSpinner className="update-status-card__spinner"/> : null}
          <span>{title}</span>
        </h3>
      </div>
      {!inModal && onOpenModal ? <Button tone="quiet" onClick={onOpenModal}>Open updates</Button> : null}
    </header>

    {showReleaseMeta ? <div className="about-update__meta">
      {showVersionTag ? <span className="about-update__tag">v{update.version.replace(/^v/i, "")}</span> : null}
      {showReleaseName ? <span className="about-update__release-name">{update.releaseName}</span> : null}
      {showNotesLink ? <button type="button" className="about-update__notes-link" aria-label="View release notes on GitHub" onClick={openUpdateRelease}>
        <span>Release notes</span>
        <Icon name="popout"/>
      </button> : null}
    </div> : null}

    {update.status !== "downloading" ? <p className="about-update__detail">{detail}</p> : null}
    {technicalDetail ? <pre className="about-update__error-detail" role="note"><code>{technicalDetail}</code></pre> : null}

    {update.status === "downloading" ? <div className="about-update__progress-block">
      <div className="about-update__progress-readout">
        <span className="about-update__bytes">{detail}</span>
        <span className="about-update__percent">{percent}%</span>
      </div>
      <div className="about-update__track" aria-hidden="true">
        <span className="about-update__fill" style={{"--update-progress": `${percent}%`} as CSSProperties}/>
      </div>
      <progress className="about-update__progress" max={100} value={update.percent} aria-label="Update download progress"/>
    </div> : null}

    {actions ? <div className="about-update__actions">{actions}</div> : null}

    {logs.length > 0 ? <div className="about-update__log-drawer">
      <div className="about-update__log-bar">
        <button type="button" className="about-update__log-toggle" aria-expanded={logExpanded} onClick={() => setLogExpanded(open => !open)}>
          <Icon name={logExpanded ? "down" : "chevron"}/>
          <span>{logExpanded ? "Hide status log" : "Status log"}</span>
          <span className="about-update__log-count">({logs.length})</span>
        </button>
        {logExpanded ? <CopyButton text={fullLogText} label="Copy full log"/> : null}
      </div>
      {logExpanded ? <pre className="about-update__log" aria-label="Update status log"><code>{fullLogText}</code></pre> : null}
    </div> : null}
  </section>;
}

/**
 * Compact modal opened from the titlebar update pill (or "Open updates" in
 * Settings › About). Closing is locked while an update is actively restarting
 * to install so the user never loses sight of an in-flight install.
 */
export function UpdatesOverlay() {
  const {version, packaged, updateLabel, updateChecking, update, updatesModalOpen, updateLogs} = useAboutState();
  const mutation = useChatSelection(chat=>!!chat.runtime?.mutationEffectiveEnabled);
  if (!updatesModalOpen) return null;

  const installing = update?.status === "installing";
  const effectiveUpdate: UpdateState = update || {
    status: updateChecking || !version ? "checking" : packaged ? "up-to-date" : "unavailable",
    reason: packaged ? "" : "dev_mode",
    currentVersion: version || "",
    platform: "",
    version: "",
    releaseName: "",
    releaseUrl: "",
    installMode: "in-app",
    percent: 0,
    transferred: 0,
    total: 0,
    error: "",
    checkedAt: 0,
  };

  return <Overlay open={updatesModalOpen} onClose={closeUpdatesModal} labelledBy="updates-overlay-title"
    className="updates-overlay" closeOnSurfaceClick={!installing}>
    <header className="deck-overlay__header updates-overlay__header">
      <h1 id="updates-overlay-title">Software updates</h1>
      <button type="button" className="updates-overlay__close" aria-label="Close software updates"
        disabled={installing} onClick={closeUpdatesModal}>
        <Icon name="close"/>
      </button>
    </header>
    <div className="updates-overlay__body">
      <VersionHero
        version={version}
        packaged={packaged}
        updateLabel={updateLabel}
        update={effectiveUpdate}
        mutation={mutation}
        compact
      />
      <UpdateStatusCard
        update={effectiveUpdate}
        logs={updateLogs}
        checking={updateChecking}
        inModal
      />
    </div>
  </Overlay>;
}
