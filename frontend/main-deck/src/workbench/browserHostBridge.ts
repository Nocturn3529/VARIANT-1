import {getSessionState} from "../state/sessionStore";
import type {RuntimeContext} from "../types";
import {browserCommandDiagnostics, isBrowserRecoveryAction, runWorkbenchBrowserCommand, waitForWorkbenchBrowser, workbenchBrowserTargets} from "./browserBridge";
import {findStartedDownload, isDownloadCommand} from "./browserDownloads";
import {BrowserCommandError, browserDeadline, checkBrowserRequest} from "./browserLifecycle";
import {adoptBrowserTab, closePreview, getPreviewState, openBrowser, selectPreview} from "./previewStore";
import {navigateTo} from "../state/appStore";
import {revealPreviewPane,rightPanelsHidden} from "./workbenchStore";
import {browserKeyInput} from "./browserKeyboard";

let context: RuntimeContext | null = null;
let connection = "offline";
let generation = 0;
const pending = new Set<AbortController>();

export function setBrowserHostContext(next: RuntimeContext) { context = next; }

export function setBrowserHostConnection(status: string) {
  if (connection === status) return;
  connection = status; generation++;
  for (const request of pending) request.abort(new BrowserCommandError("HOST_DISCONNECTED", "Browser host connection changed; request cancelled", "connection"));
  pending.clear();
  if (typeof document !== "undefined") document.body.dataset.browserHost = status === "connected" ? "registering" : "offline";
  context?.api?.log?.(`[browser-host] connection=${status} generation=${generation}`);
  if (status === "connected") context?.send({type: "browser:host:register"});
}

async function readySurface(tabId: string, signal: AbortSignal, requireDocument = true): Promise<void> {
  checkBrowserRequest(signal);
  const tab=getPreviewState().tabs.find(t=>t.id===tabId);
  const foreground=(tab?.ownerChatId || "") === (getSessionState().displayedSessionId || "");
  if(foreground && !rightPanelsHidden(tab?.ownerChatId || "")){navigateTo("chat"); selectPreview(tabId); revealPreviewPane(tabId);}
  if (foreground && browserCommandDiagnostics({tab_id: tabId}).visibility === "hidden") {
    await context?.api?.controlNativeWindow?.("deck", "focus");
  }
  if (!await waitForWorkbenchBrowser(tabId, 5000, signal, false, requireDocument)) throw new BrowserCommandError("GUEST_NOT_READY", "Browser tab did not reach the requested readiness; navigate or reload to recover", "ready");
  Array.from(document.querySelectorAll<HTMLElement>("[data-pane-id]")).find(element => element.dataset.paneId === `preview:${tabId}`)
    ?.scrollIntoView?.({block: "nearest", inline: "nearest"});
  checkBrowserRequest(signal);
}

async function ensureCommandSurface(command: Record<string, unknown>, signal: AbortSignal): Promise<string> {
  const requested = String(command.tab_id || command.target_id || "");
  const ownerChatId=String(command.owner_chat_id || "");
  const tabs = getPreviewState().tabs.filter(tab => tab.target.kind === "url" && (tab.ownerChatId || "") === ownerChatId);
  if (requested && !tabs.some(tab => tab.id === requested)) throw new BrowserCommandError("TAB_NOT_FOUND", "Requested browser tab is not open");
  const tabId = requested || tabs.find(tab => tab.id === getPreviewState().selectedId)?.id || tabs.at(-1)?.id
    || openBrowser("about:blank", {newTab: true,ownerChatId});
  await readySurface(tabId, signal, !isBrowserRecoveryAction(String(command.action || "state")));
  return tabId;
}

export function ingestBrowserHost(message: Record<string, unknown>) {
  if (message.type === "browser:host:registered") {
    if (connection === "connected" && typeof document !== "undefined") document.body.dataset.browserHost = "registered";
    return;
  }
  if (message.type !== "browser:host:command" || !context || connection !== "connected") return;
  const owner = context;
  const receivedGeneration = generation;
  const controller = new AbortController();
  const signal = controller.signal;
  pending.add(controller);
  const id = String(message.id || "");
  const command = message.command && typeof message.command === "object" ? {...message.command as Record<string, unknown>} : {};
  const action = String(command.action || "state");
  const ownerChatId=String(command.owner_chat_id || "");
  const targets=()=>workbenchBrowserTargets(ownerChatId);
  const started = Date.now();
  let diagnostics = browserCommandDiagnostics(command);
  const work = async () => {
    checkBrowserRequest(signal);
    const explicit=String(command.tab_id || command.target_id || "");
    const existing=getPreviewState().tabs.find(t=>t.id===explicit);
    if(existing && (existing.ownerChatId || "")!==ownerChatId)throw new BrowserCommandError("TAB_NOT_FOUND","Browser tab belongs to another chat");
    if (action === "keys") browserKeyInput(command.keys);
    if (isDownloadCommand(action)) return runWorkbenchBrowserCommand(command, {signal});
    if (action === "new_page") {
      const created = Date.now();
      const tabId = String(command.tab_id || command.target_id || "") || openBrowser(String(command.url || "about:blank"), {newTab: true,ownerChatId});
      if (!getPreviewState().tabs.some(tab => tab.id === tabId)) adoptBrowserTab(tabId, String(command.url || "about:blank"),ownerChatId);
      command.tab_id = tabId;
      try { await readySurface(tabId, signal); }
      catch (error) {
        const download = await findStartedDownload(tabId, String(command.url || "about:blank"), created, signal);
        if (!download) throw error;
      }
      diagnostics = browserCommandDiagnostics(command);
      return {ok: true, target: targets().find(tab => tab.id === tabId), tabs: targets()};
    }
    if (action === "close_page") {
      const tabId = String(command.tab_id || command.target_id || "");
      if (tabId) closePreview(tabId);
      return {ok: true, tabs: targets()};
    }
    if (action === "activate_page") {
      const tabId = String(command.tab_id || command.target_id || "");
      if (!getPreviewState().tabs.some(tab => tab.id === tabId && tab.target.kind === "url")) throw new BrowserCommandError("TAB_NOT_FOUND", "Browser tab is not open");
      await readySurface(tabId, signal, false);
      diagnostics = browserCommandDiagnostics(command);
      return {ok: true, target: targets().find(tab => tab.id === tabId), tabs: targets()};
    }
    if (action === "tabs") return {ok: true, tabs: targets()};
    command.tab_id = await ensureCommandSurface(command, signal);
    diagnostics = browserCommandDiagnostics(command);
    return runWorkbenchBrowserCommand(command, {signal});
  };
  return browserDeadline(work(), 25000, "request", signal).catch(error => {
    const code = error instanceof BrowserCommandError ? error.code : "BROWSER_COMMAND_FAILED";
    const phase = error instanceof BrowserCommandError ? error.phase : action;
    const detail = error instanceof Error ? error.message : String(error);
    return {ok: false, code, phase, error: `${detail} [${code}; ${phase}; guest=${diagnostics.guest_generation ?? "?"}; document=${diagnostics.document_generation ?? "?"}; host=${receivedGeneration}]`};
  }).then(result => {
    const receipt = {...result, diagnostics: {...diagnostics, host_generation: receivedGeneration, elapsed_ms: Date.now() - started, end: browserCommandDiagnostics(command)}};
    const current = receivedGeneration === generation && connection === "connected" && !signal.aborted;
    // The socket owner may have reconnected while Electron awaited a frame.
    // Never send an old completion through the new transport or replay input.
    if (current) owner.send({type: "browser:host:result", id, result: receipt});
    owner.api?.log?.(`[browser-host] ${JSON.stringify({id, action, ...receipt.diagnostics, ok: result.ok, delivered: current, ...("code" in result ? {code: result.code} : {})})}`);
  }).finally(() => { pending.delete(controller); controller.abort(); });
}
