import {setAppearance, useAppearance} from "../state/appearanceStore";
import {SettingsSection} from "./Settings";

export function AppearanceSettings() {
  const value = useAppearance();
  return <SettingsSection eyebrow="Appearance" title="Reading and motion">
    <div className="appearance-row"><div><strong>Interface density</strong><p>Comfortable adds room to read. Compact keeps more in view.</p></div>
      <div className="appearance-options" role="group" aria-label="Interface density">
        {(["comfortable", "compact"] as const).map(density => <button key={density} type="button" aria-pressed={value.density === density}
          onClick={() => setAppearance({density})}>{density === "comfortable" ? "Comfortable" : "Compact"}</button>)}
      </div>
    </div>
    <div className="appearance-row"><div><strong>Motion</strong><p>Reduced motion keeps the kernel symbol still.</p></div>
      <select aria-label="Motion preference" value={value.motion} onChange={event => setAppearance({motion: event.target.value as "system" | "reduced"})}>
        <option value="system">Follow system</option><option value="reduced">Reduced motion</option>
      </select>
    </div>
  </SettingsSection>;
}
