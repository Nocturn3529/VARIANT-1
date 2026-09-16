/**
 * Turn receipt projection. A quick client snapshot keeps the completed bubble
 * useful at done; run:settled later replaces it with the backend aggregate.
 */
import {getContextForSession} from "../sessionContextStore";
import {getChatState} from "./stateCore";
import type {ChatTurnReceipt} from "./types";

type Snapshot = {
  startedAt: number;
  tools: string[];
};

const snapshots = new Map<string, Snapshot>();

export function resetTurnReceipt(): void {
  snapshots.clear();
}

export function beginTurnReceipt(): void {
  snapshots.set(getChatState().sessionId || "", {
    startedAt: Date.now(),
    tools: [],
  });
}

export function noteReceiptTool(name: string): void {
  const tool = String(name || "").trim();
  const snapshot = snapshots.get(getChatState().sessionId || "");
  if (!tool || !snapshot) return;
  if (!snapshot.tools.includes(tool)) snapshot.tools.push(tool);
}

export function snapshotTurnReceipt(): ChatTurnReceipt | undefined {
  const id = getChatState().sessionId || "", snapshot = snapshots.get(id);
  if (!snapshot) return undefined;
  const context = getContextForSession(id);
  const cached = context.cachedInputTokens;
  const receipt: ChatTurnReceipt = {
    model: context.model || "",
    provider: context.provider || "",
    route: context.route || "",
    durationMs: Math.max(0, Date.now() - snapshot.startedAt),
    // Session context reports the input used by the current model request, not
    // a cumulative meter. Subtracting the previous request made equal-sized
    // prompts display as zero and shrinking prompts disappear entirely.
    promptTokens: context.status === "ready" ? context.usedTokens : null,
    cachedInputTokens: cached > 0 ? cached : null,
    toolCount: snapshot.tools.length,
    measurement: context.measurement || "estimated",
  };
  snapshots.delete(id);
  if (!receipt.model && !receipt.durationMs && !receipt.toolCount) return undefined;
  return receipt;
}

export function shortModelName(model: string): string {
  const raw = String(model || "").trim();
  if (!raw) return "";
  const tail = raw.split(/[/\\]/).pop() || raw;
  return tail.replace(/\.(gguf|bin)$/i, "").replace(/[-_]+/g, " ");
}

export function formatTokenCount(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}m`;
  if (value >= 10_000) return `${Math.round(value / 1000)}k`;
  if (value >= 1000) return `${(value / 1000).toFixed(1)}k`;
  return String(Math.round(value));
}

export function readableToolName(name: string): string {
  const n = String(name || "").trim();
  if (!n) return "Tool";
  const aliases: Record<string, string> = {
    ipython: "Python",
    read_file: "Read file",
    apply_patch: "Edit file",
    run_command: "Run command",
    glob: "Find files",
    grep: "Search files",
    web_search: "Search the web",
    computer: "Use the computer",
    browser_navigate: "Open page",
    browser_read: "Read page",
    browser_click: "Click in browser",
    browser_fill: "Fill in browser",
    browser_screenshot: "Browser screenshot",
    retrieve_memory: "Recall memory",
    "memory.retrieve": "Recall memory",
  };
  if (aliases[n]) return aliases[n];
  return n.replaceAll("_", " ");
}

export function parseTurnReceipt(raw: unknown): ChatTurnReceipt | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const row = raw as Record<string, unknown>;
  const rawDuration = row.durationMs ?? row.duration_ms;
  const durationMs = rawDuration == null
    ? Number(row.wall_time_s) * 1000
    : Number(rawDuration);
  const models = Array.isArray(row.models)
    ? row.models.map(value => String(value || "")).filter(Boolean)
    : [];
  const providers = Array.isArray(row.routes)
    ? row.routes.map(value => String(value || "")).filter(Boolean)
    : [];
  const model = String(row.model || models.join(", "));
  const provider = String(row.provider || providers.join(", "));
  const hasMetrics = (
    row.toolCount != null || row.tool_count != null || row.tool_calls != null
    || row.promptTokens != null || row.prompt_tokens != null
  );
  if (!Number.isFinite(durationMs) && !model && !hasMetrics) return undefined;
  const route = row.route === "cloud" || row.route === "local"
    ? row.route
    : providers.length
      ? (providers.every(value => value === "local") ? "local" : "cloud")
      : "";
  return {
    model,
    provider,
    route,
    durationMs: Math.max(0, durationMs || 0),
    promptTokens: row.promptTokens == null && row.prompt_tokens == null
      ? null
      : Number(row.promptTokens ?? row.prompt_tokens) || 0,
    cachedInputTokens: row.cachedInputTokens == null && row.cached_input_tokens == null
      ? null
      : Number(row.cachedInputTokens ?? row.cached_input_tokens) || 0,
    toolCount: Math.max(
      0, Number(row.toolCount ?? row.tool_count ?? row.tool_calls) || 0,
    ),
    measurement: String(row.measurement || (row.version ? "run_receipt" : "estimated")),
  };
}
