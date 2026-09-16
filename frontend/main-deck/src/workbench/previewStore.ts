import {destroyRetainedBrowser} from "./retainedBrowserView";
import {detachedChatId} from "../runtime/viewIdentity";
import {getSessionState} from "../state/sessionStore";
import {createExternalStore} from "../state/createModuleStore";
import {notifyToast} from "../state/toastStore";
import {forgetFileDocument, getFileDocument, isFileDocumentDirty} from "./fileDocumentStore";
import {parseBrowserViewport, type ViewportSize} from "./browserViewport";

export type PreviewKind = "url" | "file" | "output" | "directory";

export type PreviewTarget = Readonly<{
  kind: PreviewKind;
  source: string;
  url: string;
  label: string;
  path?: string;
  mediaType?: string;
  content?: string;
  renderMode?: "source" | "preview" | "diff";
}>;

export type PreviewTab = Readonly<{
  id: string;
  ownerChatId?: string;
  target: PreviewTarget;
  dirty?: boolean;
  navigation?: {revision: number; url: string};
  viewport?: ViewportSize;
}>;

export type BrowserPageState = Readonly<{
  title: string;
  url: string;
  canGoBack: boolean;
  canGoForward: boolean;
  loading: boolean;
}>;

type PreviewState = Readonly<{
  tabs: readonly PreviewTab[];
  selectedId: string;
  pages: Readonly<Record<string, BrowserPageState>>;
}>;

const STORAGE_KEY = "variant1.workbench.preview-tabs.v1";

function persistedTabs(): PreviewTab[] {
  try {
    const raw: unknown = JSON.parse(window.localStorage?.getItem(STORAGE_KEY) || "[]");
    if (!Array.isArray(raw)) return [];
    return raw.flatMap(value => {
      if (!value || typeof value !== "object") return [];
      const tab = value as Record<string, unknown>;
      const target = tab.target as Record<string, unknown> | undefined;
      if (!target) return [];
      const kind = target?.kind;
      if ((kind !== "url" && kind !== "file" && kind !== "directory") || typeof tab.id !== "string") return [];
      let viewport: ViewportSize | undefined;
      try { if (kind === "url" && tab.viewport) viewport = parseBrowserViewport(tab.viewport as Record<string, unknown>) || undefined; } catch { /* Reset malformed saved sizes. */ }
      return [{
        id: tab.id,
        ownerChatId: String(tab.ownerChatId || ""),
        viewport,
        target: {
          kind,
          source: String(target.source || target.url || ""),
          url: String(target.url || ""),
          label: String(target.label || (kind === "url" ? "Browser" : "Preview")),
          path: typeof target.path === "string" ? target.path : undefined,
          mediaType: typeof target.mediaType === "string" ? target.mediaType : undefined,
          renderMode: target.renderMode === "preview" || target.renderMode === "diff" ? target.renderMode : "source",
        },
      }];
    });
  } catch {
    return [];
  }
}

const restored = persistedTabs();
const store = createExternalStore<PreviewState>({
  tabs: restored,
  selectedId: restored[0]?.id || "",
  pages: {},
});

function persist(tabs: readonly PreviewTab[]): void {
  if(detachedChatId())return;
  try {
    window.localStorage?.setItem(STORAGE_KEY, JSON.stringify(
      tabs.filter(tab => tab.target.kind === "url" || tab.target.kind === "file" || tab.target.kind === "directory")
        .map(tab => ({...tab, dirty: false, target: {...tab.target, content: undefined}})),
    ));
  } catch {
    // Persistence is optional.
  }
}

function patch(value: Partial<PreviewState>): void {
  const next = {...store.getState(), ...value};
  store.replaceState(next);
  if (value.tabs) persist(next.tabs);
}

function uniqueId(prefix: string): string {
  const id = globalThis.crypto?.randomUUID?.() || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  return `${prefix}:${id}`;
}

function fileTabId(target: PreviewTarget): string {
  return `${target.kind}:${target.path || target.source || target.url}`;
}

function selectedBrowserId(tabs: readonly PreviewTab[]): string {
  const selected = tabs.find(tab => tab.id === store.getState().selectedId && tab.target.kind === "url");
  return selected?.id || [...tabs].reverse().find(tab => tab.target.kind === "url")?.id || uniqueId("url");
}

export function usePreviewState(): PreviewState {
  return store.useStore();
}

export function getPreviewState(): PreviewState {
  return store.getState();
}

export function openPreview(target: PreviewTarget, options: {newBrowser?: boolean; ownerChatId?: string} = {}): string {
  const state = store.getState();
  const ownerChatId = options.ownerChatId ?? getSessionState().displayedSessionId ?? "";
  const owned = state.tabs.filter(tab => (tab.ownerChatId || "") === ownerChatId);
  const id = target.kind === "url"
    ? (options.newBrowser ? uniqueId("url") : selectedBrowserId(owned))
    : `${ownerChatId}:${fileTabId(target)}`;
  const existing = state.tabs.findIndex(tab => tab.id === id);
  const navigation = target.kind === "url" ? {revision: (state.tabs[existing]?.navigation?.revision || 0) + 1, url: target.url} : undefined;
  const next: PreviewTab = {id, ownerChatId, target, navigation};
  patch({
    tabs: existing < 0
      ? [...state.tabs, next]
      : state.tabs.map((tab, index) => index === existing ? {...tab, target, navigation} : tab),
    selectedId: id,
  });
  return id;
}

export function allowedBrowserUrl(value: unknown): string {
  const raw = String(value || "").trim();
  if (!raw || raw.toLowerCase() === "about:blank") return "about:blank";
  if (/^(localhost|127\.0\.0\.1)(:\d+)?(?:\/|$)/i.test(raw)) return `http://${raw}`;
  const withScheme = /^[a-z][a-z0-9+.-]*:/i.test(raw)
    ? raw
    : (raw.includes(".") && !raw.includes(" ") ? `https://${raw}` : "");
  if (withScheme) {
    try {
      const parsed = new URL(withScheme);
      if ((parsed.protocol === "http:" || parsed.protocol === "https:")
        && !parsed.username && !parsed.password) {
        return parsed.toString();
      }
    } catch { /* fall through to search */ }
    return "about:blank";
  }
  return `https://www.google.com/search?q=${encodeURIComponent(raw)}`;
}

export function openBrowser(url = "about:blank", options: {newTab?: boolean; ownerChatId?: string} = {}): string {
  const safe = allowedBrowserUrl(url);
  return openPreview({kind: "url", source: safe, url: safe, label: "Browser"}, {newBrowser: options.newTab, ownerChatId: options.ownerChatId});
}

export function requestBrowserNavigation(id: string, url: string): void {
  const safe = allowedBrowserUrl(url);
  const state = store.getState();
  patch({tabs: state.tabs.map(tab => tab.id === id && tab.target.kind === "url" ? {
    ...tab, target: {...tab.target, url: safe, source: safe}, navigation: {revision: (tab.navigation?.revision || 0) + 1, url: safe},
  } : tab)});
}

export function setBrowserViewport(id: string, viewport: ViewportSize | null): void {
  const tabs = store.getState().tabs;
  const tab = tabs.find(item => item.id === id && item.target.kind === "url");
  if (!tab || (tab.viewport?.width === viewport?.width && tab.viewport?.height === viewport?.height)) return;
  patch({tabs: tabs.map(item => item.id === id ? {...item, viewport: viewport || undefined} : item)});
}

export function adoptBrowserTab(id: string, url: string, ownerChatId = getSessionState().displayedSessionId || ""): void {
  const state = store.getState();
  const safe = allowedBrowserUrl(url);
  const target: PreviewTarget = {kind: "url", source: safe, url: safe, label: "Browser"};
  const index = state.tabs.findIndex(tab => tab.id === id);
  patch({
    tabs: index < 0 ? [...state.tabs, {id, target, ownerChatId}] : state.tabs.map((tab, at) => at === index ? {...tab, target} : tab),
    selectedId: id,
  });
}

export function openFilePreview(path: string, label?: string, ownerChatId?:string): string {
  const normalized = String(path || "").trim();
  return openPreview({
    kind: "file",
    source: normalized,
    url: normalized,
    path: normalized,
    label: label || normalized.split(/[\\/]/).pop() || "Preview",
    renderMode: "source",
  },{ownerChatId});
}

export function openOutputPreview(target: Omit<PreviewTarget, "kind">): string {
  return openPreview({...target, kind: "output"});
}

export function selectPreview(id: string): void {
  if (store.getState().tabs.some(tab => tab.id === id)) patch({selectedId: id});
}

const closedTabs: PreviewTab[] = [];

export function canClosePreviewTabs(ids:readonly string[]):boolean {
  const state = store.getState();
  const closing = state.tabs.filter(tab => ids.includes(tab.id));
  if (closing.some(tab => getFileDocument(tab.id)?.saving)) {
    notifyToast("Wait for the file to finish saving");
    return false;
  }
  const dirty = closing.filter(tab => getFileDocument(tab.id) ? isFileDocumentDirty(tab.id) : tab.dirty);
  if (dirty.length && !window.confirm(`Discard unsaved changes in ${dirty.map(tab => tab.target.label).join(", ")}?`)) return false;
  return true;
}
function closeTabs(ids: readonly string[],confirmed=false): boolean {
  if(!confirmed && !canClosePreviewTabs(ids))return false;
  const state=store.getState(),closing=state.tabs.filter(tab=>ids.includes(tab.id));
  const index = Math.max(0, state.tabs.findIndex(tab => tab.id === state.selectedId));
  const tabs = state.tabs.filter(tab => !ids.includes(tab.id));
  const pages = {...state.pages};
  for (const tab of closing) {
    if(tab.target.kind==="url")destroyRetainedBrowser(tab.id);
    forgetFileDocument(tab.id);
    delete pages[tab.id];
    closedTabs.push({...tab, dirty: false});
  }
  if (closedTabs.length > 20) closedTabs.splice(0, closedTabs.length - 20);
  patch({
    tabs, pages,
    selectedId: tabs.some(tab => tab.id === state.selectedId) ? state.selectedId : tabs[Math.min(index, tabs.length - 1)]?.id || "",
  });
  return true;
}

export function closePreview(id: string): boolean {
  return closeTabs([id]);
}
export function closePreviewTabs(ids:readonly string[],confirmed=false):boolean{return closeTabs(ids,confirmed);}
/** Successful chat deletion retires both open tabs and their reopen history. */
export function forgetChatPreviews(chatId:string):string[] {
  const ids=[...store.getState().tabs,...closedTabs].filter(tab=>tab.ownerChatId===chatId).map(tab=>tab.id);
  closeTabs(ids,true);
  for(let i=closedTabs.length-1;i>=0;i--)if(closedTabs[i].ownerChatId===chatId)closedTabs.splice(i,1);
  return [...new Set(ids)];
}

export function reopenPreview(id?: string): string | null {
  const index = id ? closedTabs.map(tab => tab.id).lastIndexOf(id) : closedTabs.length - 1;
  if (index < 0) return null;
  const [tab] = closedTabs.splice(index, 1);
  const state = store.getState();
  patch({tabs: state.tabs.some(item => item.id === tab.id) ? state.tabs : [...state.tabs, tab], selectedId: tab.id});
  return tab.id;
}

export function closeOtherPreviews(id: string): boolean {
  const state = store.getState();
  if (!state.tabs.some(tab => tab.id === id)) return false;
  return closeTabs(state.tabs.filter(tab => tab.id !== id && tab.ownerChatId === state.tabs.find(t=>t.id===id)?.ownerChatId).map(tab => tab.id));
}

export function closePreviewsToRight(id: string): boolean {
  const state = store.getState();
  const index = state.tabs.findIndex(tab => tab.id === id);
  return index >= 0 && closeTabs(state.tabs.slice(index + 1).filter(tab=>tab.ownerChatId===state.tabs[index].ownerChatId).map(tab => tab.id));
}

export function setPreviewDirty(id: string, dirty: boolean): void {
  const state = store.getState();
  if (!state.tabs.some(tab => tab.id === id && !!tab.dirty !== dirty)) return;
  patch({tabs: state.tabs.map(tab => tab.id === id ? {...tab, dirty} : tab)});
}

export function noteBrowserPage(id: string, page: BrowserPageState): void {
  const state = store.getState();
  const previous=state.pages[id];
  if(previous && previous.url===page.url && previous.title===page.title && previous.loading===page.loading && previous.canGoBack===page.canGoBack && previous.canGoForward===page.canGoForward)return;
  patch({pages: {...state.pages, [id]: page}});
}

export function openDirectoryPreview(path: string, ownerChatId = getSessionState().displayedSessionId || ""): string {
  return openPreview({kind:"directory", source:path, url:path, path, label:path.split(/[\\/]/).pop() || path},{ownerChatId});
}
