/**
 * Pull files, URLs, screenshots, searches, and commands out of tool activity
 * (and assistant markdown) so Chat can show evidence cards without a new
 * backend contract. args_preview is a 160-char JSON dump or desktop kv line.
 */
import type {ChatEvidence, ChatEvidenceKind} from "./types";

const MAX_STEP_EVIDENCE = 8;

const PATH_KEYS = new Set([
  "path", "file", "filename", "filepath", "file_path",
  "cwd", "root", "directory", "dir",
]);
const URL_KEYS = new Set(["url", "href", "link", "uri"]);
const QUERY_KEYS = new Set(["query", "q", "search", "pattern"]);
const CMD_KEYS = new Set(["command", "cmd", "shell"]);
const SCREEN_TOOLS = new Set([
  "browser_screenshot", "computer", "vision", "capture_screen",
]);
const FOLDER_HINT = /[/\\]$|[/\\](?:src|lib|app|dist|docs|frontend|backend|scripts)$/i;
const JSON_FIELD = /"(path|file|filename|filepath|file_path|url|href|link|uri|query|q|pattern|command|cmd|cwd|directory|dir|target|root)"\s*:\s*"((?:\\.|[^"\\])*)/gi;
const URL_RE = /https?:\/\/[^\s<>"'`)\]},]+/gi;
const WIN_PATH_RE = /[A-Za-z]:[\\/][^\s"'<>|*?]{1,220}/g;
const UNIX_PATH_RE = /(?:^|[\s`'"(])((?:\.{1,2}\/|\/)(?:[\w.-]+\/)+[\w.-]+\.[A-Za-z0-9]{1,8})/g;
const PATCH_FILE_RE = /\*\*\*\s+(?:Add|Update|Delete) File:\s+(\S+)/g;
const KV_RE = /(\w+)=([^\s]+)/g;

export type EvidenceSource = {
  tool?: string;
  argsPreview?: string;
  text?: string;
};

function evidenceId(kind: ChatEvidenceKind, value: string): string {
  return `ev-${kind}:${value.toLowerCase().replace(/\s+/g, " ").slice(0, 80)}`;
}

function unescapeJson(value: string): string {
  return value
    .replace(/\\u([0-9a-fA-F]{4})/g, (_, hex) => String.fromCharCode(parseInt(hex, 16)))
    .replace(/\\["\\/bfnrt]/g, match => ({
      "\\\"": "\"",
      "\\\\": "\\",
      "\\/": "/",
      "\\b": "\b",
      "\\f": "\f",
      "\\n": "\n",
      "\\r": "\r",
      "\\t": "\t",
    }[match] || match));
}

function stripTrail(value: string): string {
  return value.replace(/[)\]}.,;:]+$/g, "").replace(/…+$/g, "").trim();
}

function basename(value: string): string {
  const clean = value.replace(/[\\/]+$/, "");
  const parts = clean.split(/[\\/]/);
  return parts[parts.length - 1] || value;
}

export function evidenceLabel(kind: ChatEvidenceKind, value: string): string {
  if (kind === "url") {
    try {
      const url = new URL(value);
      const host = url.hostname.replace(/^www\./, "");
      const tail = url.pathname.replace(/\/+$/, "").split("/").filter(Boolean).pop();
      return tail ? `${host} / ${tail}` : host || value;
    } catch {
      return value.slice(0, 48);
    }
  }
  if (kind === "file" || kind === "folder") return basename(value) || value;
  if (kind === "command") return value.split(/\s+/)[0] || "Command";
  if (kind === "screen") return value || "Screen";
  return value.slice(0, 64) || "Search";
}

function fileKind(value: string): ChatEvidenceKind {
  return FOLDER_HINT.test(value) ? "folder" : "file";
}

function pushItem(
  out: ChatEvidence[],
  kind: ChatEvidenceKind,
  rawValue: string,
  tool?: string,
  labelHint?: string,
): void {
  let value = stripTrail(rawValue);
  if (kind === "url") {
    if (value.toLowerCase().startsWith("file:")) {
      try {
        value = decodeURIComponent(value.replace(/^file:\/\//i, ""));
        if (/^\/[A-Za-z]:/.test(value)) value = value.slice(1);
      } catch {
        value = value.replace(/^file:\/\//i, "");
      }
      kind = fileKind(value);
    } else if (!/^https?:\/\//i.test(value)) {
      return;
    }
  }
  if (kind !== "screen" && !value) return;
  if (kind === "file" || kind === "folder") {
    if (value.length < 2 || value === "." || value === "..") return;
    if (/^https?:/i.test(value)) return;
    if (!value.startsWith("\\\\")) value = value.replace(/\\{2,}/g, "\\");
    if (!/[\\/]/.test(value) && !/\.[A-Za-z0-9]{1,8}$/.test(value)) return;
  }
  const label = labelHint || evidenceLabel(kind, value);
  const id = evidenceId(kind, value || label);
  if (out.some(item => item.id === id)) return;
  out.push({
    id,
    kind,
    label: label.slice(0, 120),
    value,
    tool: tool || undefined,
  });
}

function classifyKey(key: string): ChatEvidenceKind | null {
  if (PATH_KEYS.has(key)) return "file";
  if (URL_KEYS.has(key)) return "url";
  if (QUERY_KEYS.has(key)) return "search";
  if (CMD_KEYS.has(key)) return "command";
  return null;
}

function harvestJsonFields(raw: string, tool: string | undefined, out: ChatEvidence[]): void {
  JSON_FIELD.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = JSON_FIELD.exec(raw)) !== null) {
    const key = match[1].toLowerCase();
    const kind = classifyKey(key);
    if (!kind) continue;
    const value = unescapeJson(match[2] || "");
    pushItem(out, kind === "file" ? fileKind(value) : kind, value, tool);
  }
}

function harvestKv(raw: string, tool: string | undefined, out: ChatEvidence[]): void {
  KV_RE.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = KV_RE.exec(raw)) !== null) {
    const kind = classifyKey(match[1].toLowerCase());
    if (!kind) continue;
    const value = match[2] || "";
    pushItem(out, kind === "file" ? fileKind(value) : kind, value, tool);
  }
}

function harvestLoose(raw: string, tool: string | undefined, out: ChatEvidence[]): void {
  URL_RE.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = URL_RE.exec(raw)) !== null) {
    pushItem(out, "url", match[0], tool);
  }
  WIN_PATH_RE.lastIndex = 0;
  while ((match = WIN_PATH_RE.exec(raw)) !== null) {
    pushItem(out, fileKind(match[0]), match[0], tool);
  }
  UNIX_PATH_RE.lastIndex = 0;
  while ((match = UNIX_PATH_RE.exec(raw)) !== null) {
    pushItem(out, fileKind(match[1]), match[1], tool);
  }
  PATCH_FILE_RE.lastIndex = 0;
  while ((match = PATCH_FILE_RE.exec(raw)) !== null) {
    pushItem(out, fileKind(match[1]), match[1], tool);
  }
}

export function extractEvidence(source: EvidenceSource): ChatEvidence[] {
  const tool = String(source.tool || "").trim();
  const argsPreview = String(source.argsPreview || "");
  const text = String(source.text || "");
  const out: ChatEvidence[] = [];
  if (SCREEN_TOOLS.has(tool)) {
    const label = tool === "browser_screenshot" ? "Browser screenshot" : "Screen";
    pushItem(out, "screen", label, tool, label);
  }
  if (argsPreview) {
    harvestJsonFields(argsPreview, tool, out);
    if (!argsPreview.trimStart().startsWith("{")) {
      harvestKv(argsPreview, tool, out);
      harvestLoose(argsPreview, tool, out);
    }
  }
  if (text) harvestLoose(text, tool, out);
  return out.slice(0, MAX_STEP_EVIDENCE);
}

export function mergeEvidence(
  left?: ChatEvidence[],
  right?: ChatEvidence[],
): ChatEvidence[] | undefined {
  const out: ChatEvidence[] = [];
  for (const item of [...(left || []), ...(right || [])]) {
    if (!item || out.some(existing => existing.id === item.id)) continue;
    out.push(item);
    if (out.length >= MAX_STEP_EVIDENCE) break;
  }
  return out.length ? out : undefined;
}

export function parseEvidence(raw: unknown): ChatEvidence[] | undefined {
  if (!Array.isArray(raw) || !raw.length) return undefined;
  const out: ChatEvidence[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const row = item as Record<string, unknown>;
    const kindRaw = String(row.kind || "");
    const kind: ChatEvidenceKind | "" =
      kindRaw === "file" || kindRaw === "folder" || kindRaw === "url"
        || kindRaw === "screen" || kindRaw === "search" || kindRaw === "command"
        ? kindRaw
        : "";
    if (!kind) continue;
    const value = String(row.value || "").trim();
    const label = String(row.label || "").trim() || evidenceLabel(kind, value);
    if (!value && kind !== "screen") continue;
    pushItem(out, kind, value || label, row.tool != null ? String(row.tool) : undefined, label);
    if (out.length >= MAX_STEP_EVIDENCE) break;
  }
  return out.length ? out : undefined;
}
