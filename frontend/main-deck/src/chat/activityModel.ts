/**
 * Semantic activity grouping adapted from Hermes Agent's run-summary model.
 * Hermes Agent is MIT licensed, Copyright (c) 2025 Nous Research.
 */
import type {ChatTurnStep} from "./types";

export type StepCategory = "edit" | "explore" | "run" | "delegate" | "other";

const CATEGORY_COPY: Record<StepCategory, {
  past: string;
  present: string;
}> = {
  edit: {past: "Edited", present: "Editing"},
  explore: {past: "Explored", present: "Exploring"},
  run: {past: "Ran", present: "Running"},
  delegate: {past: "Delegated", present: "Delegating"},
  other: {past: "Used", present: "Using"},
};

const EDIT_RE = /(?:^|\.)(?:apply_patch|exact_replace|write_file|create_file|edit_file|replace_file)$/i;
const EXPLORE_RE = /(?:^|\.)(?:read_file|glob|grep|search|web_search|browser_read|browser_screenshot|computer_observe|observe|inspect)$/i;
const RUN_RE = /(?:^|\.)(?:run_command|terminal|execute|execute_code|ipython)$/i;
const DELEGATE_RE = /(?:^|\.)(?:children|delegate|delegate_task|spawn_agent)$/i;
const INNER_TOOL_RE = /\btools\.([A-Za-z_][\w]*)/;

function basename(value: string): string {
  const clean = value.replace(/[\\/]+$/, "");
  return clean.split(/[\\/]/).filter(Boolean).pop() || clean;
}

function compact(value: unknown, limit = 120): string {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, Math.max(1, limit - 1)).trimEnd()}…` : text;
}

function parsedArgs(step: ChatTurnStep): Record<string, unknown> {
  const raw = String(step.argsPreview || "").trim();
  if (!raw) return {};
  try {
    const value: unknown = JSON.parse(raw);
    return value && typeof value === "object" && !Array.isArray(value)
      ? value as Record<string, unknown>
      : {};
  } catch {
    return {};
  }
}

export function effectiveToolName(step: ChatTurnStep): string {
  const direct = String(step.tool || "").trim();
  if (direct !== "ipython") return direct;
  const args = parsedArgs(step);
  const code = String(args.code || args.source || step.argsPreview || "");
  const inner = INNER_TOOL_RE.exec(code)?.[1] || "";
  return inner || direct;
}

export function stepCategory(step: ChatTurnStep): StepCategory {
  const name = effectiveToolName(step);
  if (EDIT_RE.test(name)) return "edit";
  if (EXPLORE_RE.test(name) || /^browser_/i.test(name) || /^computer_/i.test(name)) return "explore";
  if (RUN_RE.test(name)) return "run";
  if (DELEGATE_RE.test(name)) return "delegate";
  return "other";
}

export function stepTarget(step: ChatTurnStep): string {
  const evidence = step.evidence?.find(item => item.value || item.label);
  if (evidence) {
    const value = compact(evidence.value || evidence.label, 90);
    return evidence.kind === "file" || evidence.kind === "folder" ? basename(value) : value;
  }
  const args = parsedArgs(step);
  for (const key of ["path", "file", "filepath", "file_path"] as const) {
    if (args[key]) return basename(compact(args[key], 90));
  }
  for (const key of ["query", "url"] as const) {
    if (args[key]) return compact(args[key], 90);
  }
  if (stepCategory(step) === "run") {
    const command = args.command || args.code || args.argv;
    if (Array.isArray(command)) return compact(command.join(" "), 90);
    if (command) return compact(command, 90);
  }
  const inner = effectiveToolName(step);
  if (inner && inner !== step.tool) {
    const code = String(args.code || args.source || step.argsPreview || "");
    const named = /(?:path|file|pattern|query|url)\s*[:=]\s*["']([^"']+)/i.exec(code)?.[1];
    const positional = /\btools\.[A-Za-z_]\w*\s*\(\s*["']([^"']+)/.exec(code)?.[1];
    const value = compact(named || positional || "", 90);
    if (value) return /[\\/]/.test(value) ? basename(value) : value;
    return "";
  }
  return "";
}

export function stepFailed(step: ChatTurnStep): boolean {
  return step.status === "error";
}

export function currentStepLabel(step: ChatTurnStep | undefined, live = false): string {
  if (!step) return live ? "Working" : "Completed";
  if (step.kind !== "tool") return compact(step.label, 120);
  const category = stepCategory(step);
  const copy = CATEGORY_COPY[category];
  const target = stepTarget(step);
  const verb = live && step.status === "running" ? copy.present : copy.past;
  return target ? `${verb} ${target}` : compact(step.label || effectiveToolName(step), 120);
}

export function normalizeActivityStatus(raw: string, event: string): ChatTurnStep["status"] {
  const status = String(raw || "").trim().toLowerCase().replace(/[\s-]+/g, "_");
  if (event === "tool:start" || status === "running" || status === "starting") return "running";
  if (["ok", "success", "succeeded", "complete", "completed", "done", "replayed"].includes(status)) return "ok";
  if ([
    "error", "failed", "failure", "invalid_arguments", "unavailable", "cancelled", "canceled",
    "timed_out", "timeout", "needs_reconciliation", "rejected", "blocked", "unknown_effect",
  ].includes(status)) return "error";
  return event === "tool:result" ? "ok" : "done";
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
