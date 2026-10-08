import {useLayoutEffect, useRef, useState} from "react";
import {Icon, type IconName} from "../ui/Icon";
import {navigateTo, revealHistory, selectSettingsCategory, useAppState} from "../state/appStore";
import {useAboutState} from "../aboutStore";
import {useQuestionChatIds} from "../state/clarificationStore";
import {useSessionState} from "../state/sessionStore";
import {openBrowser, usePreviewState} from "../workbench/previewStore";
import {isWorkbenchPaneVisible, PANE, togglePane, useWorkbenchState} from "../workbench/workbenchStore";

type PanelTool = "files" | "terminal" | "review" | "browser";
const PANEL_TOOLS: ReadonlyArray<{id: PanelTool; label: string; icon: IconName}> = [
  {id: "files", label: "Files", icon: "folder"},
  {id: "terminal", label: "Terminal", icon: "terminal"},
  {id: "review", label: "Review", icon: "review"},
  {id: "browser", label: "Browser", icon: "browser"},
];

/**
 * Window-header tools beside the window controls: the displayed chat's
 * panels (each toggles its own pane, Files included) and Settings.
 */
export function HeaderTools() {
  const app = useAppState();
  const questionChats = useQuestionChatIds();
  const workbench = useWorkbenchState();
  const previews = usePreviewState();
  const chatId = useSessionState().displayedSessionId || "";
  const browserTabs = previews.tabs.filter(tab => tab.target.kind === "url" && (tab.ownerChatId || "") === chatId);
  const update = useAboutState().update;
  const updatePill = update?.status === "available" ? "Update available"
    : update?.status === "downloading" ? `Update ${Math.round(update.percent)}%`
    : update?.status === "downloaded" ? "Update ready" : "";

  function pressed(kind: PanelTool): boolean {
    if (kind === "browser") return browserTabs.some(tab => isWorkbenchPaneVisible(`preview:${tab.id}`, workbench));
    return isWorkbenchPaneVisible(kind, workbench);
  }

  function toggle(kind: PanelTool): void {
    if (kind === "browser") {
      const tab = browserTabs[browserTabs.length - 1];
      if (!tab) openBrowser();
      else togglePane(`preview:${tab.id}`, "right");
      return;
    }
    togglePane(kind === "terminal" ? PANE.terminal : kind === "review" ? PANE.review : PANE.files, kind === "terminal" ? "bottom" : "right");
  }

  return <div className="titlebar-thread-tools" role="toolbar" aria-label="Thread tools">
    {updatePill ? <button type="button" className="titlebar-thread-tools__update" title={`VARIANT-1 ${update?.version} · open Settings › About`}
      onClick={() => { selectSettingsCategory("about"); navigateTo("settings"); }}>{updatePill}</button> : null}
    {questionChats.length ? <button type="button" className="titlebar-thread-tools__question" aria-label="Chats needing answers" title="Chats needing answers" onClick={revealHistory}>?<span>{questionChats.length}</span></button> : null}
    {PANEL_TOOLS.map(item => {
      const active = pressed(item.id);
      return <button key={item.id} type="button" className={active ? "is-active" : ""} aria-pressed={active} aria-label={item.label} title={item.label} onClick={() => toggle(item.id)}>
        <Icon name={item.icon}/>
      </button>;
    })}
    <i className="titlebar-thread-tools__divider" aria-hidden="true"/>
    <button type="button" data-view="settings" aria-label="Settings" title="Settings" aria-haspopup="dialog" aria-expanded={app.view === "settings"} onClick={() => navigateTo("settings")}>
      <Icon name="settings"/>
    </button>
  </div>;
}

/**
 * When the Agent sessions panel is docked in the window's top-left corner,
 * the header above it takes the panel's surface and title, so the panel
 * reads as one column from the top edge down to the footer. Any other
 * placement (floating, stacked with tabs, compact drawer) keeps the plain
 * header.
 */
export function SessionsRailCap() {
  const workbench = useWorkbenchState();
  const cap = useRef<HTMLDivElement>(null);
  const [docked, setDocked] = useState(false);
  useLayoutEffect(() => {
    let observer: ResizeObserver | null = null;
    const attach = () => {
      observer?.disconnect();
      observer = null;
      const panel = workbench.compact ? null : document.querySelector<HTMLElement>(".workbench .workbench-group:not(.is-floating) .history-panel");
      const workspace = document.querySelector<HTMLElement>(".app-shell > .workspace");
      if (!panel || !workspace) { setDocked(false); return; }
      const measure = () => {
        const p = panel.getBoundingClientRect(), w = workspace.getBoundingClientRect();
        const corner = p.width > 0 && p.height > 0 && Math.abs(p.top - w.top) < 1 && Math.abs(p.left - w.left) < 1;
        // On the header itself, so the centered layout tools can keep clear of it.
        if (corner) cap.current?.parentElement?.style.setProperty("--rail-width", `${p.width}px`);
        setDocked(corner);
      };
      measure();
      observer = new ResizeObserver(measure);
      observer.observe(panel);
      observer.observe(workspace);
    };
    attach();
    // Panes reach the document through NativeSurface portals, which are
    // adopted in their own layout effects after this header's. On launch the
    // panel is not in the document yet, so attach again once a surface lands.
    window.addEventListener("variant1:surface-document", attach);
    return () => { window.removeEventListener("variant1:surface-document", attach); observer?.disconnect(); };
  }, [workbench.layout, workbench.hidden, workbench.compact, workbench.floating, workbench.editMode]);
  return <div ref={cap} className="titlebar__rail" hidden={!docked}><strong>Agent sessions</strong></div>;
}
