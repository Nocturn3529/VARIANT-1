import {createExternalStore} from "../state/createModuleStore";
import {notifyToast} from "../state/toastStore";
import type {RuntimeApi} from "../types";
import {unstable_batchedUpdates} from "react-dom";

export type NativeWindowRecord = {
  window: Window;
  document: Document | null;
  title: string;
  phase: "opening" | "ready";
  pinned?: boolean;
};
export type NativeWindowPlacement = {id: string; title: string; left: number; top: number; width: number; height: number; pinned: boolean};
type SurfaceRegistration = {restore: () => void; closed: (dock: boolean) => void;canClose?:()=>boolean};
const store = createExternalStore<Readonly<Record<string, NativeWindowRecord>>>({});
const surfaces = new Map<string, SurfaceRegistration>();
const pending = new Map<string, {timer: ReturnType<typeof setInterval>; fail: () => void}>();
const closing = new Set<string>();

export const nativePaneKey = (groupId: string) => `pane:${groupId}`;
export const nativeUtilityKey = (kind: string) => `utility:${kind}`;
export const useNativeWindows = store.useStore;
export function hasNativeWindow(id: string): boolean { return !!store.getState()[id]; }
export function updateNativeWindow(id: string, value: {title?: string; pinned?: boolean}): void {
  const record = store.getState()[id];
  if (!record || Object.entries(value).every(([key, next]) => record[key as keyof NativeWindowRecord] === next)) return;
  store.setState({[id]: {...record, ...value}});
}
export async function nativeWindowPlacements(): Promise<NativeWindowPlacement[]> {
  const placements = await Promise.all(Object.entries(store.getState()).map(async ([id, value]) => {
    if (value.window.closed) return [];
    // BrowserWindow bounds include Windows' invisible resize frame. DOM
    // outerWidth/screenX use different edges and drift on each restoration.
    const result = await window.variant1Deck?.controlNativeWindow?.(id, "bounds");
    if (!result?.ok || !result.bounds) throw new Error("Could not read native window placement");
    const {x, y, width, height} = result.bounds;
    return [{id, title: value.title, left: x, top: y, width, height, pinned: !!result.pinned}];
  }));
  return placements.flat();
}
export function parseNativePlacements(value: unknown): NativeWindowPlacement[] {
  if (!Array.isArray(value)) return [];
  const seen = new Set<string>();
  return value.slice(0, 32).flatMap(row => {
    if (!row || typeof row !== "object" || typeof row.id !== "string" || seen.has(row.id)
      || !/^(pane:[\w.-]+|utility:(runtime|overview|automations))$/.test(row.id)
      || ![row.left,row.top,row.width,row.height].every(number => typeof number === "number" && Number.isFinite(number))
      || row.width < 300 || row.height < 240 || row.width > 20000 || row.height > 20000) return [];
    seen.add(row.id);
    return [{id: row.id, title: String(row.title || "Panel").slice(0, 160), left: row.left, top: row.top,
      width: row.width, height: row.height, pinned: row.pinned === true}];
  });
}
export function dockAllNativeWindows(): void {
  for (const id of Object.keys(store.getState())) closeNativeWindow(id, id.startsWith("pane:"));
  void window.variant1Deck?.controlNativeWindow?.("deck", "focus");
}

export function registerNativeSurface(id: string, registration: SurfaceRegistration): () => void {
  surfaces.set(id, registration);
  return () => {
    if (surfaces.get(id) !== registration) return;
    if (store.getState()[id]?.phase !== "opening") closeNativeWindow(id,true);
    surfaces.delete(id);
  };
}

export function installNativeWindowBridge(api: RuntimeApi | null): () => void {
  return api?.onNativeWindowClosed?.(id => closeNativeWindow(id)) || (() => {});
}

export function focusNativeWindow(id: string): boolean {
  if (closing.has(id)) return false;
  const value = store.getState()[id];
  if (!value) return false;
  if (value.phase === "opening") return true;
  if (value.window.closed) { closeNativeWindow(id); return false; }
  void window.variant1Deck?.controlNativeWindow?.(id, "focus");
  value.window.focus();
  return true;
}

/** Restore the original mount synchronously before the child document goes away. */
export function closeNativeWindow(id: string, dock = false): boolean {
  if(closing.has(id))return true;
  const value = store.getState()[id];
  if (!value) return true;
  const registration = surfaces.get(id);
  if(!dock && registration?.canClose && !registration.canClose()){
    if(!value.window.closed)return false;
    dock=true;
  }
  const wait = pending.get(id);
  if (wait) { clearInterval(wait.timer); pending.delete(id); }
  registration?.restore();
  const next = {...store.getState()};
  delete next[id];
  closing.add(id);
  unstable_batchedUpdates(() => {
    registration?.closed(dock);
    store.replaceState(next);
  });
  closing.delete(id);
  if (!value.window.closed) value.window.close();
  if (dock) {
    void window.variant1Deck?.controlNativeWindow?.("deck", "focus");
    window.focus();
  }
  return true;
}

export function closeNativePaneWindows(): void {
  for (const id of Object.keys(store.getState())) if (id.startsWith("pane:")) closeNativeWindow(id, true);
}

export function openNativeWindow(id: string, title: string, bounds?: {width?: number; height?: number; left?: number; top?: number; screen?: boolean; pinned?: boolean}): boolean {
  if (focusNativeWindow(id)) return true;
  if (!window.variant1Deck?.supportsNativeWindows) return false;
  const url = new URL("./popout.html", window.location.href);
  url.searchParams.set("surface", id);
  const left = (bounds?.screen ? 0 : window.screenX) + (bounds?.left ?? 60);
  const top = (bounds?.screen ? 0 : window.screenY) + (bounds?.top ?? 60);
  const features = `width=${Math.round(bounds?.width || 640)},height=${Math.round(bounds?.height || 600)},left=${Math.round(left)},top=${Math.round(top)},pin=${!!bounds?.pinned}`;
  const child = window.open(url.href, `variant1-panel:${id}`, features);
  if (!child) { notifyToast("Could not open the panel window."); return false; }
  store.setState({[id]: {window: child, document: null, title, phase: "opening", pinned: !!bounds?.pinned}});
  const deadline = Date.now() + 10_000;
  const fail = (reason = "window closed or load timed out") => {
    window.variant1Deck?.log?.(`[native-window] ${id}: ${reason}`);
    closeNativeWindow(id, true); notifyToast("The panel window could not load. The panel is still in the workspace.");
  };
  const timer = setInterval(() => {
    if (child.closed || Date.now() > deadline) { fail(); return; }
    try {
      if (child.location.href !== url.href || child.document.readyState !== "complete"
        || !child.document.getElementById("native-popout-root")) return;
      clearInterval(timer); pending.delete(id);
      child.document.title = `${title} — VARIANT-1`;
      child.addEventListener("beforeunload", event => {if(!closeNativeWindow(id)){event.preventDefault();event.returnValue="";}});
      store.setState({[id]: {window: child, document: child.document, title, phase: "ready", pinned: !!bounds?.pinned}});
    } catch (error) { fail(String(error)); }
  }, 40);
  pending.set(id, {timer, fail});
  return true;
}
