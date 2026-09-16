import type {RuntimeApi} from "../types";
import {createExternalStore} from "../state/createModuleStore";
import {asRecord} from "../state/storePrimitives";
import {BrowserCommandError, checkBrowserRequest} from "./browserLifecycle";

export type BrowserDownload = Readonly<{
  download_id: string; tab_id: string; guest_id: number; status: string; suggested_filename: string;
  url: string; url_chain: string[]; bytes: number; total_bytes: number; started_at: number; error: string;
}>;
const store = createExternalStore<readonly BrowserDownload[]>([]);
export const useBrowserDownloads = store.useStore;
export const downloadsForTab = (id: string) => store.getState().filter(row => row.tab_id === id);
function ingest(rows: unknown): void {
  if (!Array.isArray(rows)) return;
  store.replaceState(rows.flatMap(value => {
    const row = asRecord(value);
    if (typeof row.download_id !== "string" || typeof row.tab_id !== "string") return [];
    return [{download_id:row.download_id, tab_id:row.tab_id, guest_id:Number(row.guest_id || 0),
      status:String(row.status || "unknown"), suggested_filename:String(row.suggested_filename || "download"),
      url:String(row.url || ""), url_chain:Array.isArray(row.url_chain) ? row.url_chain.map(String) : [],
      bytes:Number(row.bytes || 0), total_bytes:Number(row.total_bytes || 0), started_at:Number(row.started_at || 0), error:String(row.error || "")}];
  }));
}
export function installBrowserDownloads(api: RuntimeApi | null): () => void {
  let disposed = false;
  const stop = api?.onWorkbenchDownloads?.(rows => { if (!disposed) ingest(rows); });
  void api?.workbenchDownloads?.({action:"list"}).then(result => { if (!disposed && result.ok) ingest(result.downloads); }).catch(() => {});
  return () => { disposed = true; stop?.(); };
}
export const isDownloadCommand = (action: string) => ["downloads", "drain_downloads", "ack_downloads", "cancel_download"].includes(action);
export async function runBrowserDownloadCommand(command: Record<string, unknown>, signal?: AbortSignal): Promise<Record<string, unknown>> {
  checkBrowserRequest(signal);
  const api = window.variant1Deck?.workbenchDownloads;
  if (!api) throw new BrowserCommandError("DOWNLOADS_UNAVAILABLE", "Native download storage is unavailable", "download");
  const result = await api({...command, action: command.action === "downloads" ? "list" : command.action});
  checkBrowserRequest(signal);
  if (!result.ok) throw new BrowserCommandError("DOWNLOAD_FAILED", String(result.error || "Download request failed"), "download");
  return result;
}
export async function findStartedDownload(tabId: string, url: string, since: number, signal?: AbortSignal): Promise<BrowserDownload | undefined> {
  const deadline = Date.now() + 1000;
  while (Date.now() < deadline) {
    checkBrowserRequest(signal);
    const result = await window.variant1Deck?.workbenchDownloads?.({action:"list", tab_id:tabId});
    if (result?.ok && Array.isArray(result.downloads)) {
      const row = result.downloads.map(asRecord).find(row => Number(row.started_at) >= since
        && (row.url === url || Array.isArray(row.url_chain) && row.url_chain.includes(url)));
      if (row) return row as BrowserDownload;
    }
    if (!window.variant1Deck?.workbenchDownloads) return;
    await new Promise(resolve => setTimeout(resolve, 25));
  }
}
