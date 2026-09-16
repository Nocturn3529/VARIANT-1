import {setPeerContext,setPeerConnection,ingestPeers,disposePeers} from "./peers/peerStore";
import {detachedChatId,installDockedChatBridge} from "./runtime/viewIdentity";
import { createRoot } from "react-dom/client";
import "@xterm/xterm/css/xterm.css";
import {
  ingestAbout,
  refreshAbout,
  setAboutConnection,
  setAboutContext,
} from "./aboutStore";
import {
  ingestAutomations,
  refreshAutomations,
  setAutomationConnection,
  setAutomationContext,
} from "./automationStore";
import {
  ingestAgentTools,
  refreshAgentTools,
  setAgentToolsConnection,
  setAgentToolsContext,
} from "./agentToolsStore";
import {
  applyChatSubtitle,
  ingestChat,
  refreshChat,
  setChatConnection,
  setChatContext,
} from "./chatStore";
import {
  ingestGeneral,
  refreshGeneral,
  setGeneralConnection,
  setGeneralContext,
} from "./generalStore";
import {
  ingestMemory,
  refreshMemoryQuiet,
  setMemoryConnection,
  setMemoryContext,
} from "./memoryStore";
import {
  enterOverview,
  ingestOverview,
  setOverviewConnection,
  setOverviewContext,
} from "./overviewStore";
import {
  ingestPlugins,
  refreshPlugins,
  setPluginsConnection,
  setPluginsContext,
} from "./pluginsStore";
import {
  ingest,
  refresh,
  setContext,
  setPlatformConnection,
} from "./store";
import { storeModuleHooks } from "./runtime/regStore";
import {installBrowserDownloads} from "./workbench/browserDownloads";
import {ingestBrowserSettings, setBrowserSettingsConnection, setBrowserSettingsContext} from "./browserSettingsStore";
import {ingestLocalModels, setLocalModelsConnection, setLocalModelsContext} from "./localModelsStore";
import {ingestServiceSettings, setServiceSettingsConnection, setServiceSettingsContext} from "./serviceSettingsStore";
import {ingestChatProjects,setChatProjectContext} from "./state/chatProjectStore";
import {
  REACT_MODULE_MESSAGE_TYPES,
  parseChatWsMessage,
  type ChatWsMessage,
  type ReactRuntimeModuleId,
  type WsMessage,
} from "./protocol";
import type { RuntimeContext } from "./types";
import type { RuntimeApi } from "./types";
import { DeckRuntime } from "./runtime/DeckRuntime";
import type {BackendConnectionState} from "./runtime/BackendClient";
import {turnController} from "./state/turnStore";
import {
  ingestBrowserHost,
  setBrowserHostConnection,
  setBrowserHostContext,
} from "./workbench/browserHostBridge";
import {
  ingestSessions,
  switchSession,
  refreshSessions,
  setSessionConnection,
  setSessionContext,
} from "./state/sessionStore";
import {
  ingestSessionContext,
  setSessionContextConnection,
  setSessionContextContext,
} from "./sessionContextStore";
import {
  ingestClarification,
  refreshClarification,
  setClarificationConnection,
  setClarificationContext,
} from "./state/clarificationStore";
import {
  disposeMic,
  ingestMic,
  setMicConnection,
  setMicContext,
} from "./state/micStore";
import {DeckApp} from "./DeckApp";
import {
  getAppState,
  navigateTo,
  type PrimaryView,
} from "./state/appStore";
import {notifyToast} from "./state/toastStore";
import {AppearanceBindings} from "./state/appearanceStore";
import {installDeckRuntime} from "./runtime/runtimeBridge";
import {
  disposeTerminalRuntime,
  ingestTerminal,
  setTerminalConnection,
  setTerminalContext,
} from "./context/terminalStore";
import "./styles/index.css";

installDockedChatBridge();
const api = (window.variant1Deck as RuntimeApi | undefined) ?? null;
const appSnapshot = getAppState();
const validViews = new Set<PrimaryView>([
  "chat",
  "memory",
  "automations",
  "overview",
  "settings",
]);
const deckRuntime = new DeckRuntime({
  api,
  notify: notifyToast,
  navigate: view => {
    if (validViews.has(view as PrimaryView)) navigateTo(view as PrimaryView);
  },
  initialView: appSnapshot.view,
  initialSettingsCategory: appSnapshot.settingsCategory,
});
installDeckRuntime(deckRuntime);
deckRuntime.subscribeState(() => {
  const diagnostics = deckRuntime.getDiagnostics();
  document.body.dataset.deckLiveSockets = String(diagnostics.liveSocketCount);
  document.body.dataset.deckMaxConcurrentSockets = String(
    diagnostics.maxConcurrentSocketCount,
  );
});
const fixtureMode = new URLSearchParams(window.location.search).get("fixture") === "1";
const disposeBrowserDownloads = installBrowserDownloads(api);
if (fixtureMode) {
  document.addEventListener("variant1:fixture-state", event => {
    const state = (event as CustomEvent).detail;
    if (state === "connecting" || state === "connected" || state === "offline") {
      deckRuntime.setDevelopmentConnectionState(state);
    }
  });
  document.addEventListener("variant1:fixture-message", event => {
    const message = (event as CustomEvent).detail;
    if (message && typeof message === "object" && typeof message.type === "string") {
      deckRuntime.ingestDevelopmentMessage(message as WsMessage);
    }
  });
  const fixtureLoader = document.createElement("script");
  fixtureLoader.src = "./dev/fixture-loader.js";
  document.head.appendChild(fixtureLoader);
}
window.addEventListener("beforeunload", () => {
  disposeBrowserDownloads();
  disposeMic();
  disposeTerminalRuntime();
  disposePeers();
  deckRuntime.stop();
}, {once: true});

const reactRoot = document.getElementById("variant1-react-root");
if (!reactRoot) throw new Error("Missing #variant1-react-root platform host");
createRoot(reactRoot).render(<><AppearanceBindings/><DeckApp api={api}/></>);

function reg(
  id: ReactRuntimeModuleId,
  hooks: {
    start?: (ctx: RuntimeContext) => void;
    enter?: (ctx: RuntimeContext, view: string) => void;
    settingsCategory?: (ctx: RuntimeContext, category: string) => void;
    /** Optional edge parser; Chat uses parseChatWsMessage. */
    parse?: (message: WsMessage) => WsMessage;
    handle?: (ctx: RuntimeContext, message: WsMessage) => void;
    /** Fired when the Deck WebSocket state changes: connected | connecting | offline. */
    connection?: (ctx: RuntimeContext, status: BackendConnectionState) => void;
    /** Presence-line updates from runtime (turn/stream/offline). */
    subtitle?: (
      ctx: RuntimeContext,
      text: string,
      state: "ready" | "working" | "idle" | "offline",
    ) => void;
  },
) {
  deckRuntime.register({
    id,
    messageTypes: REACT_MODULE_MESSAGE_TYPES[id],
    ...hooks,
  });
}

function online(ctx: RuntimeContext) {
  return !!ctx.isOpen?.();
}

// â”€â”€ Settings â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

reg("react-runtime-platform", storeModuleHooks({
  setContext,
  setConnection: setPlatformConnection,
  ingest,
  settingsCategory: ["general", "providers", "provider-keys", "custom-endpoints", "local-models", "messaging"],
  onSettings: refresh,
}));

reg("react-runtime-general", storeModuleHooks({
  setContext: setGeneralContext,
  setConnection: setGeneralConnection,
  ingest: ingestGeneral,
  settingsCategory: ["general", "local-models", "voice"],
  onSettings: refreshGeneral,
}));

reg("react-runtime-tools", storeModuleHooks({
  setContext: setAgentToolsContext,
  setConnection: setAgentToolsConnection,
  ingest: ingestAgentTools,
  settingsCategory: "search",
  onSettings: refreshAgentTools,
}));

if(!detachedChatId())reg("react-runtime-browser-host", storeModuleHooks({
  setContext: setBrowserHostContext,
  setConnection: setBrowserHostConnection,
  ingest: ingestBrowserHost,
}));

reg("react-runtime-service-settings", storeModuleHooks({
  setContext: setServiceSettingsContext, setConnection: setServiceSettingsConnection, ingest: ingestServiceSettings,
}));

reg("react-runtime-local-models", storeModuleHooks({
  setContext: setLocalModelsContext, setConnection: setLocalModelsConnection, ingest: ingestLocalModels,
}));

reg("react-runtime-browser-settings", storeModuleHooks({
  setContext: setBrowserSettingsContext,
  setConnection: setBrowserSettingsConnection,
  ingest: ingestBrowserSettings,
}));

reg("react-runtime-peers",storeModuleHooks({setContext:setPeerContext,setConnection:setPeerConnection,ingest:ingestPeers}));

reg("react-runtime-plugins", storeModuleHooks({
  setContext: setPluginsContext,
  setConnection: setPluginsConnection,
  ingest: ingestPlugins,
  settingsCategory: "plugins",
  onSettings: refreshPlugins,
}));

reg("react-runtime-about", storeModuleHooks({
  setContext: setAboutContext,
  setConnection: setAboutConnection,
  ingest: ingestAbout,
  settingsCategory: "about",
  onSettings: refreshAbout,
}));

// â”€â”€ Destinations â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

reg("react-runtime-chat", {
  start(ctx) {
    setChatContext(ctx);
  },
  enter(ctx, view) {
    setChatContext(ctx);
    if (view === "chat" && online(ctx)) refreshChat();
  },
  connection(ctx, status) {
    setChatContext(ctx);
    setChatConnection(status);
    if (status === "connected") refreshChat();
  },
  subtitle(ctx, text, state) {
    setChatContext(ctx);
    applyChatSubtitle(text, (state as "ready" | "working" | "idle" | "offline") || "ready");
  },
  // Shape high-churn chat events once at the runtime edge.
  parse: (raw) => parseChatWsMessage(raw) as WsMessage,
  handle(ctx, message) {
    setChatContext(ctx);
    ingestChat(message as ChatWsMessage);
  },
});

reg("react-runtime-sessions", storeModuleHooks({
  setContext: ctx=>{setSessionContext(ctx);setChatProjectContext(ctx);},
  setConnection: setSessionConnection,
  ingest: message=>{ingestSessions(message);ingestChatProjects(message);},
  enterView: "chat",
  onEnter: refreshSessions,
}));


reg("react-runtime-session-context", storeModuleHooks({
  setContext: setSessionContextContext,
  setConnection: setSessionContextConnection,
  ingest: ingestSessionContext,
}));

reg("react-runtime-clarifications", storeModuleHooks({
  setContext: setClarificationContext,
  setConnection: setClarificationConnection,
  ingest: ingestClarification,
  enterView: "chat",
  onEnter: refreshClarification,
}));

reg("react-runtime-mic", storeModuleHooks({
  setContext: setMicContext,
  setConnection: setMicConnection,
  ingest: ingestMic,
}));

reg("react-runtime-execution", storeModuleHooks({
  setContext: setTerminalContext,
  setConnection: setTerminalConnection,
  ingest: ingestTerminal,
}));

reg("react-runtime-memory", storeModuleHooks({
  setContext: setMemoryContext,
  setConnection: setMemoryConnection,
  ingest: ingestMemory,
  settingsCategory: "memory",
  onSettings: refreshMemoryQuiet,
}));

reg("react-runtime-automations", storeModuleHooks({
  setContext: setAutomationContext,
  setConnection: setAutomationConnection,
  ingest: ingestAutomations,
  enterView: "automations",
  onEnter: refreshAutomations,
}));

reg("react-runtime-overview", {
  ...storeModuleHooks({
    setContext: setOverviewContext,
    setConnection: setOverviewConnection,
    ingest: ingestOverview,
  }),
  enter(ctx, view) {
    setOverviewContext(ctx);
    enterOverview(view);
  },
});

deckRuntime.subscribeClose(() => {
  turnController.end({
    status: turnController.isActive() ? "error" : "complete",
    force: true,
  });
});

window.addEventListener("error", event => {
  api?.log?.(`[deck] uncaught ${event.message || "unknown"}`);
});
window.addEventListener("unhandledrejection", event => {
  const reason = event.reason;
  const message = reason instanceof Error ? reason.message : String(reason || "unknown");
  api?.log?.(`[deck] unhandledrejection ${message}`);
});

if(detachedChatId())switchSession(detachedChatId());
deckRuntime.start();
if (fixtureMode) {
  document.body.dataset.variant1FixtureReady = "1";
  document.dispatchEvent(new CustomEvent("variant1:fixture-ready"));
}
