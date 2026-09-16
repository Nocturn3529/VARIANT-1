import type {RuntimeApi} from "../types";

type PathChange = Parameters<NonNullable<RuntimeApi["onWorkbenchPathChanged"]>>[0] extends (event: infer E) => void ? E : never;

/** Own the subscription, pending registration, and debounce as one lifetime. */
export function watchPath(api: RuntimeApi | null | undefined, path: string, onChange: () => void,
  options: {scope?: "workspace" | "directory"; delay?: number; matches?: (event: PathChange) => boolean; onError?: (message: string) => void} = {}): () => void {
  let disposed = false;
  let watchId = "";
  let timer: ReturnType<typeof setTimeout> | undefined;
  const early = new Map<string, PathChange>();
  let overflowed = false;
  const report = (error: unknown) => { if (!disposed) options.onError?.(error instanceof Error ? error.message : String(error)); };
  const stop = (id: string) => { void api?.stopWorkbenchWatch?.(id).catch(report); };
  const refresh = () => {
    clearTimeout(timer);
    timer = setTimeout(() => { if (!disposed) onChange(); }, options.delay ?? 100);
  };
  const receive = (event: PathChange) => {
    if (disposed || !event.id) return;
    if (!watchId) {
      if (early.size < 128 || early.has(event.id)) {
        if (early.get(event.id)?.event !== "error") early.set(event.id, event);
      } else overflowed = true;
      return;
    }
    if (event.id !== watchId || (options.matches && !options.matches(event))) return;
    if (event.event === "error") { report(event.error || "File watcher stopped"); return; }
    refresh();
  };
  const unsubscribe = api?.onWorkbenchPathChanged?.(receive);
  void api?.watchWorkbenchPath?.(path, {scope: options.scope || "directory"}).then(result => {
    if (!result?.ok || !result.id) { early.clear(); if (result?.error) report(result.error); return; }
    if (disposed) stop(result.id);
    else {
      watchId = result.id;
      const pending = early.get(watchId);
      if (pending) receive(pending);
      else if (overflowed) refresh();
    }
    early.clear();
  }).catch(report);
  return () => {
    if (disposed) return;
    disposed = true;
    early.clear();
    clearTimeout(timer);
    unsubscribe?.();
    if (watchId) { stop(watchId); watchId = ""; }
  };
}
