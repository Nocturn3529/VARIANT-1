import {useChatWindows} from "../workbench/chatWindowStore";
import {openChatView} from "../workbench/chatViewStore";
import {useEffect, useMemo, useRef, useState} from "react";
import {closePalette, openPalette, usePalette} from "../state/paletteStore";
import {navigateTo, revealHistory, selectSettingsCategory} from "../state/appStore";
import {getSessionState, requestNewSession, switchSession, useSessionState} from "../state/sessionStore";
import {SETTINGS_PAGES} from "../state/settingsCatalog";
import {applyWorkbenchPreset, listWorkbenchPresets, PANE, revealPane} from "../workbench/workbenchStore";
import {getPreviewState, openBrowser} from "../workbench/previewStore";
import {closeNativeWindow, dockAllNativeWindows, focusNativeWindow, useNativeWindows} from "../workbench/nativeWindowStore";
import {Overlay} from "./Overlay";
import {focusMainComposer} from "./SurfaceDocument";
import {Icon, type IconName} from "./Icon";

export type PaletteAction = {id: string; label: string; group: string; icon: IconName; detail?: string; run: () => unknown};
export function filterPaletteActions(actions: readonly PaletteAction[], query: string) {
  const terms = query.toLowerCase().trim().split(/\s+/).filter(Boolean);
  return actions.filter(item => terms.every(term => `${item.label} ${item.group} ${item.detail || ""}`.toLowerCase().includes(term))).slice(0, 60);
}

function PaletteDialog({mode}: {mode: "all" | "windows"}) {
  const sessions = useSessionState();
  const chatWindows=useChatWindows();
  const windows = useNativeWindows();
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const actions = useMemo(() => {
    const items: PaletteAction[] = [];
    if (Object.keys(windows).length) items.push({id: "dock-all", label: "Dock resource panels", group: "Windows", icon: "dock", run: dockAllNativeWindows});
    for (const [id, record] of Object.entries(windows)) {
      items.push({id: `focus:${id}`, label: `Focus ${record.title}`, detail: record.pinned ? "Pinned window" : "Detached window", group: "Windows", icon: "windows", run: () => focusNativeWindow(id)});
      items.push({id: `dock:${id}`, label: `Dock ${record.title}`, group: "Windows", icon: "dock", run: () => closeNativeWindow(id, true)});
    }
    for(const record of chatWindows.windows) {
      const title=sessions.items.find(s=>s.id===record.chat_id)?.title || record.title;
      items.push({id:`focus-chat:${record.chat_id}`,label:`Focus ${title}`,detail:"Detached chat",group:"Chat windows",icon:"history",run:()=>window.variant1Deck?.manageChatWindow?.(record.chat_id,"focus")});
      items.push({id:`layout-chat:${record.chat_id}`,label:`Open ${title} in layout`,detail:"Keep the detached view open",group:"Chat windows",icon:"layout",run:()=>openChatView(record.chat_id,title)});
      items.push({id:`close-chat:${record.chat_id}`,label:`Close window: ${title}`,group:"Chat windows",icon:"close",run:()=>window.variant1Deck?.manageChatWindow?.(record.chat_id,"close")});
    }
    if (mode === "windows") return items;
    items.push(
      {id: "new", label: "New chat", group: "Chat", icon: "plus", run: () => { if (requestNewSession()) focusMainComposer(); }},
      {id: "chats", label: "Show chats", group: "Panels", icon: "history", run: revealHistory},
      {id: "files", label: "Focus Files", group: "Panels", icon: "folder", run: () => revealPane(PANE.files, "right")},
      {id: "review", label: "Focus Review", group: "Panels", icon: "review", run: () => revealPane(PANE.review, "right")},
      {id: "terminal", label: "Focus Terminal", group: "Panels", icon: "terminal", run: () => revealPane(PANE.terminal, "bottom")},
      {id: "browser", label: "Focus Browser", group: "Panels", icon: "browser", run: () => {
        const tab = [...getPreviewState().tabs].reverse().find(item => item.target.kind === "url" && (item.ownerChatId || "") === (getSessionState().displayedSessionId || ""));
        if (tab) revealPane(`preview:${tab.id}`, "right"); else openBrowser();
      }},
      {id: "runtime", label: "Python runtime", group: "Utilities", icon: "kernel", run: () => navigateTo("runtime")},
      {id: "overview", label: "Overview", group: "Utilities", icon: "overview", run: () => navigateTo("overview")},
      {id: "automations", label: "Automations", group: "Utilities", icon: "clock", run: () => navigateTo("automations")},
      {id: "logs", label: "Log monitor", group: "Utilities", icon: "terminal", run: () => window.variant1Deck?.openMonitor?.()},
    );
    for (const page of SETTINGS_PAGES) items.push({id: `settings:${page.id}`, label: page.label, detail: page.description, group: "Settings", icon: "settings", run: () => { selectSettingsCategory(page.id); navigateTo("settings"); }});
    for (const layout of listWorkbenchPresets()) items.push({id: `layout:${layout.id}`, label: layout.name, group: "Layouts", icon: "layout", run: () => applyWorkbenchPreset(layout.id)});
    for (const session of sessions.items) items.push({id: `chat:${session.id}`, label: session.title || "New chat", group: "Chats", icon: "history", detail: session.pinned ? "Pinned conversation" : "Conversation", run: () => { navigateTo("chat"); switchSession(session.id); focusMainComposer(); }});
    return items;
  }, [mode, sessions.items, windows,chatWindows.windows]);
  const results = filterPaletteActions(actions, query);
  const active = Math.min(selected, Math.max(0, results.length - 1));
  useEffect(() => { input.current?.focus(); }, []);
  useEffect(() => { document.getElementById(`palette-result-${active}`)?.scrollIntoView?.({block: "nearest"}); }, [active, query]);
  function choose(action?: PaletteAction) {
    if (!action) return;
    closePalette();
    requestAnimationFrame(() => action.run());
  }
  return <Overlay className="command-palette" labelledBy="palette-title" onClose={closePalette}>
    <header className="command-palette__heading"><h1 id="palette-title">{mode === "windows" ? "Panels & windows" : "Find an action or chat"}</h1><button type="button" aria-label="Close action palette" onClick={closePalette}><Icon name="close"/></button></header>
    {chatWindows.error ? <p role="alert">{chatWindows.error}</p>:null}
    <div className="command-palette__search"><Icon name="search"/><input ref={input} value={query} role="combobox" aria-label="Search actions, panels, and chat titles"
      aria-autocomplete="list" aria-expanded="true" aria-controls="palette-results" aria-activedescendant={results.length ? `palette-result-${active}` : undefined}
      placeholder={mode === "windows" ? "Find a detached window…" : "Actions, panels, settings, chat titles…"}
      onChange={event => { setQuery(event.target.value); setSelected(0); }}
      onKeyDown={event => {
        if (event.key === "ArrowDown" || event.key === "ArrowUp") { event.preventDefault(); setSelected((active + (event.key === "ArrowDown" ? 1 : -1) + results.length) % Math.max(1, results.length)); }
        else if (event.key === "Enter") { event.preventDefault(); choose(results[active]); }
      }}/><kbd>Esc</kbd></div>
    <div className="command-palette__results" id="palette-results" role="listbox" aria-label="Matching actions">
      {results.map((item, index) => <button type="button" tabIndex={-1} role="option" aria-selected={index === active} id={`palette-result-${index}`} key={item.id}
        onMouseMove={() => setSelected(index)} onClick={() => choose(item)}><Icon name={item.icon}/><span><strong>{item.label}</strong>{item.detail ? <small>{item.detail}</small> : null}</span><em>{item.group}</em></button>)}
      {!results.length ? <p className="command-palette__empty">{mode === "windows" && !Object.keys(windows).length ? "No detached windows. Open a chat or panel in a separate window." : "No matches. Try a panel name or chat title."}</p> : null}
    </div>
    <footer className="command-palette__footer"><span><kbd>↑ ↓</kbd> Navigate</span><span><kbd>Enter</kbd> Open</span><span><kbd>Ctrl K</kbd> Actions</span></footer>
  </Overlay>;
}

export function ActionPalette() {
  const state = usePalette();
  const windows = useNativeWindows();
  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && !event.shiftKey && !event.altKey && event.key.toLowerCase() === "k" && !event.isComposing) {
        event.preventDefault(); event.stopPropagation(); openPalette();
      }
    };
    const documents = [document, ...Object.values(windows).flatMap(record => record.document ? [record.document] : [])];
    documents.forEach(owner => owner.addEventListener("keydown", key, true));
    return () => documents.forEach(owner => owner.removeEventListener("keydown", key, true));
  }, [windows]);
  return state.open ? <PaletteDialog key={state.mode} mode={state.mode}/> : null;
}
