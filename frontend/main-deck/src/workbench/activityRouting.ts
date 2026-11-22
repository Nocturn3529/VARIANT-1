/** Route explicit work/evidence into the unified workbench without fabricating
 * terminal output or mirroring capability internals into a second UI layer. */
import type {ChatTurnStep} from "../chat/types";
import {evidenceFilePath} from "../chat/evidence";
import {notifyToast} from "../state/toastStore";
import {getSessionState} from "../state/sessionStore";
import {openBrowser, openFilePreview} from "./previewStore";
import {PANE, chatPaneId, revealPane} from "./workbenchStore";

export function paneForTool(tool: string): "review" | "browser" | "terminal" | null {
  const name = String(tool || "").trim();
  if (name === "apply_patch") return "review";
  if (name.startsWith("browser_")) return "browser";
  if (name === "run_command") return "terminal";
  return null;
}

/** Follow only capabilities with a truthful visible surface. */
export function syncPaneFromActivity(input: {
  chatId: string;
  event: string;
  tool: string;
  status?: string;
  text?: string;
  argsPreview?: string;
}): void {
  const pane = paneForTool(input.tool);
  const event = String(input.event || "");
  const active = event === "tool:start" || event === "tool:activity" || event === "tool:result";
  if (!pane || !active || input.chatId !== (getSessionState().displayedSessionId || "")) return;
  if (pane === "review") revealPane(chatPaneId(PANE.review, input.chatId), "right");
  else if (pane === "browser") openBrowser("about:blank", {ownerChatId: input.chatId});
  // A one-shot run_command does not own a PTY. Do not invent its bytes in the
  // Terminal; the pane opens from an actual terminal/process handle instead.
}

export function canRevealPaneForStep(step: ChatTurnStep): boolean {
  return !!step.evidence?.some(item => (item.kind === "file" || item.kind === "url") && item.value)
    || paneForTool(step.tool || "") !== null;
}

export async function revealPaneForStep(step: ChatTurnStep, chatId: string): Promise<void> {
  const evidence = step.evidence || [];
  const file = evidence.find(item => item.kind === "file");
  if (file) {
    let path = evidenceFilePath(file.value);
    if (!path) {
      // Legacy evidence and chats without a bound project carry only the relative name.
      if (!window.variant1Deck?.pickFolder) {
        notifyToast("Open this chat in the main Deck to choose the file’s original project folder");
        return;
      }
      notifyToast("Choose the project folder this file came from");
      let root;
      try { root = await window.variant1Deck.pickFolder(); }
      catch { notifyToast("The project folder picker could not open"); return; }
      if (!root) return;
      path = evidenceFilePath(file.value, root);
      if (!path) { notifyToast("This file needs an absolute path"); return; }
    }
    openFilePreview(path, file.label, chatId);
    return;
  }
  const url = evidence.find(item => item.kind === "url");
  if (url) {
    openBrowser(url.value, {ownerChatId: chatId});
    return;
  }
  const pane = paneForTool(step.tool || "");
  if (pane === "review") revealPane(chatPaneId(PANE.review, chatId), "right");
  else if (pane === "browser") openBrowser("about:blank", {ownerChatId: chatId});
  else if (pane === "terminal") revealPane(chatPaneId(PANE.terminal, chatId), "bottom");
}
