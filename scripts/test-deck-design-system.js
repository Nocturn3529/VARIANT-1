'use strict';

/**
 * Main Deck visual ownership contract.
 *
 * The Deck has one monochrome design system and a permanent Chat workbench.
 * Utilities share overlays, and explicit mutation accents use #39ff14.
 */
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const stylesDir = path.join(root, 'frontend', 'main-deck', 'src', 'styles');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');
const cssFiles = fs.readdirSync(stylesDir)
  .filter(name => name.endsWith('.css'))
  .sort();
assert.deepStrictEqual(cssFiles.filter(name => name.includes('theme')), [],
  'Main Deck must not carry alternate or route-local theme stylesheets');

const index = read(path.join('frontend', 'main-deck', 'src', 'styles', 'index.css'));
const imports = [...index.matchAll(/@import url\("\.\/([^"\)]+)"\);/g)]
  .map(match => match[1]);
assert.strictEqual(imports[0], 'design-system.css',
  'design-system.css must be the first authored stylesheet');
assert.strictEqual(new Set(imports).size, imports.length,
  'the authored stylesheet cascade must not import a file twice');

const designSystem = read(path.join(
  'frontend', 'main-deck', 'src', 'styles', 'design-system.css',
));
assert.match(designSystem, /--font-ui:\s*"Segoe WPC", "Segoe UI"/,
  'the accepted Mono UI uses the system Segoe stack');
assert.match(designSystem, /--deck-canvas:\s*#0e0e0e/,
  'the shared system must retain its black canvas');
assert.match(designSystem, /--deck-text-primary:\s*#eaeaea/,
  'the shared system must retain its neutral white primary ink');
for (const [token, color] of [
  ['cyan', '#eaeaea'],
  ['indigo', '#a8a8a8'],
  ['lime', '#c8c8c8'],
  ['amber', '#919191'],
  ['orange', '#b8b8b8'],
  ['magenta', '#d8d8d8'],
  ['red', '#eaeaea'],
  ['warm', '#c8c8c8'],
  ['neutral', '#e3e3e3'],
  ['idle', '#454545'],
]) {
  assert.match(designSystem, new RegExp(`--deck-signal-${token}:\\s*${color}`),
    `the shared telemetry palette must retain its ${token} signal`);
}
assert.match(designSystem, /\.deck-instrument\s*\{[^}]*background:\s*var\(--deck-canvas\)/s,
  'instrument roots must use the canvas without stacking translucent surfaces');
assert.doesNotMatch(designSystem, /--deck-page-max\s*:/,
  'non-chat destinations must not restore a centered maximum page width');
assert.match(designSystem, /\.deck-destination-page\s*\{[^}]*width:\s*calc\(100%\s*-\s*\(2\s*\*\s*var\(--deck-page-gutter\)\)\)/s,
  'non-chat destination pages must fill the frame behind adaptive gutters');

const foundationalToken = /^\s*--(?!(?:deck-(?:metric|data)-columns)\s*:)(?:deck-[\w-]+|font-(?:ui|sans|mono))\s*:/m;
const compatibilityToken = /var\(--(?:bg-[0-3]|bg-hover|bg-active|line(?:-soft|-strong)?|text-[1-5]|accent(?:-bright|-muted|-line)?|green(?:-muted)?|amber|red|panel|border|text-(?:primary|secondary|muted))\)/;
const routeBodyTheme = /body:has\(\.app-shell\.[\w-]+-active\)/;
const legacyPalette = /#(?:a18aff|b5a5ff|8670e6|7768b4|6f61a8|8b7cff|7065d8)\b|rgba?\(\s*161\s*,\s*138\s*,\s*255|rgba?\(\s*117\s*,\s*201\s*,\s*149/i;

for (const name of cssFiles) {
  const source = fs.readFileSync(path.join(stylesDir, name), 'utf8');
  if (name !== 'design-system.css') {
    assert.doesNotMatch(source, /@font-face\s*{/,
      `${name} must consume the shared Geist faces instead of declaring fonts`);
    assert.doesNotMatch(source, foundationalToken,
      `${name} must not redefine application-wide design tokens`);
  }
  assert.doesNotMatch(source, compatibilityToken,
    `${name} still consumes a retired compatibility token`);
  assert.doesNotMatch(source, routeBodyTheme,
    `${name} must not install a route-scoped body theme`);
  if (!name.startsWith('overview-')) {
    assert.doesNotMatch(source, legacyPalette,
      `${name} still contains the retired purple/green visual palette`);
  }
  const compactTextSizes = [
    ...[...source.matchAll(/\bfont-size:\s*(\d+(?:\.\d+)?)px/g)]
      .map(match => Number(match[1])),
    ...[...source.matchAll(/\bfont:\s*[^;{}\r\n]*?(\d+(?:\.\d+)?)px/g)]
      .map(match => Number(match[1])),
  ].filter(size => size > 0 && size < 11);
  if (name === 'workbench.css') {
    assert.ok(compactTextSizes.every(size => size >= 8),
      'Hermes-derived IDE chrome may be compact but must remain at least 8px');
  } else {
    assert.deepStrictEqual(compactTextSizes, [],
      `${name} must not render application text below the 11px Mono chrome minimum`);
  }
}

const automations = read(path.join(
  'frontend', 'main-deck', 'src', 'AutomationsDestination.tsx',
));
const automationCss = read(path.join(
  'frontend', 'main-deck', 'src', 'styles', 'automations.css',
));
for (const className of [
  'deck-instrument',
  'deck-metric-rail',
  'deck-section',
  'deck-data-row',
]) {
  assert.ok(automations.includes(className),
    `Automations must compose shared ${className}`);
}
assert.match(designSystem, /\.deck-empty\s*\{/,
  'the shared empty-state primitive must live in the design system');
assert.match(designSystem, /\.deck-button--quiet\s*\{/,
  'the shared button primitive must include the quiet tone');
assert.match(designSystem, /\.deck-button--icon\s*\{/,
  'the shared button primitive must include the icon tone');
assert.ok(
  fs.existsSync(path.join(root, 'frontend', 'main-deck', 'src', 'ui', 'EmptyState.tsx')),
  'EmptyState must remain the shared React empty-state control',
);
assert.ok(
  fs.existsSync(path.join(root, 'frontend', 'main-deck', 'src', 'ui', 'Button.tsx')),
  'Button must remain the shared React button control',
);
assert.ok(automations.includes('EmptyState'),
  'Automations must compose the shared EmptyState');
assert.ok(automations.includes('<Button'),
  'Automations must compose the shared Button');
assert.ok(automations.includes('tone="primary"'),
  'Automations must use the shared primary button tone');
assert.match(automationCss, /\.automations-console\s*\{[^}]*border:\s*0[^}]*border-radius:\s*0/s,
  'the full-frame Automations workspace must not sit inside a decorative outer bubble');
assert.match(automationCss, /\.automations-grid\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*2\.35fr\)\s+minmax\(320px,\s*\.8fr\)/s,
  'Automations must reserve a purposeful wide library lane and activity lane');
assert.doesNotMatch(automations, /automations-header|automations-mark|deck-destination-header/,
  'Automations must not restore the retired destination header');
assert.match(automations, /automations-console__header[\s\S]*automations-create[\s\S]*New automation[\s\S]*automations-metrics/,
  'the create action must live inside the Automation Library instrument ahead of its metrics');
assert.match(automationCss, /\.automations-create\.deck-button\s*\{[^}]*height:\s*42px[^}]*background:\s*var\(--deck-canvas\)/s,
  'the Automation create action must use the compact instrument treatment');
assert.match(designSystem, /\.deck-switch\s*\{/,
  'the shared switch primitive must live in the design system');
assert.ok(
  fs.existsSync(path.join(root, 'frontend', 'main-deck', 'src', 'ui', 'Switch.tsx')),
  'Switch must remain the shared React switch control',
);
assert.ok(automations.includes('<Switch'),
  'Automations must compose the shared Switch');

const shellCss = read(path.join(
  'frontend', 'main-deck', 'src', 'styles', 'shell.css',
));

assert.doesNotMatch(designSystem, /--deck-header-height|\.deck-destination-(?:header|title|icon|actions)/,
  'the retired destination-header system must stay deleted');
assert.doesNotMatch(automationCss, /\.workspace\s*{[^}]*display:\s*block/s,
  'Automations must not replace the shared workspace layout system');

assert.ok(!fs.existsSync(path.join(
  'frontend', 'main-deck', 'src', 'HardwareDestination.tsx',
)), 'the retired Hardware destination must stay deleted');
assert.ok(!fs.existsSync(path.join(
  'frontend', 'main-deck', 'src', 'styles', 'hardware.css',
)), 'the retired Hardware stylesheet must stay deleted');

assert.match(shellCss, /\.app-shell\s*\{[^}]*grid-template-rows:\s*var\(--titlebar-height\) minmax\(0, 1fr\) var\(--footer-height\)/s,
  'the permanent Chat shell keeps its flexible middle track');
assert.doesNotMatch(shellCss + designSystem + automationCss, /\.(?:has-working-bar|deck-destination-view|automations-view)\b/,
  'retired page-layout scaffolding must stay removed');

const memoryDestination = read(path.join('frontend', 'main-deck', 'src', 'MemoryDestination.tsx'));
const memoryCss = read(path.join('frontend', 'main-deck', 'src', 'styles', 'memory.css'));
assert.doesNotMatch(memoryDestination, /deck-destination-header|memory-header|memory-privacy-note/,
  'Memory must remain a deliberate single-page route without the retired header or privacy promotion');
assert.match(memoryDestination, /memory-page__utilities[\s\S]*id="memory-refresh"[\s\S]*id="memory-tidy"[\s\S]*memory-tabs/,
  'Memory utilities must remain compact in-page controls ahead of the memory instruments');
assert.match(memoryDestination, /memory-workspace-grid[\s\S]*memory-loops-section[\s\S]*core-profile-card[\s\S]*archival-memory-section/,
  'Memory must compose Runs, Core, and Archive as one responsive grid');
assert.doesNotMatch(memoryDestination + memoryCss, /memory-primary-layout/,
  'the retired centered Memory column layout must stay deleted');

const overviewDestination = read(path.join('frontend', 'main-deck', 'src', 'OverviewDestination.tsx'));
const overviewCss = read(path.join('frontend', 'main-deck', 'src', 'styles', 'overview.css'));
const localInference = read(path.join('frontend', 'main-deck', 'src', 'overview', 'LocalInferenceWidget.tsx'));
assert.doesNotMatch(overviewDestination, /deck-destination-header|overview-header|overview-status/,
  'Overview must remain a deliberate single-page route without the retired destination header');
assert.match(overviewDestination, /<LocalInferenceWidget[\s\S]*onRefresh=\{\(\) => requestTelemetry\(\{notify: true\}\)\}/,
  'Overview refresh must be delegated to the first telemetry instrument');
assert.match(localInference, /local-inference-header[\s\S]*id="overview-refresh"[\s\S]*local-inference-runtime/,
  'Overview refresh must remain between the first instrument title and runtime readout');
assert.match(overviewCss, /\.overview-request-list\s*\{[^}]*grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\)/s,
  'Overview request attempts must use the available wide frame as a two-lane stream');

const settingsApp = read(path.join('frontend', 'main-deck', 'src', 'DeckApp.tsx'));
const settingsOverlay = read(path.join('frontend', 'main-deck', 'src', 'SettingsOverlay.tsx'));
const settingsCss = read(path.join('frontend', 'main-deck', 'src', 'styles', 'settings.css'));
assert.doesNotMatch(settingsApp, /settings-header|settings-saved-state|settings-nav-glyph|settings-category-footer/,
  'Settings must not restore its retired route header, decorative nav glyphs, or local-build footer');
for (const kind of ['runtime', 'overview', 'automations']) {
  assert.ok(settingsApp.includes(`<UtilitySurface kind="${kind}"`), `${kind} must use a utility overlay`);
}
assert.match(read('frontend/main-deck/src/SettingsPageContent.tsx'), /memory: <MemoryDestination\/>/,
  'Memory stays in Settings rather than an unused primary-page wrapper');
assert.match(settingsApp, /<main className="workbench-view"><Workbench api=\{api\}/,
  'the Chat workbench stays mounted beneath every utility overlay');
assert.match(settingsOverlay, /<Overlay[\s\S]*labelledBy="settings-overlay-title"[\s\S]*settings-overlay__close/,
  'Settings uses the shared native modal with an explicit close control');
assert.match(read('frontend/main-deck/src/ui/Overlay.tsx'), /event\.target !== event\.currentTarget[\s\S]*getBoundingClientRect/,
  'native overlays distinguish backdrop clicks from clicks inside the surface');
assert.match(read('frontend/main-deck/src/ui/Overlay.tsx'), /showModal\(\)[\s\S]*onCancel=/,
  'native overlays own Escape and focus containment');
assert.doesNotMatch(settingsOverlay, /settings-search|Search settings|searchInputRef|event\.ctrlKey/,
  'Settings must not restore the retired search box or Ctrl+K path');
assert.doesNotMatch(settingsOverlay, /settings-brand|settings-group|settings-page__eyebrow/,
  'Settings must not restore decorative rail brands, group labels, or page eyebrows');
assert.match(settingsOverlay, /SETTINGS_PAGES\.map[\s\S]*selectSettingsCategory\(page\.id\)/,
  'Settings pages must remain directly navigable from the rail');
assert.match(settingsCss, /\.settings-overlay\s*\{[^}]*position:\s*fixed[^}]*z-index:\s*80/s,
  'Settings must be an overlay rather than a primary destination frame');
assert.match(settingsCss, /\.settings-overlay__layout\s*\{[^}]*grid-template-columns:\s*218px\s+minmax\(0,\s*1fr\)/s,
  'wide Settings must retain its stable navigation rail');
assert.match(settingsCss, /@container settings \(max-width:\s*830px\)[\s\S]*\.settings-rail\s*\{\s*display:\s*none[\s\S]*\.settings-mobile-nav/s,
  'narrow Settings must replace the rail with the compact page selector');
assert.doesNotMatch(settingsCss, /settings-category-sidebar|settings-sidebar-toggle|grid-template-columns:\s*repeat\(12/,
  'retired embedded navigation and the broken 12-column General rack must stay deleted');

const chat = read(path.join('frontend', 'main-deck', 'src', 'styles', 'chat.css'));
const chatMessages = read(path.join('frontend', 'main-deck', 'src', 'chat', 'ChatMessageList.tsx'));
const deckApp = read(path.join('frontend', 'main-deck', 'src', 'DeckApp.tsx'));
assert.match(chat, /\.chat-workspace\s*\{[^}]*--chat-column:\s*760px/s,
  'Chat must remain the deliberate centered-reading-column exception');
assert.doesNotMatch(chat + chatMessages, /chat-jump-latest|>\s*Latest\s*</,
  'Chat must not restore the retired floating Latest bubble');
assert.doesNotMatch(deckApp + shellCss + designSystem, /titlebar__center|window-title/,
  'the titlebar must not restore global route or conversation text');
assert.match(deckApp, /<header className="titlebar">[\s\S]*titlebar__drag[\s\S]*<WindowControls/,
  'the text-free titlebar must retain its drag surface, brand, and window controls');
assert.match(read(path.join('frontend', 'main-deck', 'src', 'styles', 'composer.css')), /--context-accent:/,
  'the context meter retains category geometry in the monochrome system');

assert.match(overviewCss, /var\(--deck-signal-(?:cyan|indigo|lime|amber|orange|magenta|red|warm)\)/,
  'Overview telemetry must consume the shared intentional signal palette');
assert.doesNotMatch(overviewCss, /#[0-9a-f]{6}/i,
  'Overview must not bypass canonical signal tokens with route-local color literals');

assert.ok(!fs.existsSync(path.join(stylesDir, 'overview-shell-theme.css')),
  'the retired Overview shell theme must stay deleted');
for (const name of [
  'destinations.css',
  'platform.css',
  'overview-inference.css',
  'overview-performance.css',
  'overview-cost.css',
  'overview-model-usage.css',
  'review.css',
]) {
  assert.ok(!fs.existsSync(path.join(stylesDir, name)),
    `retired cascade owner ${name} must stay deleted`);
}
assert.ok(!fs.existsSync(path.join(root, 'assets', 'unused-assets', 'overview-widgets')),
  'the retired standalone Overview frontend must stay deleted');

for (const name of [
  'chat-design.css',
  'hardware-design.css',
  'memory-design.css',
  'settings-design.css',
]) {
  assert.ok(
    !fs.existsSync(path.join(stylesDir, name)),
    `retired destination overlay ${name} must stay deleted`,
  );
}

const layout = read(path.join('frontend', 'main-deck', 'src', 'layout.ts'));
const breakpointValues = [...layout.matchAll(/^\s+(?:narrow|medium|wide):\s*(\d+),?$/gm)]
  .map(match => match[1]);
assert.deepStrictEqual(
  breakpointValues,
  ['830', '980', '1180'],
  'layout.ts must keep the three product breakpoints',
);

const allowedMedia = new Set([
  '@media (max-width: 830px)',
  '@media (max-width: 980px)',
  '@media (max-width: 1180px)',
  '@media (prefers-reduced-motion: reduce)',
]);
for (const name of cssFiles) {
  const source = fs.readFileSync(path.join(stylesDir, name), 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '');
  const queries = source.match(/@media[^{]+/g) || [];
  for (const raw of queries) {
    const query = raw.replace(/\s+/g, ' ').trim();
    assert.ok(
      allowedMedia.has(query),
      `${name} has unsanctioned media query: ${query}`,
    );
  }
}

console.log('deck design system contract: ok');
