import {detachedChatId} from "../runtime/viewIdentity";
import {notifyToast} from "../state/toastStore";
import {unregisterChatView,registerChatView} from "./chatViewStore";
import {getSessionState} from "../state/sessionStore";
import {dockAllNativeWindows, nativeWindowPlacements, parseNativePlacements, type NativeWindowPlacement} from "./nativeWindowStore";
import {createExternalStore} from "../state/createModuleStore";
import {DECK_BREAKPOINT} from "../layout";
import {canClosePreviewTabs,closePreview,closePreviewTabs,getPreviewState,reopenPreview} from "./previewStore";
import {isSidePane} from "./trackModel";
import {browserWindowSize, DEFAULT_BROWSER_VIEWPORT} from "./browserViewport";
import {closeNativePaneWindows, closeNativeWindow, focusNativeWindow, hasNativeWindow, nativePaneKey, openNativeWindow} from "./nativeWindowStore";
import {
  allPaneIds,
  findGroupOfPane,
  group,
  insertAtGroup,
  isLayoutNode,
  movePane,
  normalize,
  removePane,
  reorderPane,
  setActivePane,
  setGroupMinimized,
  setGroupTabStrip,
  split,
  updateSplitWeights,
  type DropPosition,
  type LayoutNode,
  type TabStripMode,
} from "./layoutModel";

export const PANE = {
  history: "history",
  workspace: "workspace",
  files: "files",
  review: "review",
  terminal: "terminal",
} as const;


export function chatPaneId(kind:string,chatId=getSessionState().displayedSessionId || ""):string {
  return ["files","review","terminal"].includes(kind) && chatId ? `owned:${kind}:${chatId}` : kind;
}
export function paneOwner(id:string):string {return id.startsWith("owned:") ? id.split(":").slice(2).join(":") : "";}

const LAYOUT_KEY = "variant1.workbench.layout.v1";
const HIDDEN_KEY = "variant1.workbench.hidden.v1";
const CLOSED_KEY = "variant1.workbench.closed.v1";
const PRESETS_KEY = "variant1.workbench.presets.v1";

export type WorkbenchState = Readonly<{
  floating: Readonly<Record<string, {left: number; top: number; width: number; height: number}>>;
  layout: LayoutNode;
  hidden: Readonly<Record<string, boolean>>;
  activeGroupId: string;
  hoveredGroupId: string;
  editMode: boolean;
  lastClosed: readonly string[];
  compact: boolean;
  overlayPaneId: string | null;
}>;

function defaultLayout(): LayoutNode {
  return split("row", [
    group([PANE.history], {id: "group-history"}),
    group([PANE.workspace], {id: "group-workspace"}),
    split("column", [
      split("row", [
        group([PANE.review], {id: "group-review"}),
        group([PANE.files], {id: "group-files"}),
      ], [1, 1.2], "split-right-top"),
      group([PANE.terminal], {id: "group-terminal", minimized: true}),
    ], [1.6, 1], "split-right"),
  ], [1, 3.4, 1.25], "split-root");
}

function builtInPreset(id: string): LayoutNode | null {
  if (id === "default") return defaultLayout();
  if (id === "focus") return split("row", [
    group([PANE.history]),
    group([PANE.workspace, PANE.files, PANE.review, PANE.terminal], {active: PANE.workspace}),
  ], [1, 4.6]);
  if (id === "terminal-deck") return split("column", [
    split("row", [group([PANE.history]), group([PANE.workspace]), group([PANE.files, PANE.review])], [1, 3.2, 1.2]),
    group([PANE.terminal]),
  ], [3, 1]);
  if (id === "quad") return split("column", [
    split("row", [group([PANE.history, PANE.files]), group([PANE.workspace])], [1, 3]),
    split("row", [group([PANE.terminal]), group([PANE.review])], [1.4, 1]),
  ], [3, 1]);
  return null;
}

export type WorkbenchPreset = {id: string; name: string; user: boolean};

function presetPane(id: string): string {
  if (id.startsWith("preview:") || id.startsWith("chatview:")) return "";
  return id.startsWith("owned:") ? id.split(":")[1] : id;
}
function mapPresetLayout(node: LayoutNode, owner: string): LayoutNode {
  return node.type === "group" ? {...node,
    panes: node.panes.map(presetPane).filter(Boolean).map(id => chatPaneId(id, owner)),
    active: chatPaneId(presetPane(node.active), owner)} : {...node, children: node.children.map(child => mapPresetLayout(child, owner))};
}
function presetHidden(hidden: Readonly<Record<string, boolean>>, owner: string): Record<string, boolean> {
  return Object.fromEntries(Object.entries(hidden).flatMap(([id, value]) => {
    const key = id.startsWith("right:") ? (owner ? `right:${owner}` : "right") : id === "right" ? (owner ? `right:${owner}` : "right") : chatPaneId(presetPane(id), owner);
    return key ? [[key, value]] : [];
  }));
}

function userPresets(): Record<string, {name: string; layout: LayoutNode; hidden?: Record<string, boolean>; windows: NativeWindowPlacement[]}> {
  try {
    const raw: unknown = JSON.parse(window.localStorage?.getItem(PRESETS_KEY) || "{}");
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
    return Object.fromEntries(Object.entries(raw as Record<string, unknown>).flatMap(([id, value]) => {
      if (!value || typeof value !== "object") return [];
      const row = value as Record<string, unknown>;
      return typeof row.name === "string" && isLayoutNode(row.layout) ? [[id, {name: row.name, layout: row.layout, windows: parseNativePlacements(row.windows),
        hidden: row.hidden && typeof row.hidden === "object" ? Object.fromEntries(Object.entries(row.hidden).filter(([, value]) => typeof value === "boolean")) as Record<string, boolean> : undefined}]] : [];
    }));
  } catch { return {}; }
}

export function listWorkbenchPresets(): WorkbenchPreset[] {
  return [
    {id: "default", name: "Default", user: false},
    {id: "focus", name: "Focus", user: false},
    {id: "terminal-deck", name: "Terminal deck", user: false},
    {id: "quad", name: "Quad", user: false},
    ...Object.entries(userPresets()).map(([id, value]) => ({id, name: value.name, user: true})),
  ];
}

function withLivePanes(layout:LayoutNode,owner:string,focus=false):LayoutNode {
    const previews=getPreviewState().tabs.filter(t=>(t.ownerChatId || "")===owner);
    const destination=findGroupOfPane(layout,focus ? PANE.workspace : chatPaneId(PANE.files,owner)) || findGroupOfPane(layout,PANE.workspace);
    if(destination)for(const tab of previews)layout=insertAtGroup(layout,destination.id,`preview:${tab.id}`,"center") || layout;
    const chatGroup=findGroupOfPane(layout,PANE.workspace);
    if(chatGroup)for(const pane of allPaneIds(store.getState().layout).filter(p=>p.startsWith("chatview:")))layout=insertAtGroup(layout,chatGroup.id,pane,"center") || layout;
    if(chatGroup)layout=setActivePane(layout,chatGroup.id,PANE.workspace);
  return layout;
}

export function applyWorkbenchPreset(id: string): boolean {
  const saved = userPresets()[id];
  const base = builtInPreset(id) || saved?.layout;
  const owner=getSessionState().displayedSessionId || "";
  let layout=base ? normalize(mapPresetLayout(base, owner)) : null;
  if(layout)layout=withLivePanes(layout,owner,id==="focus");
  if (!layout) return false;
  dockAllNativeWindows();
  // Bare keys hide legacy placeholders. Explicit built-in presets reveal their
  // current-chat tools; saved user visibility is remapped to owned IDs instead.
  replace({layout: structuredClone(layout), floating: {}, hidden: saved?.hidden ? {...presetHidden(saved.hidden, owner),[PANE.workspace]:false} : {[PANE.files]:true,[PANE.review]:true,[PANE.terminal]:true}});
  for (const placement of saved?.windows || []) {
    const isPane = placement.id.startsWith("pane:");
    const group = isPane ? findGroupById(layout, placement.id.slice(5)) : null;
    if (isPane && (!group || group.panes.some(id => id === PANE.workspace || id === PANE.history || id.startsWith("chatview:")))) continue;
    openNativeWindow(placement.id, placement.title, {...placement, screen: true});
  }
  return true;
}

export async function saveWorkbenchPreset(name: string): Promise<string | null> {
  const trimmed = name.trim();
  if (!trimmed) return null;
  const id = `user-${trimmed.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || Date.now().toString(36)}`;
  const current = getSessionState().displayedSessionId || "";
  let currentLayout = store.getState().layout;
  for (const id of allPaneIds(currentLayout)) if (paneOwner(id) && paneOwner(id) !== current) currentLayout = removePane(currentLayout, id) || currentLayout;
  const layout = normalize(mapPresetLayout(currentLayout, ""));
  if (!layout) return null;
  const hidden = presetHidden(Object.fromEntries(Object.entries(store.getState().hidden).filter(([id]) =>
    (!paneOwner(id) || paneOwner(id) === current) && (!id.startsWith("right:") || id === `right:${current}`))), "");
  let windows: NativeWindowPlacement[];
  try { windows = await nativeWindowPlacements(); } catch { return null; }
  const presets = userPresets();
  presets[id] = {name: trimmed, layout, hidden, windows};
  try { window.localStorage?.setItem(PRESETS_KEY, JSON.stringify(presets)); } catch { return null; }
  return id;
}

export function deleteWorkbenchPreset(id: string): void {
  const presets = userPresets();
  if (!presets[id]) return;
  delete presets[id];
  try { window.localStorage?.setItem(PRESETS_KEY, JSON.stringify(presets)); } catch { /* optional */ }
}

function readLayout(): LayoutNode {
  try {
    const value: unknown = JSON.parse(window.localStorage?.getItem(LAYOUT_KEY) || "null");
    if (isLayoutNode(value) && allPaneIds(value).includes(PANE.workspace)) return value;
  } catch {
    // Fall back to the tested default when persisted layout is unavailable.
  }
  return defaultLayout();
}

function readHidden(): Record<string, boolean> {
  try {
    const value: unknown = JSON.parse(window.localStorage?.getItem(HIDDEN_KEY) || "null");
    if (value && typeof value === "object" && !Array.isArray(value)) {
      return Object.fromEntries(Object.entries(value as Record<string, unknown>).filter(([key]) => key !== PANE.workspace).map(([key, hidden]) => [key, !!hidden]));
    }
  } catch {
    // Storage is optional.
  }
  return {review: true,files:true,terminal:true};
}

function readClosed(): string[] {
  try {
    const value: unknown = JSON.parse(window.localStorage?.getItem(CLOSED_KEY) || "[]");
    return Array.isArray(value) ? value.filter(item => typeof item === "string").slice(-20) : [];
  } catch {
    return [];
  }
}

const store = createExternalStore<WorkbenchState>({
  floating: {},
  layout: readLayout(),
  hidden: readHidden(),
  activeGroupId: "group-workspace",
  hoveredGroupId: "",
  editMode: false,
  lastClosed: readClosed(),
  compact: window.innerWidth <= DECK_BREAKPOINT.narrow,
  overlayPaneId: null,
});

function persist(state: WorkbenchState): void {
  if(detachedChatId())return;
  try {
    window.localStorage?.setItem(LAYOUT_KEY, JSON.stringify(state.layout));
    window.localStorage?.setItem(HIDDEN_KEY, JSON.stringify(state.hidden));
    window.localStorage?.setItem(CLOSED_KEY, JSON.stringify(state.lastClosed));
  } catch {
    // Workbench remains usable when storage is unavailable.
  }
}

function replace(patch: Partial<WorkbenchState>): void {
  const next = {...store.getState(), ...patch};
  store.replaceState(next);
  persist(next);
}

export function getWorkbenchState(): WorkbenchState {
  return store.getState();
}

export function useWorkbenchState(): WorkbenchState {
  return store.useStore();
}

export function isWorkbenchPaneDetached(paneId: string, state = store.getState()): boolean {
  paneId=chatPaneId(paneId);
  const groupValue = findGroupOfPane(state.layout, paneId);
  return !!groupValue && hasNativeWindow(nativePaneKey(groupValue.id));
}

export function detachWorkbenchPane(paneId: string): void {
  if(paneId.startsWith("chatview:")) {
    const id=paneId.slice(9),title=getSessionState().items.find(s=>s.id===id)?.title || "Chat";
    void window.variant1Deck?.openChatWindow?.(id,title);return;
  }
  paneId=chatPaneId(paneId);
  const state = store.getState();
  let groupValue = findGroupOfPane(state.layout, paneId);
  if ((state.compact && !window.variant1Deck?.supportsNativeWindows) || !groupValue || paneId === PANE.workspace || paneId === PANE.history) return;
  const element = document.querySelector<HTMLElement>(`[data-group-id="${CSS.escape(groupValue.id)}"]`);
  const rect = element?.getBoundingClientRect();
  const host = document.querySelector<HTMLElement>(".workbench")?.getBoundingClientRect();
  if (!rect || !host) return;
  // A browser stacked with Chat still detaches through the shared native host.
  // Split that tab into its own group before opening the host; Chat stays docked.
  if (groupValue.panes.includes(PANE.workspace) || groupValue.panes.includes(PANE.history) || groupValue.panes.some(id=>id.startsWith("chatview:"))) {
    if (!window.variant1Deck?.supportsNativeWindows) return;
    const layout = movePane(state.layout, paneId, {groupId: groupValue.id, position: "right"});
    groupValue = findGroupOfPane(layout, paneId);
    if (!groupValue || groupValue.panes.includes(PANE.workspace)) return;
    replace({layout, hidden: {...state.hidden, [paneId]: false}});
  }
  if (window.variant1Deck?.supportsNativeWindows) {
    closeCompactPane();
    if (groupValue.minimized) replace({layout: setGroupMinimized(store.getState().layout, groupValue.id, false)});
    const title = paneId === PANE.history ? "Chats" : element?.querySelector(".workbench-tab.is-active span")?.textContent
      || ((paneId.startsWith("owned:") ? paneId.split(":")[1] : paneId).replace(/^./, letter => letter.toUpperCase()));
    const browserSizes = getPreviewState().tabs.filter(tab => tab.target.kind === "url" && groupValue!.panes.includes(`preview:${tab.id}`))
      .map(tab => browserWindowSize(tab.viewport || DEFAULT_BROWSER_VIEWPORT));
    openNativeWindow(nativePaneKey(groupValue.id), title, {
      width: Math.max(340, rect.width, ...browserSizes.map(size => size.width)),
      height: Math.max(360, rect.height, ...browserSizes.map(size => size.height)), left: rect.left + 24, top: rect.top + 24,
    });
    return;
  }
  const width = Math.min(Math.max(rect.width, 280), Math.max(200, host.width - 24));
  const height = Math.min(Math.max(rect.height, 300), Math.max(160, host.height - 24));
  replace({layout: groupValue.minimized ? setGroupMinimized(state.layout, groupValue.id, false) : state.layout, floating: {...state.floating, [groupValue.id]: {
    width, height,
    left: Math.max(host.left + 12, Math.min(rect.left + 12, host.right - width - 12)),
    top: Math.max(host.top + 12, Math.min(rect.top + 12, host.bottom - height - 12)),
  }}});
}

export function dockWorkbenchGroup(groupId: string): void {
  closeNativeWindow(nativePaneKey(groupId), true);
  const floating = {...store.getState().floating};
  delete floating[groupId];
  store.setState({floating});
}
export function canCloseWorkbenchGroup(groupId:string):boolean {
  const groupValue=findGroupOfPane(store.getState().layout,findGroupActivePane(store.getState().layout,groupId));
  return canClosePreviewTabs((groupValue?.panes || []).filter(id=>id.startsWith("preview:")).map(id=>id.slice(8)));
}
export function closeWorkbenchGroup(groupId:string,confirmed=false):void {
  if(!confirmed && !canCloseWorkbenchGroup(groupId))return;
  const state=store.getState(),groupValue=findGroupOfPane(state.layout,findGroupActivePane(state.layout,groupId));
  if(!groupValue)return;
  const panes=groupValue.panes.filter(id=>id!==PANE.workspace && id!==PANE.history);
  closePreviewTabs(panes.filter(id=>id.startsWith("preview:")).map(id=>id.slice(8)),true);
  const hidden={...store.getState().hidden};for(const id of panes)hidden[id]=true;
  let layout=store.getState().layout;for(const id of panes)if(id.startsWith("preview:") || id.startsWith("chatview:")){
    layout=removePane(layout,id) || layout;
    if(id.startsWith("chatview:"))unregisterChatView(id.slice(9));
  }
  replace({layout,hidden,lastClosed:[...state.lastClosed.filter(id=>!panes.includes(id)),...panes].slice(-20),overlayPaneId:panes.includes(state.overlayPaneId || "") ? null : state.overlayPaneId});
}

export function positionFloatingGroup(groupId: string, left: number, top: number): void {
  const state = store.getState();
  const value = state.floating[groupId];
  if (value) store.setState({floating: {...state.floating, [groupId]: {...value, left, top}}});
}

export function isWorkbenchPaneVisible(
  paneId: string,
  state: WorkbenchState = store.getState(),
): boolean {
  paneId=chatPaneId(paneId);
  const groupValue = findGroupOfPane(state.layout, paneId);
  if (groupValue && hasNativeWindow(nativePaneKey(groupValue.id))) return !state.hidden[paneId] && groupValue.active === paneId;
  if (state.compact && isSidePane(paneId)) return state.overlayPaneId === paneId && !state.hidden[paneId];
  if (!groupValue || state.hidden[paneId] || groupValue.minimized) return false;
  const visible = groupValue.panes.filter(id => !state.hidden[id] && !(state.compact && isSidePane(id)));
  return (visible.includes(groupValue.active) ? groupValue.active : visible[0]) === paneId;
}

export function noteActiveGroup(groupId: string): void {
  if (store.getState().activeGroupId !== groupId) replace({activeGroupId: groupId});
}

export function noteHoveredGroup(groupId: string): void {
  if (store.getState().hoveredGroupId !== groupId) replace({hoveredGroupId: groupId});
}

export function selectPane(groupId: string, paneId: string): void {
  const state = store.getState();
  replace({
    layout: setActivePane(state.layout, groupId, paneId),
    hidden: {...state.hidden, [paneId]: false},
    activeGroupId: groupId,
    ...(state.compact && isSidePane(paneId) ? {overlayPaneId: paneId} : {}),
  });
}

function insertionAnchor(layout: LayoutNode, placement: "left" | "right" | "bottom" | "main"): {
  groupId: string;
  position: DropPosition;
} {
  const workspace = findGroupOfPane(layout, PANE.workspace);
  if (placement === "left") return {groupId: workspace?.id || "group-workspace", position: "left"};
  if (placement === "bottom") return {groupId: workspace?.id || "group-workspace", position: "bottom"};
  if (placement === "main") return {groupId: workspace?.id || "group-workspace", position: "center"};
  const files = findGroupOfPane(layout, chatPaneId(PANE.files));
  return {groupId: files?.id || workspace?.id || "group-workspace", position: "right"};
}

export function revealPane(
  paneId: string,
  placement: "left" | "right" | "bottom" | "main" = "right",
  stackWith?: string,
): void {
  paneId=chatPaneId(paneId);
  const state = store.getState();
  let layout = state.layout;
  let target = findGroupOfPane(layout, paneId);
  if (!target) {
    const stack = stackWith ? findGroupOfPane(layout, stackWith) : null;
    const anchor = stack
      ? {groupId: stack.id, position: "center" as const}
      : insertionAnchor(layout, placement);
    layout = insertAtGroup(layout, anchor.groupId, paneId, anchor.position) || layout;
    target = findGroupOfPane(layout, paneId);
  }
  if (target) layout = setActivePane(layout, target.id, paneId);
  if (target) focusNativeWindow(nativePaneKey(target.id));
  replace({layout, hidden: {...state.hidden, [paneId]: false, [`right:${paneOwner(paneId) || getPreviewState().tabs.find(t=>`preview:${t.id}`===paneId)?.ownerChatId || getSessionState().displayedSessionId || ""}`]:false}, activeGroupId: target?.id || state.activeGroupId,
    ...(state.compact && isSidePane(paneId) ? {overlayPaneId: paneId} : {})});
}

/** UI-created and host-created previews must use the same insertion policy. */
export function revealPreviewPane(tabId: string): void {
  const tabs = getPreviewState().tabs;
  const tab = tabs.find(item => item.id === tabId);
  if (!tab) return;
  const layout = store.getState().layout;
  const candidates = tabs.filter(item => item.id !== tabId && item.ownerChatId === tab.ownerChatId && findGroupOfPane(layout, `preview:${item.id}`));
  const anchor = candidates.find(item => item.target.kind === tab.target.kind) || candidates[0];
  const files=chatPaneId("files",tab.ownerChatId || "");
  revealPane(`preview:${tabId}`, "right", tab.target.kind === "directory" && findGroupOfPane(layout,files) ? files : anchor ? `preview:${anchor.id}` : undefined);
}

export function setWorkbenchCompact(compact: boolean): void {
  if (store.getState().compact !== compact) store.setState({compact, overlayPaneId: null});
}

export function closeCompactPane(id?: string): void {
  if (!id || store.getState().overlayPaneId === id) store.setState({overlayPaneId: null});
}

export function hidePane(paneId: string): void {
  paneId=chatPaneId(paneId);
  const state = store.getState();
  if (paneId === PANE.workspace) return;
  replace({hidden: {...state.hidden, [paneId]: true}, overlayPaneId: state.overlayPaneId === paneId ? null : state.overlayPaneId});
}

export function togglePane(paneId: string, placement: "left" | "right" | "bottom" | "main" = "right"): void {
  paneId=chatPaneId(paneId);
  const state = store.getState();
  const nativeGroup = findGroupOfPane(state.layout, paneId);
  if (nativeGroup && hasNativeWindow(nativePaneKey(nativeGroup.id))) { revealPane(paneId, placement); return; }
  if (state.compact && isSidePane(paneId) && state.overlayPaneId === paneId) {
    closeCompactPane();
    return;
  }
  if (isWorkbenchPaneVisible(paneId, state)) hidePane(paneId);
  else revealPane(paneId, placement);
}

export function closePane(paneId: string): void {
  paneId=chatPaneId(paneId);
  if (paneId === PANE.workspace) return;
  if (paneId.startsWith("owned:") && ["files","review","terminal"].includes(paneId.split(":")[1])) {hidePane(paneId);return;}
  if (([PANE.workspace, PANE.history, PANE.files, PANE.review] as readonly string[]).includes(paneId)) {
    hidePane(paneId);
    return;
  }
  if (paneId === PANE.terminal) {
    hidePane(paneId);
    return;
  }
  if (paneId.startsWith("preview:") && !closePreview(paneId.slice("preview:".length))) return;
  if (paneId.startsWith("chatview:")) unregisterChatView(paneId.slice(9));
  const state = store.getState();
  const layout = removePane(state.layout, paneId) || state.layout;
  replace({
    layout,
    hidden: {...state.hidden, [paneId]: true},
    lastClosed: [...state.lastClosed.filter(item => item !== paneId), paneId].slice(-20),
  });
}

export function forgetPane(paneId: string): void {
  const state = store.getState();
  const layout = removePane(state.layout, paneId) || state.layout;
  const hidden = {...state.hidden};
  delete hidden[paneId];
  if (paneId.startsWith("chatview:")) unregisterChatView(paneId.slice(9));
  replace({layout, hidden,lastClosed:state.lastClosed.filter(id=>id!==paneId)});
}

export function reopenLastClosed(): string | null {
  const state = store.getState();
  const paneId = state.lastClosed[state.lastClosed.length - 1];
  if (!paneId) {
    const id = reopenPreview();
    if (id) revealPane(`preview:${id}`, "right");
    return id ? `preview:${id}` : null;
  }
  replace({lastClosed: state.lastClosed.slice(0, -1)});
  if (paneId.startsWith("preview:")) reopenPreview(paneId.slice("preview:".length));
  if (paneId.startsWith("chatview:")) {const id=paneId.slice(9);registerChatView(id,getSessionState().items.find(row=>row.id===id)?.title || "Chat");}
  revealPane(paneId, "right");
  return paneId;
}

export function moveWorkbenchPane(
  paneId: string,
  groupId: string,
  position: DropPosition,
  before?: string | null,
): void {
  const state = store.getState();
  const source = findGroupOfPane(state.layout, paneId);
  const target=findGroupById(state.layout,groupId);
  const owner=(id:string)=>paneOwner(id) || getPreviewState().tabs.find(tab=>`preview:${tab.id}`===id)?.ownerChatId || "";
  const sourceOwner=owner(paneId);
  if(position==="center" && sourceOwner && target?.panes.some(id=>owner(id) && owner(id)!==sourceOwner)) {
    notifyToast("Tabs from different chats stay in separate groups.");return;
  }
  if (source) dockWorkbenchGroup(source.id);
  dockWorkbenchGroup(groupId);
  replace({layout: movePane(state.layout, paneId, {groupId, position, before})});
}

export function reorderWorkbenchPane(groupId: string, paneId: string, before: string | null): void {
  const state = store.getState();
  if(findGroupOfPane(state.layout,paneId)?.id!==groupId){moveWorkbenchPane(paneId,groupId,"center",before);return;}
  replace({layout: reorderPane(state.layout, groupId, paneId, before)});
}

let layoutSaveTimer:ReturnType<typeof setTimeout>|null=null;
export function flushWorkbenchLayout():void {if(layoutSaveTimer)clearTimeout(layoutSaveTimer);layoutSaveTimer=null;persist(store.getState());}
export function setWorkbenchSplitWeights(splitId: string, weights: readonly number[], sizes?: Readonly<Record<string, number>>, transient=false): void {
  const state = store.getState();
  if(!transient){replace({layout:updateSplitWeights(state.layout,splitId,weights,sizes)});return;}
  store.replaceState({...state,layout:updateSplitWeights(state.layout,splitId,weights,sizes)});
  if(layoutSaveTimer)clearTimeout(layoutSaveTimer);layoutSaveTimer=setTimeout(flushWorkbenchLayout,150);
}

export function forgetChatWorkbench(chatId:string,previewIds:readonly string[]):void {
  const removed=new Set([...previewIds.map(id=>`preview:${id}`),`chatview:${chatId}`]);
  for(const id of allPaneIds(store.getState().layout))if(paneOwner(id)===chatId)removed.add(id);
  const groups=new Set([...removed].map(id=>findGroupOfPane(store.getState().layout,id)?.id).filter((id):id is string=>!!id));
  for(const id of groups)closeNativeWindow(nativePaneKey(id),true);
  const state=store.getState(),hidden={...state.hidden},floating={...state.floating};let layout=state.layout;
  for(const id of removed){layout=removePane(layout,id)||layout;delete hidden[id];}
  delete hidden[`right:${chatId}`];for(const id of groups)delete floating[id];
  unregisterChatView(chatId);
  replace({layout,hidden,floating,lastClosed:state.lastClosed.filter(id=>!removed.has(id)),overlayPaneId:removed.has(state.overlayPaneId||"")?null:state.overlayPaneId});
}

export function toggleGroupMinimized(groupId: string): void {
  const state = store.getState();
  const groupValue = findGroupOfPane(state.layout, findGroupActivePane(state.layout, groupId));
  replace({layout: setGroupMinimized(state.layout, groupId, !groupValue?.minimized)});
}

function findGroupActivePane(node: LayoutNode, groupId: string): string {
  if (node.type === "group") return node.id === groupId ? node.active : "";
  for (const child of node.children) {
    const value = findGroupActivePane(child, groupId);
    if (value) return value;
  }
  return "";
}

export function setWorkbenchTabStrip(groupId: string, mode?: TabStripMode): void {
  const state = store.getState();
  replace({layout: setGroupTabStrip(state.layout, groupId, mode)});
}

export function setWorkbenchEditMode(editMode: boolean): void {
  replace({editMode});
}

export function resetWorkbenchLayout(): void {
  closeNativePaneWindows();
  const owner=getSessionState().displayedSessionId || "";
  const layout=normalize(mapPresetLayout(defaultLayout(),owner))!;
  replace({
    floating: {},
    layout: withLivePanes(layout,owner),
    // Match Default preset: hide obsolete bare panes, not the mapped tools.
    hidden: {files:true,review:true,terminal:true},
    activeGroupId: "group-workspace",
    hoveredGroupId: "",
    editMode: false,
    overlayPaneId: null,
  });
}


function keyboardPanes(group: NonNullable<ReturnType<typeof findGroupOfPane>>, state: WorkbenchState): string[] {
  if (group.minimized) return [];
  const detached = hasNativeWindow(nativePaneKey(group.id)), current = getSessionState().displayedSessionId || "";
  return group.panes.filter(id => {
    if (state.hidden[id]) return false;
    if (detached) return true;
    const owner = paneOwner(id) || getPreviewState().tabs.find(tab => `preview:${tab.id}` === id)?.ownerChatId || "";
    if (["files","review","terminal"].includes(id)) return !current;
    if (id.startsWith("preview:") && !owner && current) return false;
    return !owner || (owner === current && !state.hidden[`right:${owner}`]);
  });
}

export function cycleFocusedGroup(direction: 1 | -1): string | null {
  const state = store.getState();
  const groupId = state.hoveredGroupId || state.activeGroupId;
  const groupValue = findGroupById(state.layout, groupId);
  if (!groupValue || groupValue.panes.length < 2) return null;
  const visible = keyboardPanes(groupValue, state);
  if (visible.length < 2) return null;
  const index = Math.max(0, visible.indexOf(groupValue.active));
  const paneId = visible[(index + direction + visible.length) % visible.length];
  selectPane(groupValue.id, paneId);
  return paneId;
}

export function activateFocusedSlot(slot: number): string | null {
  const state = store.getState();
  const groupId = state.hoveredGroupId || state.activeGroupId;
  const groupValue = findGroupById(state.layout, groupId);
  const panes = groupValue ? keyboardPanes(groupValue, state) : [];
  const paneId = panes[slot - 1];
  if (!groupValue || !paneId) return null;
  selectPane(groupValue.id, paneId);
  return paneId;
}

function findGroupById(node: LayoutNode, groupId: string): ReturnType<typeof findGroupOfPane> {
  if (node.type === "group") return node.id === groupId ? node : null;
  for (const child of node.children) {
    const match = findGroupById(child, groupId);
    if (match) return match;
  }
  return null;
}

export function closeFocusedPane(): string | null {
  const state = store.getState();
  const groupValue = findGroupById(state.layout, state.hoveredGroupId || state.activeGroupId);
  if (!groupValue?.active || groupValue.active === PANE.workspace) return null;
  closePane(groupValue.active);
  return groupValue.active;
}

export function __resetWorkbenchForTests(): void {
  if(layoutSaveTimer)clearTimeout(layoutSaveTimer);layoutSaveTimer=null;
  store.replaceState({
    floating: {},
    layout: defaultLayout(),
    hidden: {review: true},
    activeGroupId: "group-workspace",
    hoveredGroupId: "",
    editMode: false,
    lastClosed: [],
    compact: window.innerWidth <= DECK_BREAKPOINT.narrow,
    overlayPaneId: null,
  });
}

export function rightPanelsHidden(chatId=getSessionState().displayedSessionId || ""):boolean {return !!store.getState().hidden[`right:${chatId}`];}
export function toggleRightPanels():void {
  const owner=getSessionState().displayedSessionId || "",state=store.getState();
  const key=`right:${owner}`;
  const hasPane=allPaneIds(state.layout).some(id=>(paneOwner(id)===owner && !!owner || getPreviewState().tabs.some(t=>`preview:${t.id}`===id && t.ownerChatId===owner)) && !state.hidden[id]);
  if(!hasPane && !state.hidden[key]){revealPane(PANE.files,"right");return;}
  replace({hidden:{...state.hidden,[key]:!state.hidden[key]}});
}

export function rightPanelsVisible():boolean {
  const owner=getSessionState().displayedSessionId || "",state=store.getState();
  if(rightPanelsHidden(owner))return false;
  return allPaneIds(state.layout).some(id=>{
    const group=findGroupOfPane(state.layout,id);
    if(!group || hasNativeWindow(nativePaneKey(group.id)) || state.hidden[id])return false;
    return (!!owner && paneOwner(id)===owner) || getPreviewState().tabs.some(t=>`preview:${t.id}`===id && t.ownerChatId===owner);
  });
}

export function revealChatPane(paneId:string,placement:"right"|"bottom"|"center"="right"):void {
  const state=store.getState();let layout=state.layout;
  let target=findGroupOfPane(layout,paneId);
  if(!target) {const anchor=findGroupOfPane(layout,PANE.workspace);if(!anchor)return;layout=insertAtGroup(layout,anchor.id,paneId,placement) || layout;target=findGroupOfPane(layout,paneId);}
  if(target)layout=setActivePane(layout,target.id,paneId);
  replace({layout,hidden:{...state.hidden,[paneId]:false},activeGroupId:target?.id || state.activeGroupId});
}
