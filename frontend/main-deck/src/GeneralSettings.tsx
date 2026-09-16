/**
 * Shared settings controls split across focused overlay pages.
 */
import {
  notifyGeneral,
  sendGeneral,
  setLaunchAtLogin,
  setStartHidden,
  useGeneralState,
} from "./generalStore";
import {send as sendPlatform, usePlatformState} from "./store";
import {LocalRuntimePanel} from "./ProviderCenter";
import {LocalModelLibrary} from "./LocalModelLibrary";
import {SettingsSection, SettingToggleRow} from "./ui/Settings";
import {AppearanceSettings} from "./ui/AppearanceSettings";

export type GeneralSettingsPage = "general" | "local-models";

export function GeneralSettings({page = "general"}: {page?: GeneralSettingsPage}) {
  const s = useGeneralState();
  const {config: platformConfig} = usePlatformState();

  const cap = s.capabilities;
  const capPassed = cap
    ? [cap.tools, cap.thinking, cap.vision, Number(cap.ctx_size || 0) > 0].filter(Boolean).length
    : null;

  return <div className={`gen-shell gen-shell--${page}`}>
    {page === "general" ? <AppearanceSettings/> : null}
    {page === "local-models" ? <LocalRuntimePanel/> : null}
    {page === "general" ? <SettingsSection eyebrow="Startup" title="When Windows starts">
        <SettingToggleRow
          title="Launch VARIANT-1 at login"
          description="Start the AI host automatically when you sign in."
          checked={s.launchAtLogin}
          onChange={value => { void setLaunchAtLogin(value); }}
        />
        <SettingToggleRow
          title="Start hidden in the tray"
          description="Launch without opening the main window."
          checked={s.startHidden}
          onChange={value => { void setStartHidden(value); }}
        />
    </SettingsSection> : null}

    {page === "general" ? <SettingsSection eyebrow="Local engine" title="Availability">
        <SettingToggleRow
          title="Pre-warm local engine"
          description="Keep the selected local runtime ready while this chat uses a cloud model."
          checked={!!platformConfig.local_prewarm}
          onChange={value => sendPlatform({type: "local:prewarm:set", value})}
        />
    </SettingsSection> : null}

    {page === "local-models" ? <>
      <LocalModelLibrary/>
        <button
          type="button"
          className="gen-action deck-data-row"
          onClick={() => {
            sendGeneral({type: "capabilities:get"});
            notifyGeneral("Reading model capabilities…");
          }}
        >
          <span>
            <strong>Model capability snapshot</strong>
            <small>Configured tool surface, reasoning, vision, and context support.</small>
          </span>
          <em>
            {capPassed == null
              ? "Not loaded"
              : `${capPassed}/4 · ${cap?.source || "model"}`}
          </em>
        </button>
    </> : null}


  </div>;
}
