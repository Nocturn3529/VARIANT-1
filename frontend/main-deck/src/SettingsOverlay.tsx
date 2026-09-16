import {Icon} from "./ui/Icon";
import {Overlay} from "./ui/Overlay";
import {SettingsPageContent} from "./SettingsPageContent";
import {selectSettingsCategory, type SettingsCategory} from "./state/appStore";

import {SETTINGS_PAGES} from "./state/settingsCatalog";

function SettingsIcon({path}: {path: string}) {
  return <svg viewBox="0 0 24 24" aria-hidden="true"><path d={path}/></svg>;
}

export function SettingsOverlay({
  category,
  onClose,
}: {
  category: SettingsCategory;
  onClose: () => void;
}) {
  const active = SETTINGS_PAGES.find(page => page.id === category) || SETTINGS_PAGES[0];

  return <Overlay className="settings-overlay" labelledBy="settings-overlay-title" onClose={onClose}>
    <section
      className="settings-overlay__surface"
      aria-labelledby="settings-overlay-title"
    >
      <header className="settings-overlay__chrome">
        <button
          type="button"
          className="settings-overlay__close"
          aria-label="Close settings"
          onClick={onClose}
        ><Icon name="close"/></button>
      </header>

      <div className="settings-overlay__layout">
        <aside className="settings-rail" aria-label="Settings sections">
          <nav>
            {SETTINGS_PAGES.map(page => <button
              key={page.id}
              type="button"
              className={[page.nested ? "nested" : "", page.id === category ? "active" : ""].filter(Boolean).join(" ") || undefined}
              aria-current={page.id === category ? "page" : undefined}
              onClick={() => selectSettingsCategory(page.id)}
            >
              <SettingsIcon path={page.path}/>
              <span>{page.label}</span>
            </button>)}
          </nav>
        </aside>

        <div className="settings-mobile-nav">
          <select
            aria-label="Settings page"
            value={category}
            onChange={event => selectSettingsCategory(event.target.value as SettingsCategory)}
          >
            {SETTINGS_PAGES.map(page => <option key={page.id} value={page.id}>{page.label}</option>)}
          </select>
        </div>

        <main className="settings-overlay__main">
          <header className="settings-page-heading">
            <h1 id="settings-overlay-title">{active.label}</h1>
            <p>{active.description}</p>
          </header>
          <div className="settings-page-body">
            <SettingsPageContent category={category}/>
          </div>
        </main>
      </div>
    </section>
  </Overlay>;
}
