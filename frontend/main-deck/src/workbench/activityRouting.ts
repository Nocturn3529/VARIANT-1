/** Route explicit work/evidence into the unified workbench without fabricating
 * terminal output or mirroring capability internals into a second UI layer. */
import type {ChatTurnStep} from "../chat/types";
import {openBrowser, openFilePreview} from "./previewStore";
import {PANE, revealPane} from "./workbenchStore";

export function paneForTool(tool: string): "review" | "browser" | "terminal" | null {
  const name = String(tool || "").trim();
  if (name === "apply_patch") return "review";
  if (name.startsWith("browser_")) return "browser";
  if (name === "run_command") return "terminal";
  return null;
}

/** Follow only capabilities with a truthful visible surface. */
export function syncPaneFromActivity(input: {
  event: string;
  tool: string;
  status?: string;
  text?: string;
  argsPreview?: string;
}): void {
  const pane = paneForTool(input.tool);
  const event = String(input.event || "");
  const active = event === "tool:start" || event === "tool:activity" || event === "tool:result";
  if (!pane || !active) return;
  if (pane === "review") revealPane(PANE.review, "right");
  else if (pane === "browser") openBrowser();
  // A one-shot run_command does not own a PTY. Do not invent its bytes in the
  // Terminal; the pane opens from an actual terminal/process handle instead.
}

export function canRevealPaneForStep(step: ChatTurnStep): boolean {
  return !!step.evidence?.some(item => (item.kind === "file" || item.kind === "url") && item.value)
    || paneForTool(step.tool || "") !== null;
}

export function revealPaneForStep(step: ChatTurnStep): void {
  const evidence = step.evidence || [];
  const file = evidence.find(item => item.kind === "file");
  if (file) {
    openFilePreview(file.value);
    return;
  }
  const url = evidence.find(item => item.kind === "url");
  if (url) {
    openBrowser(url.value);
    return;
  }
  const pane = paneForTool(step.tool || "");
  if (pane === "review") revealPane(PANE.review, "right");
  else if (pane === "browser") openBrowser();
  else if (pane === "terminal") revealPane(PANE.terminal, "bottom");
}
