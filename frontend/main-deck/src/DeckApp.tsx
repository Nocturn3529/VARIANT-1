import {installChatWindowInventory,useChatWindows} from "./workbench/chatWindowStore";
import {detachedChatId,isDockedChat} from "./runtime/viewIdentity";
import {StartupCover} from "./ui/StartupCover";
import {ChatDestination} from "./ChatDestination";
import {ActionPalette} from "./ui/ActionPalette";
import {openPalette} from "./state/paletteStore";
import {Icon} from "./ui/Icon";
import {KernelGlyph} from "./motion/KernelGlyph";
import {lazy, Suspense, useEffect} from "react";
import {cancelChatTurn, notifyChat, useChatState} from "./chatStore";
import {LazySurface} from "./ui/LazySurface";
import {focusMainComposer} from "./ui/SurfaceDocument";
import {Overlay, OverlayHeader} from "./ui/Overlay";
import {closeSettings, collapseHistory, isPrimaryView, navigateTo, revealHistory, useAppState} from "./state/appStore";
import type {RuntimeApi} from "./types";
import {ToastHost} from "./ui/ToastHost";
import {requestNewSession} from "./state/sessionStore";
import {toggleMic} from "./state/micStore";
import {Workbench} from "./workbench/Workbench";
import {isWorkbenchPaneVisible, PANE, resetWorkbenchLayout, setWorkbenchEditMode, useWorkbenchState, rightPanelsVisible,toggleRightPanels} from "./workbench/workbenchStore";
import {kernelStatusLabel, RuntimeDetails} from "./RuntimeOverlay";
import {NativeSurface} from "./workbench/NativeSurface";
import {installNativeWindowBridge, nativeUtilityKey, openNativeWindow, useNativeWindows} from "./workbench/nativeWindowStore";
import {setOverviewDetached} from "./overviewStore";

const AutomationsDestination = lazy(() => import("./deferred/destinations").then(module => ({default: module.AutomationsDestination})));
const OverviewDestination = lazy(() => import("./deferred/destinations").then(module => ({default: module.OverviewDestination})));
const SettingsOverlay = lazy(() => import("./deferred/settings").then(module => ({default: module.SettingsOverlay})));
const closeUtility = () => navigateTo("chat");

function UtilitySurface({kind, api}: {kind: "overview" | "runtime" | "automations"; api: RuntimeApi | null}) {
  const app = useAppState();
  const records = useNativeWindows();
  const id = nativeUtilityKey(kind);
  const record = records[id];
  const title = kind === "runtime" ? "Python runtime" : kind === "overview" ? "Overview" : "Automations";
  const detached = !!record;
  useEffect(() => {
    if (kind === "overview") setOverviewDetached(detached);
    return () => { if (kind === "overview") setOverviewDetached(false); };
  }, [detached, kind]);
  useEffect(() => { if (detached && app.view === kind) navigateTo("chat"); }, [detached, kind, app.view]);
  if (app.view !== kind && !detached) return null;
  return <Overlay className={`utility-overlay utility-overlay--${kind}`} labelledBy={`${kind}-overlay-title`} open={app.view === kind && !detached} onClose={closeUtility}>
    <NativeSurface id={id} title={title} onClosed={dock => { if (dock) navigateTo(kind); }}>
      {!detached ? <OverlayHeader title={title} id={`${kind}-overlay-title`} onClose={closeUtility}
        onPopout={api?.supportsNativeWindows ? () => { openNativeWindow(id, title, {width: kind === "runtime" ? 480 : 720, height: 600}); } : undefined}/> : null}
      <div className={`native-utility-content utility-overlay utility-overlay--${kind}`}>
        {kind === "overview" ? <LazySurface label="Overview"><OverviewDestination/></LazySurface> : null}
        {kind === "automations" ? <LazySurface label="Automations"><AutomationsDestination/></LazySurface> : null}
        {kind === "runtime" ? <RuntimeDetails/> : null}
      </div>
    </NativeSurface>
  </Overlay>;
}

function SettingsOverlayFallback() {
  return <Overlay className="utility-overlay" labelledBy="settings-loading-title" onClose={closeSettings}>
    <OverlayHeader title="Settings" id="settings-loading-title" onClose={closeSettings}/>
    <p className="deck-overlay__loading" role="status">Loading settings…</p>
  </Overlay>;
}

function WindowControls({api}: {api: RuntimeApi | null}) {
  return <div className="window-actions" aria-label="Window controls">
    <button type="button" aria-label="Minimize" onClick={() => api?.minimize?.()}><Icon name="minimize"/></button>
    <button type="button" aria-label="Maximize or restore" onClick={() => api?.toggleMaximize?.()}><Icon name="maximize"/></button>
    <button type="button" className="window-actions__close" aria-label="Close" onClick={() => api?.close?.()}><Icon name="close"/></button>
  </div>;
}

function WorkbenchTitlebarTools() {
  const nativeWindows = useNativeWindows();
  const chatWindows=useChatWindows().windows;
  const workbench = useWorkbenchState();
  const historyVisible = isWorkbenchPaneVisible(PANE.history, workbench);
  const panelVisible=rightPanelsVisible();
  const filesLabel = panelVisible ? "Hide right panel" : "Show right panel";
  return <div className="titlebar-workbench-tools" aria-label="Workbench layout">
    <button type="button" aria-label={historyVisible ? "Hide chats" : "Show chats"} aria-pressed={historyVisible} onClick={() => historyVisible ? collapseHistory() : revealHistory()}><Icon name="history"/></button>
    <button type="button" className={workbench.editMode ? "is-active" : ""} aria-label="Edit layout" aria-pressed={workbench.editMode} title="Edit layout; Shift-click resets" onClick={event => event.shiftKey ? resetWorkbenchLayout() : setWorkbenchEditMode(!workbench.editMode)}><Icon name="layout"/></button>
    <button type="button" aria-label={filesLabel} aria-pressed={panelVisible} onClick={toggleRightPanels}><Icon name="panels"/></button>
    <button type="button" aria-label="Panels and windows" title="Panels and windows" onClick={() => openPalette("windows")}><Icon name="windows"/>{(Object.keys(nativeWindows).length+chatWindows.length) ? <small className="window-count">{Object.keys(nativeWindows).length+chatWindows.length}</small> : null}</button>
    <button type="button" aria-label="Find an action or chat" title="Find an action or chat (Ctrl K)" onClick={() => openPalette()}><Icon name="search"/></button>
  </div>;
}

function SectionFooter({api}: {api: RuntimeApi | null}) {
  useNativeWindows();
  const app = useAppState();
  const chat = useChatState();
  const kernelLabel = kernelStatusLabel(chat.connected, chat.runtime?.kernelState);
  return <footer className="section-footer" aria-label="Main Deck footer">
    <div className="section-footer__nav" aria-label="Runtime and utilities">
      <button type="button" className="section-footer__button footer-runtime" aria-label="Python runtime and backend status" aria-haspopup="dialog" aria-expanded={app.view === "runtime"} onClick={() => navigateTo("runtime")}>
        <KernelGlyph seed={`${chat.sessionId}:${chat.runtime?.kernelGeneration}`} phase={!chat.connected ? "offline" : chat.runtime?.kernelState === "busy" ? "running" : "idle"} mutation={chat.runtime?.mutationEffectiveEnabled} size={24}/>
        <span>{kernelLabel}</span><small>{chat.connected ? "Backend connected" : "Backend offline"}</small>
      </button>
      <button type="button" className="section-footer__button" aria-label="Log monitor" onClick={async () => {
        try {
          const result = await api?.openMonitor?.();
          if (!result?.ok) notifyChat(result?.reason || "Log monitor is available in the desktop app.");
        } catch { notifyChat("Could not open Log monitor."); }
      }}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m4 7 4 5-4 5m7 0h9"/></svg><span>Log monitor</span></button>
      <button type="button" className="section-footer__button" aria-label="Overview" aria-haspopup="dialog" aria-expanded={app.view === "overview"} onClick={() => navigateTo("overview")}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 19V9m5 10V5m5 14v-7m5 7V3"/></svg><span>Overview</span></button>
    </div>
    <div className="section-footer__tools">
      <button type="button" className="section-footer__button" data-view="settings" aria-label="Settings" aria-haspopup="dialog" aria-expanded={app.view === "settings"} onClick={() => navigateTo("settings")}><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M19 5l-2 2M7 17l-2 2"/></svg><span>Settings</span></button>
    </div>
  </footer>;
}

export function DeckApp({api}: {api: RuntimeApi | null}) {
  const nativeWindows = useNativeWindows();
  const app = useAppState();
  const chat = useChatState();
  const workbench = useWorkbenchState();
  useEffect(() => installNativeWindowBridge(api), [api]);
  useEffect(()=>!detachedChatId() ? installChatWindowInventory(api) : undefined,[api]);
  useEffect(() => {
    const unsubscribe = api?.onNavigate?.(view => { if (isPrimaryView(view)) navigateTo(view); });
    return typeof unsubscribe === "function" ? () => { unsubscribe(); } : undefined;
  }, [api]);
  useEffect(() => {
    const unsubscribe = api?.onVoiceToggle?.(() => { navigateTo("chat"); toggleMic(); });
    return typeof unsubscribe === "function" ? unsubscribe : undefined;
  }, [api]);
  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      const ownerDocument = event.currentTarget as Document;
      if (event.defaultPrevented || (ownerDocument === document && app.view !== "chat") || ownerDocument.querySelector("dialog[open]")) return;
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "n") {
        event.preventDefault();
        if (requestNewSession()) focusMainComposer();
      } else if (event.key === "Escape" && chat.turnActive) {
        const target = event.target as HTMLElement | null;
        if (ownerDocument.querySelector(".runtime-session-menu, .composer-command-menu, .model-picker, .context-meter-menu") || target?.closest("[role='menu'], [role='listbox'], [popover]")) return;
        event.preventDefault(); cancelChatTurn();
      }
    };
    const documents = [document, ...Object.values(nativeWindows).flatMap(record => record.document ? [record.document] : [])];
    documents.forEach(owner => owner.addEventListener("keydown", key));
    return () => documents.forEach(owner => owner.removeEventListener("keydown", key));
  }, [app.view, chat.turnActive, nativeWindows]);

  if(detachedChatId())return <div className="app-shell chat-shell chat-active compact-chat">
    {!isDockedChat() ? <header className="titlebar"><div className="titlebar__drag"><span className="titlebar__name">{chat.title || "Chat"}</span></div><WindowControls api={api}/></header>:null}
    <main className="chat-workspace"><ChatDestination/></main>{app.view === "settings" ? <Suspense fallback={<SettingsOverlayFallback/>}><SettingsOverlay category={app.settingsCategory} onClose={closeSettings}/></Suspense>:null}<StartupCover/><ToastHost/>
  </div>;
  const settingsOpen = app.view === "settings";
  return <div className={["app-shell chat-shell chat-active", settingsOpen ? "settings-open" : "", workbench.hidden[PANE.history] ? "history-collapsed" : "", workbench.overlayPaneId === PANE.history ? "history-open-mobile" : ""].filter(Boolean).join(" ")} id="app-shell">
    <header className="titlebar"><div className="titlebar__drag"><span className="titlebar__mark" aria-hidden="true"><KernelGlyph seed={`${chat.sessionId}:${chat.runtime?.kernelGeneration}`} size={20} phase={!chat.connected ? "offline" : chat.runtime?.kernelState === "busy" ? "running" : "idle"} mutation={chat.runtime?.mutationEffectiveEnabled}/></span><span className="titlebar__name">VARIANT-1</span></div><WorkbenchTitlebarTools/><WindowControls api={api}/></header>
    <div className="workspace"><main className="workbench-view"><Workbench api={api}/></main></div>
    <SectionFooter api={api}/>
    <UtilitySurface kind="runtime" api={api}/>
    <UtilitySurface kind="overview" api={api}/>
    <UtilitySurface kind="automations" api={api}/>
    {settingsOpen ? <Suspense fallback={<SettingsOverlayFallback/>}><SettingsOverlay category={app.settingsCategory} onClose={closeSettings}/></Suspense> : null}
    <ActionPalette/>
    <ToastHost/><StartupCover/>
  </div>;
}
