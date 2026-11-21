/**
 * Semantic activity grouping adapted from Hermes Agent's run-summary model.
 * Hermes Agent is MIT licensed, Copyright (c) 2025 Nous Research.
 */
import type {ChatTurnStep} from "./types";
import type {activityPresentation} from "./activityPresentation";

// A fallback for direct harness tools outside the more specific trace labels.
const verbs: ReadonlyArray<readonly [RegExp, string, string]> = [
  [/(?:^|\.)(?:apply_patch|exact_replace|write_file|create_file|edit_file|replace_file)$/i, "Edited", "Editing"],
  [/(?:^|\.)(?:read_file|glob|grep|search|web_search|browser_read|browser_screenshot|computer_observe|observe|inspect)$|^(?:browser|computer)_/i, "Explored", "Exploring"],
  [/(?:^|\.)(?:run_command|terminal|execute|execute_code|ipython)$/i, "Ran", "Running"],
  [/(?:^|\.)(?:children|delegate|delegate_task|spawn_agent)$/i, "Delegated", "Delegating"],
];

function compact(value: unknown, limit = 120): string {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit - 1).trimEnd()}…` : text;
}

function filename(value: string) {
  return value.split(/[\\/]/).filter(Boolean).pop() || value;
}

export function stepFailed(step: ChatTurnStep): boolean {
  return step.status === "error" || step.status === "timed_out";
}

export function activityStatusLabel(status: ChatTurnStep["status"]): string {
  return ({running:"Running",ok:"Complete",done:"Complete",error:"Error",cancelled:"Stopped",
    interrupted:"Interrupted",timed_out:"Timed out",skipped:"Skipped",degraded:"Degraded",unknown:"Unknown outcome"})[status || "unknown"];
}

export function currentStepLabel(step: ChatTurnStep, live: boolean, {args, filePath}: ReturnType<typeof activityPresentation>): string {
  if (step.kind !== "tool") return compact(step.label, 120);
  const tool = step.tool?.trim() || "";
  const match = verbs.find(([pattern]) => pattern.test(tool));
  const verb = live && step.status === "running" ? match?.[2] || "Using" : match?.[1] || "Used";
  const evidence = step.evidence?.find(item => item.value || item.label);
  let target: unknown = args?.query || args?.url;
  if (evidence) {
    target = evidence.value || evidence.label;
    if (evidence.kind === "file" || evidence.kind === "folder") target = filename(String(target));
  } else if (filePath) target = filename(filePath);
  else if (!target && match?.[1] === "Ran") {
    const command = args?.command || args?.code || args?.argv;
    target = Array.isArray(command) ? command.join(" ") : command;
  }
  return target ? `${verb} ${compact(target, 90)}` : compact(step.label || tool);
}

export function normalizeActivityStatus(raw: string, event: string): ChatTurnStep["status"] {
  const status = String(raw || "").trim().toLowerCase().replace(/[\s-]+/g, "_");
  if (event === "tool:start" || status === "running" || status === "starting") return "running";
  if (["ok", "success", "succeeded", "complete", "completed", "done", "replayed"].includes(status)) return "ok";
  if ([
    "error", "failed", "failure", "invalid_arguments", "unavailable",
    "needs_reconciliation", "rejected", "blocked", "unknown_effect",
  ].includes(status)) return "error";
  if (["cancelled", "canceled", "cancelled_before_start", "canceled_before_start"].includes(status)) return "cancelled";
  if (["timed_out", "timeout"].includes(status)) return "timed_out";
  if (status === "interrupted") return "interrupted";
  if (status === "skipped" || status === "degraded") return status;
  return status ? "unknown" : event === "tool:result" ? "ok" : "done";
}

export function formatActivityDuration(milliseconds: number | undefined): string {
  const value = Math.max(0, Number(milliseconds) || 0);
  if (!value) return "";
  if (value < 1_000) return `${Math.round(value)}ms`;
  const seconds = value / 1_000;
  if (seconds < 60) return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = Math.round(seconds % 60);
  return `${minutes}m${remainder ? ` ${remainder}s` : ""}`;
}
