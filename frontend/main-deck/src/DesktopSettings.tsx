/**
 * Settings › Desktop control: CuaDriver status and what each OS needs.
 */
import {useEffect} from "react";
import {Button} from "./ui/Button";
import {refreshDesktopStatus, requestDesktopPermissions, useDesktopStatus, type DesktopStatus} from "./desktopStore";

const STATE_LABEL: Record<DesktopStatus["driver"]["state"], string> = {
  running: "Running",
  stopped: "Ready",
  error: "Needs attention",
  unavailable: "Unavailable",
  waiting_permissions: "Waiting for permission",
};

function grant(value: boolean | null): string {
  return value === true ? "Allowed" : value === false ? "Not allowed" : "Checked when CuaDriver first starts";
}

/** What this platform needs from the user, in the OS's own words. */
function PlatformNotes({status}: {status: DesktopStatus}) {
  if (status.platform === "darwin") {
    return <p className="platform-note">VARIANT-1 controls apps through CuaDriver. Allow CuaDriver under System Settings › Privacy &amp; Security › Accessibility and Screen Recording. The permissions belong to CuaDriver, so they stay in place when VARIANT-1 updates.</p>;
  }
  if (status.platform === "linux") {
    const wayland = status.sessionType === "wayland";
    const gnome = /gnome/i.test(status.desktop || "");
    return <p className="platform-note">{wayland
      ? `This is a Wayland session${status.desktop ? ` (${status.desktop})` : ""}. Wayland compositors decide what other apps may see and control${gnome ? "; GNOME restricts input and screen capture for third-party tools, so some apps can't be controlled" : ", so some apps may not be controllable"}. An X11 session has no such limits.`
      : "VARIANT-1 controls apps through CuaDriver in this X11 session."}</p>;
  }
  return <p className="platform-note">VARIANT-1 controls apps through CuaDriver. Windows doesn't let a normal app control windows running as administrator, so elevated apps can't be controlled.</p>;
}

export function DesktopSettings() {
  const {connected, status, pending, requesting, error} = useDesktopStatus();
  const waiting = status?.driver.state === "waiting_permissions";

  useEffect(() => { if (connected) refreshDesktopStatus(); }, [connected]);
  // No push events: check again while macOS waits on the user's answer.
  useEffect(() => {
    if (!waiting || !connected) return;
    const id = setInterval(() => refreshDesktopStatus(), 2000);
    return () => clearInterval(id);
  }, [waiting, connected]);

  const permissions = status?.permissions;
  const missing = !!permissions && (permissions.accessibility !== true || permissions.screenRecording !== true);
  return <section className="about-card desktop-card deck-instrument">
    <header className="deck-instrument__header">
      <div>
        <span className="deck-instrument__eyebrow">DESKTOP CONTROL</span>
        <h3 className="deck-instrument__title">CuaDriver</h3>
      </div>
      <span className={status?.driver.state === "running" || status?.driver.state === "stopped" ? "platform-badge platform-badge--ready" : "platform-badge"}>
        {status ? STATE_LABEL[status.driver.state] : connected ? "Checking" : "Offline"}
      </span>
    </header>
    {status ? <>
      <PlatformNotes status={status}/>
      <div className="deck-data-list">
        <div className="deck-data-row"><span><strong>Driver</strong><small>{status.driver.available ? `CuaDriver ${status.driver.version || ""}`.trim() : "Not installed with this build"}</small></span><em>{STATE_LABEL[status.driver.state]}</em></div>
        {status.driver.error ? <div className="deck-data-row"><span><strong>Last problem</strong><small>{status.driver.error}</small></span></div> : null}
        {status.platform === "darwin" ? <>
          <div className="deck-data-row"><span><strong>Accessibility</strong><small>Lets CuaDriver click and type in other apps.</small></span><em>{grant(permissions?.accessibility ?? null)}</em></div>
          <div className="deck-data-row"><span><strong>Screen Recording</strong><small>Lets CuaDriver see other apps' windows.</small></span><em>{grant(permissions?.screenRecording ?? null)}</em></div>
        </> : null}
      </div>
    </> : null}
    {error ? <p className="runtime-error" role="alert">{error}</p> : null}
    <div className="desktop-card__actions">
      {status?.canRequestPermissions && (missing || waiting) ? <Button tone="primary" disabled={!connected || !!pending || waiting} onClick={requestDesktopPermissions}>
        {waiting ? "Waiting for your answer in macOS…" : requesting ? "Asking macOS…" : "Allow CuaDriver"}
      </Button> : null}
      <Button tone="quiet" disabled={!connected || !!pending} onClick={refreshDesktopStatus}>{pending && !requesting ? "Checking…" : "Refresh"}</Button>
    </div>
  </section>;
}
