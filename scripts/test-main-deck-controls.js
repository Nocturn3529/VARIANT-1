'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..');
const read = relative => fs.readFileSync(path.join(root, relative), 'utf8');
const exists = relative => fs.existsSync(path.join(root, relative));

const app = read('frontend/main-deck/src/DeckApp.tsx');
const appStore = read('frontend/main-deck/src/state/appStore.ts');
const main = read('frontend/main-deck/src/main.tsx');
const protocol = read('frontend/main-deck/src/protocol.ts');
const composer = read('frontend/main-deck/src/chat/ChatComposer.tsx');
const chatDest = read('frontend/main-deck/src/ChatDestination.tsx');
const modelPicker = read('frontend/main-deck/src/chat/ModelPicker.tsx');
const sessionContextStore = read('frontend/main-deck/src/sessionContextStore.ts');
const automationStore = read('frontend/main-deck/src/automationStore.ts');
const automationView = read('frontend/main-deck/src/AutomationsDestination.tsx');
const stylesIndex = read('frontend/main-deck/src/styles/index.css');
const windows = read('electron-app-windows.js');
const boot = read('electron-app-boot.js');
const deckRoutes = JSON.parse(read('deck-routes.json'));
const memoryStore = read('frontend/main-deck/src/memoryStore.ts');
const terminalStore = read('frontend/main-deck/src/context/terminalStore.ts');
const terminalPanel = read('frontend/main-deck/src/context/TerminalPanel.tsx');
const terminalSurface = read('frontend/main-deck/src/workbench/TerminalSurface.tsx');
const deckIpc = read('electron-deck-ipc.js');
const preload = read('deck-preload.js');
const aboutStore = read('frontend/main-deck/src/aboutStore.ts');
const workbench = read('frontend/main-deck/src/workbench/Workbench.tsx');
const workbenchStore = read('frontend/main-deck/src/workbench/workbenchStore.ts');
const layoutModel = read('frontend/main-deck/src/workbench/layoutModel.ts');
const previewStore = read('frontend/main-deck/src/workbench/previewStore.ts');
const previewPane = read('frontend/main-deck/src/workbench/PreviewPane.tsx');
const browserBridge = read('frontend/main-deck/src/workbench/browserBridge.ts');
const browserHost = read('frontend/main-deck/src/workbench/browserHostBridge.ts');
const filesPanel = read('frontend/main-deck/src/context/FilesPanel.tsx');
const reviewPanel = read('frontend/main-deck/src/context/ReviewPanel.tsx');
const activityRouting = read('frontend/main-deck/src/workbench/activityRouting.ts');
const shellCss = read('frontend/main-deck/src/styles/shell.css');
const workbenchCss = read('frontend/main-deck/src/styles/workbench.css');
const chatCss = read('frontend/main-deck/src/styles/chat.css');
const chatMessageList = read('frontend/main-deck/src/chat/ChatMessageList.tsx');
const turnActivity = read('frontend/main-deck/src/chat/TurnActivity.tsx');
const activityModel = read('frontend/main-deck/src/chat/activityModel.ts');
const conversationTimeline = read('frontend/main-deck/src/chat/ConversationTimeline.tsx');
const chatEvents = read('frontend/main-deck/src/protocol/chatEvents.ts');

const primaryViews = ['chat', 'memory', 'automations', 'overview', 'runtime', 'settings'];
assert.deepStrictEqual(deckRoutes.primary, primaryViews);
assert.match(windows, /require\('\.\/deck-routes\.json'\)/);
for (const view of primaryViews) assert.match(appStore, new RegExp(`"${view}"`));

assert.match(app, /<Workbench api=\{api\}/,
  'Chat must render through the one workbench tree');
assert.match(workbench, /SplitView[\s\S]*GroupView[\s\S]*<TerminalPanel chatId=/,
  'the workbench must own split groups, pane groups, and chat-owned terminal panels');
assert.match(read('frontend/main-deck/src/context/TerminalPanel.tsx'), /<PersistentTerminalSurface chatId=\{chatId\}/,
  'each terminal panel must bind its persistent emulator to the same owner');
assert.match(layoutModel, /export function normalize[\s\S]*export function movePane[\s\S]*export function updateSplitWeights/,
  'the copied Hermes model must retain canonical normalization, pane moves, and resize weights');
assert.match(workbenchStore, /variant1\.workbench\.layout\.v1[\s\S]*defaultLayout[\s\S]*terminal-deck[\s\S]*quad/,
  'layout and bundled presets must persist through one versioned store');
assert.match(workbench, /application\/x-variant1-pane[\s\S]*workbench-drop--/,
  'pane tabs must support center/edge drag placement');
assert.match(workbench, /Ctrl\+|cycleFocusedGroup|activateFocusedSlot|reopenLastClosed/,
  'focused-zone keyboard tab behavior must remain wired');
assert.match(workbenchCss, /workbench-sash[\s\S]*workbench-drop[\s\S]*workbench-side-overlay/,
  'resizable seams, drop targets, and narrow overlays must be styled');

for (const retired of [
  'frontend/main-deck/src/shell/ContextPanel.tsx',
  'frontend/main-deck/src/state/contextStore.ts',
  'frontend/main-deck/src/BrowserFabricDestination.tsx',
  'frontend/main-deck/src/DesktopFabricDestination.tsx',
  'frontend/main-deck/src/ArtifactsDestination.tsx',
  'frontend/main-deck/src/context/DetailsPanel.tsx',
  'frontend/main-deck/src/state/paneSync.ts',
  'frontend/main-deck/src/browserHost.ts',
  'electron-browser.js',
]) assert.strictEqual(exists(retired), false, `${retired} must stay deleted`);
assert.doesNotMatch(main, /react-runtime-(?:review|artifacts|browser-fabric|desktop-fabric)/,
  'retired panel socket modules must not remain registered');
assert.doesNotMatch(protocol, /react-runtime-(?:review|artifacts|browser-fabric|desktop-fabric)/,
  'retired panel message routing must not remain');

assert.match(filesPanel, /readWorkbenchDirectory[\s\S]*\.gitignore[\s\S]*openFilePreview/,
  'Files must be a lazy real project tree with ignore and preview behavior');
assert.match(filesPanel, /addChatPathAttachment[\s\S]*renameWorkbenchPath[\s\S]*trashWorkbenchPath/,
  'Files must support attach, rename, and recoverable delete');
assert.match(deckIpc, /workbench:fs:readDir[\s\S]*workbench:fs:readFile[\s\S]*workbench:fs:watch/,
  'Electron must own the narrow filesystem/read/watch bridge');
assert.match(preload, /readWorkbenchDirectory[\s\S]*watchWorkbenchPath/);

assert.match(reviewPanel, /getWorkbenchGitStatus[\s\S]*getWorkbenchGitDiff[\s\S]*runWorkbenchGit/,
  'Review must observe and operate on live Git truth');
for (const action of ['stage', 'unstage', 'revert', 'commit', 'commit_push', 'create_pr']) {
  assert.ok(deckIpc.includes(`action === '${action}'`), `${action} must be a live Review action`);
}
assert.doesNotMatch(reviewPanel, /review:discover|review:start|review:approve/,
  'manual durable Review ceremony must stay deleted from the UI');

assert.match(terminalStore, /type:\s*"terminal:open"/);
assert.doesNotMatch(terminalPanel, /truePty \? "ConPTY"/,
  'a real PTY must show its transport, not the Windows ConPTY label');
assert.match(terminalPanel, /active\.transport/);
assert.doesNotMatch(terminalStore, /profile:\s*"powershell"/,
  'Open Terminal must not demand PowerShell on every host');
assert.match(terminalStore, /process:logs/,
  'Terminal and background-process mirrors must use Execution Fabric');
assert.match(terminalSurface, /SerializeAddon[\s\S]*Unicode11Addon[\s\S]*WebLinksAddon/,
  'persistent xterm keeps snapshot, Unicode and link support with the DOM renderer');
assert.doesNotMatch(terminalSurface,/new WebglAddon/,'software graphics must not allocate a WebGL renderer for each terminal');
assert.match(terminalSurface, /allowProposedApi:\s*true[\s\S]*loadAddon\(unicode\)/,
  'Unicode11 must not crash a restored xterm instance');
assert.match(terminalSurface, /snapshots\.size>12/, "retained emulator snapshots must remain bounded");
assert.match(terminalSurface, /end:appliedEnd\.current,cols:terminal\.cols,rows:terminal\.rows/, "snapshots retain their consumed stream offset and terminal dimensions");
assert.match(terminalPanel, /selectedProcessId[\s\S]*Close process mirror/,
  'background processes must render as closeable read-only mirrors');
assert.doesNotMatch(terminalStore + main, /terminalStart|electron-deck-terminal|installTerminalBridge/,
  'a second Electron PTY authority must not exist');
assert.doesNotMatch(activityRouting, /appendTerminalOutput/,
  'tool summaries must never be fabricated as terminal bytes');

assert.match(windows, /webviewTag:\s*true/,
  'the Deck must enable real Chromium preview guests');
assert.match(boot, /persist:variant1-preview[\s\S]*setPermissionRequestHandler/,
  'the explicit Browser surface must own one persistent same-user guest partition');
assert.match(previewStore, /openPreview[\s\S]*openBrowser[\s\S]*openFilePreview[\s\S]*openOutputPreview/,
  'all dynamic documents must converge on one preview registry');
assert.match(previewPane, /ownerDocument\.createElement\("webview"\)[\s\S]*Back[\s\S]*Forward[\s\S]*Reload[\s\S]*DevTools[\s\S]*Pop out/,
  'Browser must expose real guest navigation and diagnostics');
for (const behavior of ['workbench-browser__console', 'console-message', 'findInPage']) {
  assert.ok(previewPane.includes(behavior), `Browser must retain ${behavior}`);
}
assert.match(browserBridge, /sendInputEvent[\s\S]*data-variant1-browser-ref[\s\S]*captureWorkbenchPreview/,
  'model browser actions must use the same guest, real input, screenshots, and semantic refs');
assert.match(browserHost, /new_page[\s\S]*activate_page[\s\S]*runWorkbenchBrowserCommand/,
  'ASTB must address explicit visible workbench browser tabs');
assert.match(previewPane, /detachWorkbenchPane\(`preview:\$\{tab.id\}`\)/,
  'browser pop-outs must use the same native panel system as Files and Terminal');

assert.match(previewPane, /writeWorkbenchFile[\s\S]*conflict[\s\S]*Discard and reload[\s\S]*Overwrite/,
  'file editing must guard against external changes');
assert.match(previewPane, /parseMarkdown[\s\S]*highlightCode[\s\S]*PdfPreview/,
  'file preview must cover rendered Markdown, source highlighting, and media/PDF');
assert.match(previewStore, /kind === "url" \|\| tab\.target\.kind === "file"/,
  'only file and URL tabs may persist; transient outputs must not');
assert.match(workbench, /keepAlive:\s*true/,
  'dynamic previews must opt into mounted tab state');
assert.match(workbench, /workbench-pane-layer[\s\S]*pane\.render\(\)/,
  'inactive browser and file previews must stay mounted inside their tab group');

assert.match(chatDest, /WORKBAR_TABS[\s\S]*terminal[\s\S]*review[\s\S]*browser/);
assert.match(activityRouting, /apply_patch[\s\S]*browser_[\s\S]*run_command/);
assert.match(activityRouting, /A one-shot run_command does not own a PTY/);
assert.match(chatCss, /\.history-item__select\s*\{[^}]*min-width:\s*0[^}]*flex:\s*1/s);
assert.match(chatMessageList + chatCss, /chat-completion-announcement/);
for (const component of ['TraceEntry', 'TurnActivity']) {
  assert.ok(turnActivity.includes(`function ${component}`),
    `Chat must retain the accessible ${component} component`);
}
assert.match(activityModel, /currentStepLabel[\s\S]*normalizeActivityStatus/,
  'tool runs must have semantic summaries and one status vocabulary');
assert.match(conversationTimeline, /MIN_ENTRIES = 4[\s\S]*170/,
  'long chats must use the bounded four-prompt timeline');
assert.match(chatEvents, /call_id[\s\S]*duration_ms[\s\S]*admission_ms/,
  'activity wire projection must preserve exact call identity and timings');
const activityCss = chatCss.slice(
  chatCss.indexOf('.turn-activity-stack'),
  chatCss.indexOf('.message__delivery'),
);
assert.doesNotMatch(activityCss, /deck-accent|signal-cyan|deck-danger/,
  'Steps and running-state styling must remain black, white, and secondary grey');
assert.doesNotMatch(chatMessageList + chatCss, /TurnStepsList|chat-turn-activity|work-beeper|typing-caret/,
  'the former Activity card and blue running treatments must stay deleted');
assert.doesNotMatch(app + shellCss, /titlebar__center|window-title/);

assert.match(composer, /mutation/i);
assert.match(composer, />\s*Steer current task\s*</);
assert.match(composer, />\s*Queue next message\s*</);
assert.match(composer, /setChatDelivery\("steer",sessionId\)/);
assert.match(composer, /setChatDelivery\("follow_up",sessionId\)/);
assert.match(composer, /send\(deliveryMode\)/);
assert.strictEqual((composer.match(/<ContextMeter\b/g) || []).length, 1);
assert.match(modelPicker, /role="group"[\s\S]*reasoning effort[\s\S]*aria-pressed/i);
assert.match(composer, /reasoning:effort:set/);
assert.match(sessionContextStore + modelPicker, /model:options[\s\S]*model-picker__provider[\s\S]*model-picker__effort-menu/);

assert.match(app, /AutomationsDestination/);
assert.match(main, /reg\("react-runtime-automations"/);
for (const command of ['automation:list', 'automations:history', 'automation:add', 'automation:update', 'automation:remove', 'automation:run']) {
  assert.ok(automationStore.includes(command));
}
for (const trigger of ['daily', 'weekdays', 'weekly', 'interval', 'cron']) assert.match(automationView, new RegExp(`value="${trigger}"`));
assert.doesNotMatch(automationView + automationStore, /value="(?:file|window|clipboard)"|Queue latest|queue_latest|backpressure/);
assert.match(stylesIndex, /\.\/workbench\.css/);
assert.match(memoryStore, /fact was not saved[\s\S]*goal run was not created/);
assert.match(deckIpc, /available:\s*!!\(r && r\.isUpdateAvailable\)/);
assert.match(aboutStore, /result\.available && result\.version/);
assert.doesNotMatch(deckIpc, /dialog:pickFiles/);

const generalStore = read('frontend/main-deck/src/generalStore.ts');
const generalSettings = read('frontend/main-deck/src/GeneralSettings.tsx');
const platformSettings = read('frontend/main-deck/src/PlatformSettings.tsx');
const providerCenter = read('frontend/main-deck/src/ProviderCenter.tsx');
const providerAccounts = providerCenter.slice(
  providerCenter.indexOf('export function ProviderAccounts'),
  providerCenter.indexOf('function ProviderKeyRow'),
);
const platformStore = read('frontend/main-deck/src/store.ts');
const settingsOverlay = read('frontend/main-deck/src/SettingsOverlay.tsx');
const voiceSettings = read('frontend/main-deck/src/VoiceSettings.tsx');
const agentToolsSettings = read('frontend/main-deck/src/AgentToolsSettings.tsx');
const agentToolsStore = read('frontend/main-deck/src/agentToolsStore.ts');
const messagingSettings = read('frontend/main-deck/src/MessagingSettings.tsx');
const pluginsStore = read('frontend/main-deck/src/pluginsStore.ts');

assert.match(generalStore, /result\.ok && result\.value === on/);
assert.match(generalSettings, /Pre-warm local engine[\s\S]*local:prewarm:set/);
assert.doesNotMatch(generalSettings + generalStore + deckIpc, /Overlay behavior|settings:setProjectRoot/);
assert.match(providerCenter, /ProviderAccounts[\s\S]*ProviderKeys[\s\S]*CustomEndpointsPanel/);
assert.match(providerCenter, /provider-oauth-overlay[\s\S]*provider-device-code/);
assert.match(providerAccounts, /others\.length > 0[\s\S]*Connect another provider/,
  'Accounts must disclose only real unconnected account-auth providers');
assert.doesNotMatch(providerAccounts, /<strong>API key provider<\/strong>|<strong>Custom endpoint<\/strong>/,
  'Accounts must not duplicate the API keys or Custom endpoints subsections');
assert.match(providerCenter, /TrashIcon[\s\S]*cloud:oauth:disconnect[\s\S]*ProviderKeyRow/,
  'stored OAuth and API-key credentials must expose visible remove actions');
assert.doesNotMatch(providerCenter + platformStore + protocol,
  /cloud:provider:(?:set|accepted|rejected)/,
  'credential rows must not select or label a process-global inference route');
assert.doesNotMatch(providerCenter, /active \? "✓ Active"/,
  'connected account badges must never become route-selection state');
assert.doesNotMatch(platformSettings, /RuntimeEndpointPanel|RemoteNodeSettings|Inference nodes/);
assert.match(read('frontend/main-deck/src/ui/Overlay.tsx'), /showModal\(\)/, 'native modal stacking must support nested editors');
assert.doesNotMatch(voiceSettings, /fields\.voice|const \[voice, setVoice\]/);
assert.match(agentToolsStore, /searxng:status/);
assert.doesNotMatch(agentToolsSettings, /type: "web_search:set", provider: "searxng"/);
assert.match(messagingSettings, /<PlatformDetail key=\{selected\.id\}/);
assert.match(pluginsStore, /package_id[\s\S]*plugins\.map[\s\S]*active/);

console.log('main deck controls: Hermes workbench, live Files/Review/Terminal/Browser, providers, automations, and mutation controls verified');
