import {ChatViewPane,PersistentChatViews} from "./ChatViewPane";
import {useChatViews,chatViewId} from "./chatViewStore";
import {getSessionState,useSessionState} from "../state/sessionStore";
import {chooseChatProject,useChatProjects} from "../state/chatProjectStore";
import {chatPaneId,paneOwner} from "./workbenchStore";
import {openDirectoryPreview} from "./previewStore";
import {Icon, type IconName} from "../ui/Icon";
import {
  Fragment,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type DragEvent as ReactDragEvent,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";
import {ChatDestination} from "../ChatDestination";
import {TerminalPanel} from "../context/TerminalPanel";
import {FilesPanel} from "../context/FilesPanel";
import {ReviewPanel} from "../context/ReviewPanel";
import {getTerminalSnapshot, killTerminal, openNewTerminal, selectTerminal} from "../context/terminalStore";
import {HistoryRail} from "../shell/HistoryRail";
import {useAppState} from "../state/appStore";
import {PopupMenu} from "../ui/PopupMenu";
import {SurfaceDocumentContext, useSurfaceDocument} from "../ui/SurfaceDocument";
import {NativeSurface} from "./NativeSurface";
import {closeNativeWindow, hasNativeWindow, nativePaneKey, useNativeWindows} from "./nativeWindowStore";
import {DECK_BREAKPOINT} from "../layout";
import {allocateTracks, childTracks, isSidePane, paneSide, trackFor, type TrackContext} from "./trackModel";
import type {RuntimeApi} from "../types";
import {PreviewPane, previewPaneTitle} from "./PreviewPane";
import {
  closeOtherPreviews,
  closePreview,
  closePreviewsToRight,
  getPreviewState,
  openBrowser,
  selectPreview,
  usePreviewState,
} from "./previewStore";
import {
  activateFocusedSlot,
  applyWorkbenchPreset,
  closeCompactPane,
  detachWorkbenchPane,
  dockWorkbenchGroup,
  canCloseWorkbenchGroup,closeWorkbenchGroup,flushWorkbenchLayout,
  positionFloatingGroup,
  setWorkbenchCompact,
  closeFocusedPane,
  closePane,
  cycleFocusedGroup,
  forgetPane,
  hidePane,
  getWorkbenchState,
  moveWorkbenchPane,
  noteActiveGroup,
  noteHoveredGroup,
  PANE,
  listWorkbenchPresets,
  reopenLastClosed,
  reorderWorkbenchPane,
  resetWorkbenchLayout,
  revealPane,
  revealPreviewPane,
  selectPane,
  saveWorkbenchPreset,
  deleteWorkbenchPreset,
  setWorkbenchEditMode,
  setWorkbenchSplitWeights,
  setWorkbenchTabStrip,
  toggleGroupMinimized,
  togglePane,
  useWorkbenchState,
  type WorkbenchState,
} from "./workbenchStore";
import {allPaneIds,findGroupOfPane, type DropPosition, type GroupNode, type LayoutNode, type SplitNode} from "./layoutModel";

type PaneDescriptor = {
  id: string;
  label: string;
  icon: IconName;
  close: "never" | "hide" | "close";
  render: () => ReactNode;
  newTab?: () => void;
  dirty?: boolean;
  tabless?: boolean;
  keepAlive?: boolean;
  browser?: boolean;
};

type MenuState = {
  ownerDocument: Document;
  x: number;
  y: number;
  groupId: string;
  paneId: string;
} | null;

const PREVIEW_PREFIX = "preview:";

const previewPaneId = (tabId: string) => `${PREVIEW_PREFIX}${tabId}`;
const previewTabId = (paneId: string) => paneId.startsWith(PREVIEW_PREFIX) ? paneId.slice(PREVIEW_PREFIX.length) : "";

function SplitView({node, renderNode, context}: {
  node: SplitNode;
  renderNode: (node: LayoutNode) => ReactNode;
  context: TrackContext;
}) {
  const host = useRef<HTMLDivElement>(null);
  const [available, setAvailable] = useState(0);
  const items = childTracks(node, node.orientation, context);
  const weights = items.map(item => node.weights[item.index] || 1);
  const dockedCount = items.filter(item => item.track.max > 0).length;
  const sizes = allocateTracks(items.map(item => item.track), available - Math.max(0, dockedCount - 1), weights);
  const startRef = useRef<{pointerId: number; at: number; coordinate: number; sizes: number[]} | null>(null);
  const resizeFrame=useRef(0),pendingResize=useRef<{at:number;sizes:number[];delta:number}|null>(null);
  useEffect(()=>()=>{if(resizeFrame.current)cancelAnimationFrame(resizeFrame.current);pendingResize.current=null;flushWorkbenchLayout();},[]);
  useLayoutEffect(() => {
    const measure = () => {
      const rect = host.current?.getBoundingClientRect();
      const value = node.orientation === "row" ? rect?.width : rect?.height;
      if (value) setAvailable(value);
    };
    measure();
    let frame = 0;
    const observer = new ResizeObserver(() => {
      if (!frame) frame = requestAnimationFrame(() => { frame = 0; measure(); });
    });
    if (host.current) observer.observe(host.current);
    return () => { observer.disconnect(); if (frame) cancelAnimationFrame(frame); document.body.classList.remove("workbench-resizing"); };
  }, [node.orientation]);

  function resize(at: number, initial: number[], delta: number,transient=false): void {
    const nextIndex = items.findIndex((item, index) => index > at && item.track.max > 0);
    if (nextIndex < 0) return;
    const before = items[at].track;
    const after = items[nextIndex].track;
    const low = Math.max(Math.min(before.min, initial[at]) - initial[at], initial[nextIndex] - after.max);
    const high = Math.min(before.max - initial[at], initial[nextIndex] - Math.min(after.min, initial[nextIndex]));
    const change = Math.max(low, Math.min(high, delta));
    const next = [...initial];
    next[at] += change; next[nextIndex] -= change;
    const nextWeights = [...node.weights];
    const fixed = {...node.sizes};
    items.forEach((item, index) => {
      if (item.track.preferred === null) nextWeights[item.index] = Math.max(.05, next[index]);
      else if (item.track.max > item.track.min) fixed[item.child.id] = next[index];
    });
    setWorkbenchSplitWeights(node.id, nextWeights, fixed,transient);
  }

  function end(event: ReactPointerEvent<HTMLDivElement>): void {
    if (startRef.current?.pointerId !== event.pointerId) return;
    if(resizeFrame.current)cancelAnimationFrame(resizeFrame.current);resizeFrame.current=0;
    if(pendingResize.current){const last=pendingResize.current;pendingResize.current=null;resize(last.at,last.sizes,last.delta,true);}
    flushWorkbenchLayout();
    startRef.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    document.body.classList.remove("workbench-resizing");
  }

  const overflow = available > 0 && sizes.reduce((total, size) => total + size, Math.max(0, dockedCount - 1)) > available + 1;
  return <div ref={host} className={`workbench-split workbench-split--${node.orientation}${overflow ? " is-overflowing" : ""}`} data-split-id={node.id}>
    {items.map(({child, track}, index) => <Fragment key={child.id}>
      <div className="workbench-split__child" data-split-child={child.id}
        style={track.max === 0 ? {flex: "0 0 0px"} : available && (items.length > 1 || overflow) ? {flex: `0 0 ${sizes[index]}px`} : {flex: 1}}>{renderNode(child)}</div>
      {track.max > 0 && items.some((item, i) => i > index && item.track.max > 0) ? <div
        className={`workbench-sash workbench-sash--${node.orientation}`}
        role="separator" tabIndex={0} aria-label="Resize panes"
        aria-orientation={node.orientation === "row" ? "vertical" : "horizontal"}
        aria-valuenow={Math.round(sizes[index] || 0)} aria-valuemin={0} aria-valuemax={Math.round(Math.max(available, sizes[index] || 0))}
        onPointerDown={event => {
          if (event.button !== 0) return;
          event.preventDefault();
          startRef.current = {pointerId: event.pointerId, at: index,
            coordinate: node.orientation === "row" ? event.clientX : event.clientY, sizes: [...sizes]};
          event.currentTarget.setPointerCapture(event.pointerId);
          document.body.classList.add("workbench-resizing");
        }}
        onPointerMove={event => {
          const start = startRef.current;
          if (!start || start.pointerId !== event.pointerId) return;
          pendingResize.current={at:start.at,sizes:start.sizes,delta:(node.orientation === "row" ? event.clientX : event.clientY)-start.coordinate};
          if(!resizeFrame.current)resizeFrame.current=requestAnimationFrame(()=>{resizeFrame.current=0;const next=pendingResize.current;pendingResize.current=null;if(next)resize(next.at,next.sizes,next.delta,true);});
        }}
        onPointerUp={end} onPointerCancel={end}
        onKeyDown={event => {
          const keys = node.orientation === "row" ? ["ArrowLeft", "ArrowRight"] : ["ArrowUp", "ArrowDown"];
          if (!keys.includes(event.key)) return;
          event.preventDefault(); resize(index, sizes, event.key === keys[0] ? -16 : 16);
        }}
      /> : null}
    </Fragment>)}
  </div>;
}

function GroupView({
  node,
  descriptors,
  hidden,
  dragging,
  setDragging,
  openMenu,
  parentAxis = "column",
  floating,
  compact = false,
  native = false,
}: {
  node: GroupNode;
  descriptors: ReadonlyMap<string, PaneDescriptor>;
  hidden: Readonly<Record<string, boolean>>;
  dragging: string;
  setDragging: (id: string) => void;
  openMenu: (event: ReactMouseEvent, groupId: string, paneId: string) => void;
  parentAxis?: "row" | "column";
  floating?: WorkbenchState["floating"][string];
  compact?: boolean;
  native?: boolean;
}) {
  const ownerDocument = useSurfaceDocument();
  const surface = useRef<HTMLElement>(null);
  const wasFloating = useRef(false);
  const dragStart = useRef<{x: number; y: number; left: number; top: number} | null>(null);
  const visible = node.panes.filter(id => descriptors.has(id) && !hidden[id]);
  const background=!visible.length && node.panes.some(id=>descriptors.get(id)?.browser);
  const active = visible.includes(node.active) ? node.active : visible[0] || (background ? node.panes.find(id=>descriptors.get(id)?.browser) : undefined);
  const descriptor = active ? descriptors.get(active) : null;
  const [dropPosition, setDropPosition] = useState<DropPosition | "">("");
  const isFloating = !!floating && !compact && !native;
  useEffect(() => {
    if (native && !visible.length && !background) closeNativeWindow(nativePaneKey(node.id));
  }, [native, visible.length, node.id]);
  useLayoutEffect(() => {
    const element = surface.current;
    if (!element) return;
    if (isFloating && !element.matches(":popover-open")) element.showPopover();
    else if (!isFloating && element.matches(":popover-open")) element.hidePopover();
    if (isFloating && !wasFloating.current) element.querySelector<HTMLButtonElement>(".workbench-float-grip")?.focus({preventScroll: true});
    if (!isFloating && wasFloating.current) element.querySelector<HTMLButtonElement>(".workbench-tabs__detach, .history-panel__actions button")?.focus({preventScroll: true});
    wasFloating.current = isFloating;
  }, [isFloating, active]);
  function moveTo(left: number, top: number) {
    const rect = surface.current?.getBoundingClientRect();
    const host = document.querySelector(".workbench")?.getBoundingClientRect();
    if (!rect || !host) return;
    positionFloatingGroup(node.id,
      Math.max(host.left + 8, Math.min(left, host.right - rect.width - 8)),
      Math.max(host.top + 8, Math.min(top, host.bottom - rect.height - 8)));
  }
  useEffect(() => {
    if (!isFloating || !floating) return;
    const resize = () => moveTo(floating.left, floating.top);
    window.addEventListener("resize", resize);
    return () => window.removeEventListener("resize", resize);
  }, [isFloating, floating]);
  if (!descriptor || !active) return null;
  const showTabs = node.minimized || node.tabStrip === "always"
    || (node.tabStrip !== "never" && (visible.length > 1 || !descriptor.tabless));

  function drop(event: ReactDragEvent<HTMLDivElement>): void {
    event.preventDefault();
    const paneId = dragging || event.dataTransfer.getData("application/x-variant1-pane");
    if (paneId && paneId !== active) moveWorkbenchPane(paneId, node.id, dropPosition || "center");
    setDropPosition("");
    setDragging("");
  }

  function locateDrop(event: ReactDragEvent<HTMLElement>): DropPosition {
    const rect = event.currentTarget.getBoundingClientRect();
    const x = (event.clientX - rect.left) / Math.max(1, rect.width);
    const y = (event.clientY - rect.top) / Math.max(1, rect.height);
    if (x < 0.22) return "left";
    if (x > 0.78) return "right";
    if (y < 0.22) return "top";
    if (y > 0.78) return "bottom";
    return "center";
  }

  return <section
    ref={surface}
    popover={isFloating ? "manual" : undefined}
    aria-label={isFloating ? `Detached ${descriptor.label} panel` : undefined}
    style={background ? {position:"fixed",left:-12000,top:0,width:900,height:700,visibility:"hidden",pointerEvents:"none"} : isFloating ? floating : undefined}
    aria-hidden={background || undefined}
    className={`workbench-group${isFloating ? " is-floating" : ""}${node.minimized ? ` is-minimized is-minimized--${parentAxis}` : ""}${dragging ? " is-dragging" : ""}`}
    data-group-id={node.id}
    onPointerDown={() => noteActiveGroup(node.id)}
    onPointerEnter={() => noteHoveredGroup(node.id)}
    onPointerLeave={() => noteHoveredGroup("")}
    onDragOver={event => { if (dragging || event.dataTransfer.types.includes("application/x-variant1-pane")) { event.preventDefault(); setDropPosition(locateDrop(event)); } }}
    onDragLeave={event => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDropPosition(""); }}
    onDrop={drop}
  >
    {isFloating ? <header className="workbench-float-heading">
      <button type="button" className="workbench-float-grip" aria-label={`Move ${descriptor.label} panel; use arrow keys`} onPointerDown={event => {
        if (event.button !== 0) return;
        const rect = surface.current!.getBoundingClientRect();
        dragStart.current = {x: event.clientX, y: event.clientY, left: rect.left, top: rect.top};
        event.currentTarget.setPointerCapture(event.pointerId);
      }} onPointerMove={event => {
        const start = dragStart.current;
        if (start) moveTo(start.left + event.clientX - start.x, start.top + event.clientY - start.y);
      }} onPointerUp={() => { dragStart.current = null; }} onPointerCancel={() => { dragStart.current = null; }}
        onKeyDown={event => {
          const offsets: Record<string, number[]> = {ArrowLeft: [-16, 0], ArrowRight: [16, 0], ArrowUp: [0, -16], ArrowDown: [0, 16]};
          const offset = offsets[event.key];
          if (offset && floating) { event.preventDefault(); moveTo(floating.left + offset[0], floating.top + offset[1]); }
        }}>⠿ <span>{descriptor.label}</span></button>
      <button type="button" aria-label={`Dock ${descriptor.label} panel`} onClick={() => dockWorkbenchGroup(node.id)}>Dock</button>
      <button type="button" aria-label={`Hide ${descriptor.label} panel`} onClick={() => { node.panes.forEach(id => hidePane(id)); dockWorkbenchGroup(node.id); }}><Icon name="close"/></button>
    </header> : null}
    {showTabs ? <header className="workbench-tabs" role="tablist" aria-label="Pane tabs">
      <div className="workbench-tabs__scroll">
        {visible.map((paneId, index) => {
          const item = descriptors.get(paneId)!;
          const selected = active === paneId;
          return <div
            className={`workbench-tab${selected ? " is-active" : ""}`}
            data-pane-tab={paneId}
            draggable={!native}
            onDragStart={event => {
              setDragging(paneId);
              event.dataTransfer.effectAllowed = "move";
              event.dataTransfer.setData("application/x-variant1-pane", paneId);
            }}
            onDragEnd={() => setDragging("")}
            onDragOver={event => event.preventDefault()}
            onDrop={event => {
              event.stopPropagation();
              const moved = dragging || event.dataTransfer.getData("application/x-variant1-pane");
              if (moved) reorderWorkbenchPane(node.id, moved, paneId);
              setDragging("");
            }}
            onAuxClick={event => { if (event.button === 1 && item.close !== "never") closePane(paneId); }}
            onContextMenu={event => openMenu(event, node.id, paneId)}
            key={paneId}
          >
            <button
              type="button"
              role="tab"
              aria-selected={selected}
              tabIndex={selected ? 0 : -1}
              onClick={() => {
                selectPane(node.id, paneId);
                const tabId = previewTabId(paneId);
                if (tabId) selectPreview(tabId);
              }}
              onKeyDown={event => {
                if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
                  event.preventDefault();
                  const direction = event.key === "ArrowRight" ? 1 : -1;
                  const next = visible[(index + direction + visible.length) % visible.length];
                  selectPane(node.id, next);
                  ownerDocument.querySelector<HTMLElement>(`[data-pane-tab="${CSS.escape(next)}"] button`)?.focus();
                }
              }}
            >
              <Icon name={item.icon}/>
              <span>{item.label}</span>
              {item.dirty ? <em aria-label="Unsaved changes"/> : null}
            </button>
            {item.close !== "never" ? <button className="workbench-tab__close" aria-label={`Close ${item.label}`} onClick={() => {
              closePane(paneId);
            }}><Icon name="close"/></button> : null}
          </div>;
        })}
      </div>
      {descriptor.newTab ? <button className="workbench-tabs__new" aria-label={`New ${descriptor.label}`} onClick={descriptor.newTab}>+</button> : null}
      {(!compact || window.variant1Deck?.supportsNativeWindows) && !isFloating && !native && !node.panes.includes(PANE.workspace) && !node.panes.includes(PANE.history) ? <button type="button" className="workbench-tabs__detach" aria-label={`Detach ${descriptor.label} panel`}
        title={node.panes.some(id => descriptors.get(id)?.browser) ? "Open in a separate window (browser pages reload)" : "Detach panel"}
        aria-description={node.panes.some(id => descriptors.get(id)?.browser) ? "Moving this panel reloads its browser pages at their current URLs." : undefined}
        onClick={() => detachWorkbenchPane(active)}><Icon name="popout"/></button> : null}
      {!native ? <button className="workbench-tabs__collapse" aria-label={node.minimized ? "Restore pane" : "Minimize pane"} onClick={() => toggleGroupMinimized(node.id)}><Icon name="down" className={node.minimized ? "is-reversed" : ""}/></button> : null}
    </header> : null}
    <div className="workbench-group__content" data-pane-id={active} hidden={node.minimized}>
      {node.panes.filter(paneId => (visible.includes(paneId) && paneId === active) || descriptors.get(paneId)?.keepAlive).map(paneId => {
        const pane = descriptors.get(paneId)!;
        return <div
          className={`workbench-pane-layer${paneId === active ? " is-active" : ""}`}
          aria-hidden={paneId === active ? undefined : true}
          inert={paneId === active ? undefined : true}
          key={paneId}
        >{pane.render()}</div>;
      })}
    </div>
    {dragging && dropPosition ? <div className={`workbench-drop workbench-drop--${dropPosition}`} aria-hidden="true"/> : null}
  </section>;
}

function PaneMenu({menu, close}: {menu: Exclude<MenuState, null>; close: () => void}) {
  const tabId = previewTabId(menu.paneId);
  const preview = usePreviewState();
  const index = preview.tabs.findIndex(tab => tab.id === tabId);
  const selected = preview.tabs[index];
  const native = hasNativeWindow(nativePaneKey(menu.groupId));
  const group = findGroupOfPane(getWorkbenchState().layout, menu.paneId);
  const reloadsBrowser = group?.panes.some(id => preview.tabs.some(tab => previewPaneId(tab.id) === id && tab.target.kind === "url"));
  function action(run: () => void): void { run(); close(); }
  return <SurfaceDocumentContext.Provider value={menu.ownerDocument}><PopupMenu className="workbench-menu" x={menu.x} y={menu.y} onClose={close}>
    {menu.paneId !== PANE.workspace && menu.paneId !== PANE.history ? <button role="menuitem" title={reloadsBrowser ? "Browser pages reload when moving between windows" : undefined}
      onClick={() => action(() => native ? dockWorkbenchGroup(menu.groupId) : detachWorkbenchPane(menu.paneId))}>{native ? "Dock panel" : "Detach panel"}</button> : null}
    {selected?.target.kind === "url" ? <>
      <button role="menuitem" onClick={() => action(() => openBrowser("about:blank", {newTab: true,ownerChatId:selected?.ownerChatId}))}>New browser tab</button>
      <button role="menuitem" disabled={!selected.target.url || selected.target.url === "about:blank"} onClick={() => action(() => window.variant1Deck?.openExternal?.(selected.target.url))}>Open external</button>
      <hr/>
    </> : null}
    {!native ? <>
    <button role="menuitem" onClick={() => action(() => moveWorkbenchPane(menu.paneId, menu.groupId, "left"))}>Split left</button>
    <button role="menuitem" onClick={() => action(() => moveWorkbenchPane(menu.paneId, menu.groupId, "right"))}>Split right</button>
    <button role="menuitem" onClick={() => action(() => moveWorkbenchPane(menu.paneId, menu.groupId, "top"))}>Split up</button>
    <button role="menuitem" onClick={() => action(() => moveWorkbenchPane(menu.paneId, menu.groupId, "bottom"))}>Split down</button>
    <hr/>
    <button role="menuitem" onClick={() => action(() => setWorkbenchTabStrip(menu.groupId, "never"))}>Hide tab strip</button>
    <button role="menuitem" onClick={() => action(() => toggleGroupMinimized(menu.groupId))}>Minimize zone</button>
    </> : null}
    {tabId ? <>
      <hr/>
      <button role="menuitem" onClick={() => action(() => closeOtherPreviews(tabId))}>Close others</button>
      <button role="menuitem" disabled={index < 0 || index === preview.tabs.length - 1} onClick={() => action(() => closePreviewsToRight(tabId))}>Close to right</button>
      <button role="menuitem" onClick={() => action(() => closePreview(tabId))}>Close</button>
    </> : menu.paneId !== PANE.workspace ? <button role="menuitem" onClick={() => action(() => closePane(menu.paneId))}>Hide</button> : null}
  </PopupMenu></SurfaceDocumentContext.Provider>;
}

export function Workbench({api}: {api: RuntimeApi | null}) {
  const nativeWindows = useNativeWindows();
  const sessions=useSessionState();
  const chatViews=useChatViews();
  const projects=useChatProjects();
  const chatId=sessions.displayedSessionId || "";
  const app = useAppState();
  const state = useWorkbenchState();
  const preview = usePreviewState();
  const knownProjects=useRef<Record<string,string>>({});
  useEffect(()=>{
    const root=projects.projects[chatId]?.root;
    if(chatId && root && (!findGroupOfPane(getWorkbenchState().layout,chatPaneId("files",chatId)) || (knownProjects.current[chatId] && knownProjects.current[chatId]!==root)))revealPane(chatPaneId("files",chatId),"right");
    if(chatId)knownProjects.current[chatId]=root || "";
  },[chatId,projects.projects[chatId]?.root]);
  const previousPreviewIds = useRef<Set<string>>(new Set());
  const [dragging, setDragging] = useState("");
  const [menu, setMenu] = useState<MenuState>(null);
  const [presetId, setPresetId] = useState("default");
  const [presetRevision, setPresetRevision] = useState(0);
  const [presetName, setPresetName] = useState<string | null>(null);
  const [savingPreset, setSavingPreset] = useState(false);
  const [presetError, setPresetError] = useState("");

  useEffect(() => {
    const current = new Set(preview.tabs.map(tab => previewPaneId(tab.id)));
    for (const paneId of current) {
      if (!findPreviewPaneInLayout(paneId)) {
        revealPreviewPane(previewTabId(paneId));
      }
    }
    for (const paneId of previousPreviewIds.current) {
      if (!current.has(paneId)) forgetPane(paneId);
    }
    previousPreviewIds.current = current;
  }, [preview.tabs, state.layout]);

  useEffect(() => {
    if (!preview.selectedId || preview.tabs.find(t=>t.id===preview.selectedId)?.ownerChatId !== chatId) return;
    const paneId = previewPaneId(preview.selectedId);
    const group = findPreviewPaneInLayout(paneId);
    if (group) selectPane(group, paneId);
  }, [preview.selectedId]);


  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      const ownerDocument = event.currentTarget as Document;
      if (event.defaultPrevented || (ownerDocument === document && app.view !== "chat") || ownerDocument.querySelector("dialog[open], [data-deck-menu]")) return;
      const command = event.ctrlKey || event.metaKey;
      if (!command) return;
      const groupId = ownerDocument !== document ? ownerDocument.querySelector<HTMLElement>(".workbench-group")?.dataset.groupId : undefined;
      const nativePane=ownerDocument !== document ? ownerDocument.querySelector<HTMLElement>("[data-pane-id]")?.dataset.paneId || "" : "";
      const actionChat=paneOwner(nativePane) || preview.tabs.find(t=>previewPaneId(t.id)===nativePane)?.ownerChatId || chatId;
      if (groupId) { noteHoveredGroup(""); noteActiveGroup(groupId); }
      if (command && event.key.toLowerCase() === "j") {
        event.preventDefault(); togglePane(chatPaneId(PANE.files,actionChat), "right");
      } else if (command && event.key.toLowerCase() === "g") {
        event.preventDefault(); togglePane(chatPaneId(PANE.review,actionChat), "right");
      } else if (command && event.shiftKey && event.key.toLowerCase() === "l") {
        event.preventDefault();
        if (!preview.tabs.some(tab => tab.target.kind === "url" && tab.ownerChatId === actionChat)) openBrowser("about:blank",{ownerChatId:actionChat});
        else revealPane(previewPaneId([...preview.tabs].reverse().find(tab => tab.target.kind === "url" && tab.ownerChatId === actionChat)!.id), "right");
      } else if (event.ctrlKey && event.key === "`") {
        event.preventDefault();
        if (event.shiftKey) { revealPane(chatPaneId(PANE.terminal,actionChat), "bottom"); void openNewTerminal(undefined,actionChat); }
        else togglePane(chatPaneId(PANE.terminal,actionChat), "bottom");
      } else if (event.ctrlKey && event.shiftKey && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
        const terminal = getTerminalSnapshot(actionChat);
        if (terminal.terminals.length) {
          event.preventDefault();
          const index = Math.max(0, terminal.terminals.findIndex(item => item.id === terminal.activeId));
          const direction = event.key === "ArrowDown" ? 1 : -1;
          selectTerminal(terminal.terminals[(index + direction + terminal.terminals.length) % terminal.terminals.length].id,actionChat);
          revealPane(chatPaneId(PANE.terminal,actionChat), "bottom");
        }
      } else if (event.ctrlKey && event.shiftKey && event.key.toLowerCase() === "w") {
        event.preventDefault(); killTerminal(actionChat);
      } else if (command && event.key.toLowerCase() === "w") {
        if ((event.target as HTMLElement | null)?.closest("input,textarea,[contenteditable=true]")) return;
        event.preventDefault(); closeFocusedPane();
      } else if (command && event.shiftKey && event.key.toLowerCase() === "t") {
        event.preventDefault(); reopenLastClosed();
      } else if (event.ctrlKey && event.key === "Tab") {
        event.preventDefault(); cycleFocusedGroup(event.shiftKey ? -1 : 1);
      } else if (command && /^[1-9]$/.test(event.key)) {
        if (activateFocusedSlot(Number(event.key))) event.preventDefault();
      } else if (command && event.key === "\\") {
        event.preventDefault();
        if (event.shiftKey) setWorkbenchEditMode(!getWorkbenchState().editMode);

      }
    };
    const documents = [document, ...Object.entries(nativeWindows).flatMap(([id, record]) => id.startsWith("pane:") && record.document ? [record.document] : [])];
    documents.forEach(owner => owner.addEventListener("keydown", key));
    return () => documents.forEach(owner => owner.removeEventListener("keydown", key));
  }, [preview.tabs, state.editMode, app.view, nativeWindows]);

  const descriptors = useMemo(() => {
    const rows = new Map<string, PaneDescriptor>();
    rows.set(PANE.history, {id: PANE.history, label: "Chats", icon: "history", close: "hide", tabless: true, render: () => <div className="history-panel"><HistoryRail/></div>});
    rows.set(PANE.workspace, {id: PANE.workspace, label: sessions.items.find(s=>s.id===chatId)?.title || "Chat", icon: "kernel", close: "never", tabless: !chatViews.length, keepAlive: true, render: () => <main className="chat-workspace"><ChatDestination/></main>});
    for(const view of chatViews) {
      const title=sessions.items.find(s=>s.id===view.id)?.title || view.title;
      rows.set(chatViewId(view.id),{id:chatViewId(view.id),label:title,icon:"history",close:"close",keepAlive:true,render:()=> <ChatViewPane chatId={view.id} title={title}/>});
    }
    for(const session of sessions.items) {
      const owner=session.id, project=projects.projects[owner] !== undefined ? projects.projects[owner] : session.project;
      const picker=<div className="workbench-preview__state"><p>No project selected for this chat.</p><button onClick={()=>void chooseChatProject(owner)}>Choose project folder</button>{projects.errors[owner] ? <p role="alert">{projects.errors[owner]}</p>:null}</div>;
      const moreFiles=async()=>{const path=await api?.pickFolder?.();if(path)openDirectoryPreview(path,owner);};
      rows.set(chatPaneId("files",owner), {id:chatPaneId("files",owner),label:project?.name || "Files",icon:"folder",close:"hide",newTab:()=>void moreFiles(),render:()=> <>{project ? <FilesPanel key={project.root} directory={project.root} chatId={owner} onChooseProject={()=>void chooseChatProject(owner)}/> : picker}</>});
      rows.set(chatPaneId("review",owner),{id:chatPaneId("review",owner),label:"Review",icon:"review",close:"hide",render:()=>project ? <ReviewPanel key={project.root} directory={project.root} chatId={owner}/> : picker});
      rows.set(chatPaneId("terminal",owner),{id:chatPaneId("terminal",owner),label:"Terminal",icon:"terminal",close:"hide",render:()=> <TerminalPanel chatId={owner}/>});
    }
    for (const tab of preview.tabs) {
      rows.set(previewPaneId(tab.id), {
        id: previewPaneId(tab.id),
        label: previewPaneTitle(tab),
        icon: tab.target.kind === "url" ? "browser" : tab.target.kind === "directory" ? "folder" : tab.target.kind === "file" ? "file" : "image",
        close: "close",
        render: () => <PreviewPane tabId={tab.id} api={api}/>,
        newTab: tab.target.kind === "url" ? () => openBrowser("about:blank", {newTab: true,ownerChatId:tab.ownerChatId}) : undefined,
        dirty: !!tab.dirty,
        keepAlive: true,
        browser: tab.target.kind === "url",
      });
    }
    return rows;
  }, [api, preview.tabs, preview.pages, sessions.items, projects,chatViews,chatId]);

  const hidden: Record<string, boolean> = {...state.hidden, [PANE.workspace]: false,files:true,review:true,terminal:true};
  for(const id of descriptors.keys()) {
    const owner=paneOwner(id) || (id.startsWith("preview:") ? preview.tabs.find(t=>previewPaneId(t.id)===id)?.ownerChatId : "");
    const group=findGroupOfPane(state.layout,id);
    if(owner && (owner!==chatId || state.hidden[`right:${owner}`]) && !(group && hasNativeWindow(nativePaneKey(group.id))))hidden[id]=true;
    if(id.startsWith("preview:") && !owner && chatId)hidden[id]=true;
  }

  const nativeGroups = new Set(Object.keys(nativeWindows).filter(id => id.startsWith("pane:")).map(id => id.slice(5)));
  const context: TrackContext = {hidden, known: new Set(descriptors.keys()), compact: state.compact, height: window.innerHeight, floatingGroups: new Set(Object.keys(state.floating)), nativeGroups};
  const gridHidden = {...hidden};
  if (state.compact) for (const id of descriptors.keys()) if (isSidePane(id)) gridHidden[id] = true;

  const groupView = (node: GroupNode, parentAxis: "row" | "column", groupHidden = gridHidden) => <NativeSurface id={nativePaneKey(node.id)} title={`${descriptors.get(node.active)?.label || "Panel"} · ${sessions.items.find(s=>s.id === (paneOwner(node.active) || preview.tabs.find(t=>previewPaneId(t.id)===node.active)?.ownerChatId))?.title || "VARIANT-1"}`}
    onBeforeClose={()=>canCloseWorkbenchGroup(node.id)} onClosed={dock => { if(!dock)closeWorkbenchGroup(node.id,true);else if(state.compact && isSidePane(node.active))revealPane(node.active); }}><GroupView
    node={node} descriptors={descriptors} hidden={nativeGroups.has(node.id) ? hidden : groupHidden} parentAxis={parentAxis}
    native={nativeGroups.has(node.id)}
    floating={state.floating[node.id]} compact={state.compact}
    dragging={dragging} setDragging={setDragging}
    openMenu={(event, groupId, paneId) => {
      event.preventDefault(); event.stopPropagation();
      setMenu({x: event.clientX, y: event.clientY, groupId, paneId, ownerDocument: event.currentTarget.ownerDocument});
    }}
  /></NativeSurface>;
  const renderNode = (node: LayoutNode, parentAxis: "row" | "column" = "row"): ReactNode => {
    if (!trackFor(node, parentAxis, context)) return null;
    if (node.type === "group") return groupView(node, parentAxis);
    const visible = childTracks(node, node.orientation, context);
    // Keep the wrapper identity when neighbors appear/disappear. Only the
    // effective collapse axis passes through a single-child split.
    return <SplitView node={node} context={context} renderNode={child => renderNode(child, visible.length === 1 ? parentAxis : node.orientation)}/>;
  };
  const overlayId = state.compact ? state.overlayPaneId : null;
  const overlayGroup = overlayId ? findGroupOfPane(state.layout, overlayId) : null;
  const overlayTitle = overlayId ? descriptors.get(overlayId)?.label : "";
  const overlayRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const update = () => setWorkbenchCompact(window.innerWidth <= DECK_BREAKPOINT.narrow);
    update();
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
  }, []);
  useEffect(() => {
    if (!overlayId) return;
    const previous = document.activeElement as HTMLElement | null;
    overlayRef.current?.querySelector<HTMLButtonElement>("button")?.focus();
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || document.querySelector("dialog[open], [data-deck-menu], .runtime-session-menu")) return;
      event.preventDefault(); event.stopImmediatePropagation(); closeCompactPane();
    };
    document.addEventListener("keydown", escape, true);
    return () => {
      document.removeEventListener("keydown", escape, true);
      requestAnimationFrame(() => { if (previous?.isConnected && document.activeElement === document.body) previous.focus(); });
    };
  }, [overlayId]);

  return <div onDragStart={event=>setDragging(event.dataTransfer.getData("application/x-variant1-pane"))} onDragEnd={()=>setDragging("")} className={`workbench${state.editMode ? " is-editing" : ""}${overlayId === PANE.history ? " history-mobile-open" : ""}`} style={{"--workbench-titlebar-offset": "0px"} as CSSProperties}>
    {renderNode(state.layout)}
    {overlayGroup && overlayId && !hidden[overlayId] ? <>
      <button className="workbench-overlay-dismiss" tabIndex={-1} aria-label="Dismiss side panel" onClick={() => closeCompactPane()}/>
      <section ref={overlayRef} className={`workbench-side-overlay workbench-side-overlay--${paneSide(state.layout, overlayId)}`} aria-label={`${overlayTitle} panel`}>
        <header className="workbench-overlay-heading"><strong>{overlayTitle}</strong><button aria-label={`Close ${overlayTitle} panel`} onClick={() => closeCompactPane()}><Icon name="close"/></button></header>
        {groupView({...overlayGroup, panes: overlayGroup.panes.filter(id => isSidePane(id) && !hidden[id]), active: overlayId, minimized: false, tabStrip: "always"}, "column", hidden)}
      </section>
    </> : null}
    <PersistentChatViews views={chatViews.filter(view=>allPaneIds(state.layout).includes(chatViewId(view.id)))} dragging={!!dragging}/>
    {state.editMode ? <div className="workbench-editbar">
      <strong>Layout editing</strong>
      <select aria-label="Layout preset" value={presetId} onChange={event => {
        const id = event.target.value;
        setPresetId(id);
        applyWorkbenchPreset(id);
      }}>
        {listWorkbenchPresets().map(preset => <option value={preset.id} key={`${preset.id}:${presetRevision}`}>{preset.name}</option>)}
      </select>
      <button onClick={resetWorkbenchLayout}>Reset</button>
      {presetName === null ? <button onClick={() => { setPresetName(""); setPresetError(""); }}>Save as</button> : <form className="workbench-preset-form" onSubmit={async event => {
        event.preventDefault();
        if (!presetName.trim() || savingPreset) return;
        setSavingPreset(true);
        const id = await saveWorkbenchPreset(presetName);
        setSavingPreset(false);
        if (id) { setPresetId(id); setPresetRevision(value => value + 1); setPresetName(null); }
        else setPresetError("Could not save this layout. Try again.");
      }}>
        <input autoFocus aria-label="Layout name" disabled={savingPreset} value={presetName} maxLength={80} onChange={event => setPresetName(event.target.value)}
          onKeyDown={event => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); setPresetName(null); } }}/>
        <button type="submit" disabled={!presetName.trim() || savingPreset}>{savingPreset ? "Saving…" : "Save"}</button><button type="button" disabled={savingPreset} onClick={() => setPresetName(null)}>Cancel</button>
        {presetError ? <span role="alert">{presetError}</span> : null}
      </form>}
      {listWorkbenchPresets().find(preset => preset.id === presetId)?.user ? <button onClick={() => {
        deleteWorkbenchPreset(presetId);
        setPresetId("default");
        setPresetRevision(value => value + 1);
      }}>Delete</button> : null}
      <button onClick={() => setWorkbenchEditMode(false)}>Done</button>
    </div> : null}
    {menu ? <PaneMenu menu={menu} close={() => setMenu(null)}/> : null}
  </div>;
}

function findPreviewPaneInLayout(paneId: string): string {
  const root = getWorkbenchState().layout;
  const search = (node: LayoutNode): string => {
    if (node.type === "group") return node.panes.includes(paneId) ? node.id : "";
    for (const child of node.children) {
      const match = search(child);
      if (match) return match;
    }
    return "";
  };
  return search(root);
}

export function toggleWorkbenchPane(id: "files" | "review" | "terminal" | "browser"): void {
  if (id === "browser") {
    const browser = [...getPreviewTabs()].reverse().find(tab => tab.target.kind === "url");
    if (!browser) openBrowser();
    else togglePane(previewPaneId(browser.id), "right");
    return;
  }
  togglePane(id, id === "terminal" ? "bottom" : "right");
}

function getPreviewTabs() {
  return getPreviewState().tabs.filter(t=>(t.ownerChatId || "") === (getSessionState().displayedSessionId || ""));
}
