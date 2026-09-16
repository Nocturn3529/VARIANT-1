const SAFE_LINK = /^https?:\/\//i;
const LIST_RE = /^(\s*)([-+*]|\d+[.)])\s+(.*)$/;
const FENCE_RE = /^\s*```([\w+-]*)\s*$/;
const TABLE_DIVIDER_RE = /^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$/;

export type InlineToken =
  | {type: "text"; text: string}
  | {type: "code"; text: string}
  | {type: "strong" | "em"; children: InlineToken[]}
  | {type: "link"; href: string; children: InlineToken[]};

export type ListItem = {
  inline: InlineToken[];
  children: MdBlock[];
};

export type MdBlock =
  | {type: "heading"; level: number; inline: InlineToken[]}
  | {type: "paragraph"; lines: InlineToken[][]}
  | {type: "blockquote"; blocks: MdBlock[]}
  | {type: "list"; ordered: boolean; items: ListItem[]}
  | {type: "table"; headers: InlineToken[][]; rows: InlineToken[][][]}
  | {type: "code"; language: string; text: string};

export type HighlightToken = {
  type: "text" | "comment" | "string" | "number" | "keyword";
  text: string;
};

export type MessageHeights = ReadonlyArray<number | null | undefined> | null | undefined;

export type VirtualWindowOptions = {
  total?: number;
  scrollTop?: number;
  viewportHeight?: number;
  heights?: MessageHeights;
  defaultHeight?: number;
  overscan?: number;
};

export type VirtualWindow = {
  start: number;
  end: number;
  topPad: number;
  bottomPad: number;
  totalHeight: number;
  firstVisible?: number;
};

export function safeHttpUrl(value: unknown): string {
  const raw = String(value || "").trim();
  if (!SAFE_LINK.test(raw)) return "";
  try {
    const parsed = new URL(raw);
    return parsed.protocol === "http:" || parsed.protocol === "https:" ? parsed.href : "";
  } catch {
    return "";
  }
}

export function formatTime(ts?: number): string {
  const date = new Date((Number(ts) || Date.now() / 1000) * 1000);
  return date.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
}

function messageHeightAt(
  heights: MessageHeights,
  index: number,
  defaultHeight = 96,
): number {
  const measured = heights && heights[index];
  const n = Number(measured);
  if (Number.isFinite(n) && n > 0) return n;
  const fallback = Number(defaultHeight);
  return Number.isFinite(fallback) && fallback > 0 ? fallback : 96;
}

export function sumMessageHeights(
  heights: MessageHeights,
  start: number,
  end: number,
  defaultHeight = 96,
): number {
  const from = Math.max(0, Number(start) || 0);
  const to = Math.max(from, Number(end) || 0);
  let total = 0;
  for (let index = from; index < to; index += 1) {
    total += messageHeightAt(heights, index, defaultHeight);
  }
  return total;
}

function indexAtScrollOffset(
  heights: MessageHeights,
  offset: number,
  total: number,
  defaultHeight = 96,
): number {
  const n = Math.max(0, Number(total) || 0);
  if (n <= 0) return 0;
  let remaining = Math.max(0, Number(offset) || 0);
  for (let index = 0; index < n; index += 1) {
    const height = messageHeightAt(heights, index, defaultHeight);
    if (remaining < height) return index;
    remaining -= height;
  }
  return n - 1;
}

/** Compute an inclusive-exclusive message window and its spacer heights. */
export function virtualWindow({
  total = 0,
  scrollTop = 0,
  viewportHeight = 600,
  heights = null,
  defaultHeight = 96,
  overscan = 8,
}: VirtualWindowOptions = {}): VirtualWindow {
  const n = Math.max(0, Number(total) || 0);
  if (n === 0) {
    return {start: 0, end: 0, topPad: 0, bottomPad: 0, totalHeight: 0};
  }
  const resolvedDefault = Number(defaultHeight) > 0 ? Number(defaultHeight) : 96;
  const resolvedOverscan = Math.max(0, Math.floor(Number(overscan) || 0));
  const view = Math.max(1, Number(viewportHeight) || 1);
  const top = Math.max(0, Number(scrollTop) || 0);
  const first = indexAtScrollOffset(heights, top, n, resolvedDefault);
  const start = Math.max(0, first - resolvedOverscan);
  let end = first;
  let accumulated = sumMessageHeights(heights, 0, first, resolvedDefault);
  while (end < n && accumulated < top + view) {
    accumulated += messageHeightAt(heights, end, resolvedDefault);
    end += 1;
  }
  end = Math.min(n, end + resolvedOverscan);
  if (end <= start) end = Math.min(n, start + 1);
  const topPad = sumMessageHeights(heights, 0, start, resolvedDefault);
  const middleHeight = sumMessageHeights(heights, start, end, resolvedDefault);
  const totalHeight = sumMessageHeights(heights, 0, n, resolvedDefault);
  const bottomPad = Math.max(0, totalHeight - topPad - middleHeight);
  return {start, end, topPad, bottomPad, totalHeight, firstVisible: first};
}

function parseInline(value: unknown): InlineToken[] {
  const text = String(value || "");
  const tokens: InlineToken[] = [];
  let cursor = 0;
  // Groups: 1 code, 2 strong, 3 em, 4 md-link whole, 5 label, 6 href, 7 bare URL.
  const special = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\*[^*\n]+\*)|(\[([^\]]+)\]\(([^)\s]+)\))|(https?:\/\/[^\s<>()]+)/g;
  let match: RegExpExecArray | null;
  while ((match = special.exec(text))) {
    if (match.index > cursor) {
      tokens.push({type: "text", text: text.slice(cursor, match.index)});
    }
    if (match[1]) {
      tokens.push({type: "code", text: match[1].slice(1, -1)});
    } else if (match[2]) {
      tokens.push({type: "strong", children: parseInline(match[2].slice(2, -2))});
    } else if (match[3]) {
      tokens.push({type: "em", children: parseInline(match[3].slice(1, -1))});
    } else {
      const bareUrl = match[7] || "";
      const href = safeHttpUrl(match[6] || bareUrl);
      const label = match[5] || bareUrl || "";
      if (!href) {
        tokens.push({type: "text", text: match[0]});
      } else if (bareUrl && !match[5]) {
        // Re-parsing an autolink label would match the same URL forever.
        tokens.push({type: "link", href, children: [{type: "text", text: bareUrl}]});
      } else {
        tokens.push({type: "link", href, children: parseInline(label)});
      }
    }
    cursor = special.lastIndex;
  }
  if (cursor < text.length) tokens.push({type: "text", text: text.slice(cursor)});
  return tokens;
}

function splitTableRow(line: string): string[] {
  let value = String(line || "").trim();
  if (value.startsWith("|")) value = value.slice(1);
  if (value.endsWith("|")) value = value.slice(0, -1);
  const cells: string[] = [];
  let current = "";
  let escaped = false;
  for (const char of value) {
    if (escaped) {
      current += char;
      escaped = false;
    } else if (char === "\\") {
      escaped = true;
    } else if (char === "|") {
      cells.push(current.trim());
      current = "";
    } else {
      current += char;
    }
  }
  cells.push(current.trim());
  return cells;
}

function blockStarts(lines: string[], index: number): boolean {
  const line = lines[index] || "";
  if (!line.trim()) return true;
  if (
    FENCE_RE.test(line)
    || /^\s*#{1,6}\s+/.test(line)
    || /^\s*>/.test(line)
    || LIST_RE.test(line)
  ) return true;
  return index + 1 < lines.length
    && line.includes("|")
    && TABLE_DIVIDER_RE.test(lines[index + 1]);
}

type ParsedList = {
  block: Extract<MdBlock, {type: "list"}>;
  next: number;
};

function parseList(lines: string[], start: number, baseIndent: number): ParsedList {
  const first = LIST_RE.exec(lines[start]);
  const ordered = Boolean(first && /^\d/.test(first[2]));
  const items: ListItem[] = [];
  let index = start;
  while (index < lines.length) {
    const match = LIST_RE.exec(lines[index]);
    if (!match) break;
    const indent = match[1].replace(/\t/g, "    ").length;
    if (indent < baseIndent) break;
    if (indent > baseIndent) {
      if (!items.length) break;
      const nested = parseList(lines, index, indent);
      items[items.length - 1].children.push(nested.block);
      index = nested.next;
      continue;
    }
    if (/^\d/.test(match[2]) !== ordered) break;
    const item: ListItem = {inline: parseInline(match[3]), children: []};
    items.push(item);
    index += 1;
    while (index < lines.length && lines[index].trim() && !LIST_RE.test(lines[index])) {
      if (blockStarts(lines, index)) break;
      item.children.push({type: "paragraph", lines: [parseInline(lines[index].trim())]});
      index += 1;
    }
  }
  return {block: {type: "list", ordered, items}, next: index};
}

export function parseMarkdown(value: unknown): MdBlock[] {
  const lines = String(value || "").replace(/\r\n?/g, "\n").split("\n");
  const blocks: MdBlock[] = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }
    const fence = FENCE_RE.exec(line);
    if (fence) {
      const code: string[] = [];
      index += 1;
      while (index < lines.length && !/^\s*```\s*$/.test(lines[index])) {
        code.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      blocks.push({
        type: "code",
        language: (fence[1] || "").toLowerCase(),
        text: code.join("\n"),
      });
      continue;
    }
    const heading = /^\s*(#{1,6})\s+(.+)$/.exec(line);
    if (heading) {
      blocks.push({type: "heading", level: heading[1].length, inline: parseInline(heading[2])});
      index += 1;
      continue;
    }
    if (/^\s*>/.test(line)) {
      const quoted: string[] = [];
      while (index < lines.length && /^\s*>/.test(lines[index])) {
        quoted.push(lines[index].replace(/^\s*>\s?/, ""));
        index += 1;
      }
      blocks.push({type: "blockquote", blocks: parseMarkdown(quoted.join("\n"))});
      continue;
    }
    const listMatch = LIST_RE.exec(line);
    if (listMatch) {
      const parsed = parseList(lines, index, listMatch[1].replace(/\t/g, "    ").length);
      blocks.push(parsed.block);
      index = parsed.next;
      continue;
    }
    if (
      index + 1 < lines.length
      && line.includes("|")
      && TABLE_DIVIDER_RE.test(lines[index + 1])
    ) {
      const headers = splitTableRow(line).map(parseInline);
      index += 2;
      const rows: InlineToken[][][] = [];
      while (index < lines.length && lines[index].trim() && lines[index].includes("|")) {
        rows.push(splitTableRow(lines[index]).map(parseInline));
        index += 1;
      }
      blocks.push({type: "table", headers, rows});
      continue;
    }
    const paragraph: InlineToken[][] = [];
    while (index < lines.length && lines[index].trim() && !blockStarts(lines, index)) {
      paragraph.push(parseInline(lines[index]));
      index += 1;
    }
    if (!paragraph.length) {
      paragraph.push(parseInline(lines[index]));
      index += 1;
    }
    blocks.push({type: "paragraph", lines: paragraph});
  }
  return blocks;
}

const JAVASCRIPT_KEYWORDS = new Set(
  "async await break case catch class const continue default delete do else export extends false finally for from function if import in instanceof let new null of return static super switch this throw true try typeof undefined var void while yield".split(" "),
);
const TYPESCRIPT_KEYWORDS = new Set(
  "abstract any as async await boolean break case catch class const constructor continue declare default delete do else enum export extends false finally for from function if implements import in infer interface keyof let namespace never new null number object of private protected public readonly return static string super switch symbol this throw true try type typeof undefined unknown var void while yield".split(" "),
);
const PYTHON_KEYWORDS = new Set(
  "and as assert async await break class continue def del elif else except False finally for from global if import in is lambda None nonlocal not or pass raise return True try while with yield".split(" "),
);
const BASH_KEYWORDS = new Set(
  "case do done elif else esac export fi for function if in local then while".split(" "),
);

const LANGUAGE_KEYWORDS: Readonly<Record<string, ReadonlySet<string>>> = {
  js: JAVASCRIPT_KEYWORDS,
  javascript: JAVASCRIPT_KEYWORDS,
  ts: TYPESCRIPT_KEYWORDS,
  python: PYTHON_KEYWORDS,
  py: PYTHON_KEYWORDS,
  json: new Set("true false null".split(" ")),
  bash: BASH_KEYWORDS,
  sh: BASH_KEYWORDS,
  powershell: new Set(
    "begin break catch class continue data do dynamicparam else elseif end enum exit filter finally for foreach from function hidden if in param process return static switch throw trap try until using var while".split(" "),
  ),
};

export function highlightCode(code: unknown, language: unknown): HighlightToken[] {
  const source = String(code || "");
  const lang = String(language || "").toLowerCase();
  const keywords = LANGUAGE_KEYWORDS[lang] || new Set<string>();
  const pattern = /(\/\/[^\n]*|#[^\n]*|\/\*[\s\S]*?\*\/)|(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)|(\b\d+(?:\.\d+)?\b)|(\b[A-Za-z_$][\w$-]*\b)/g;
  const tokens: HighlightToken[] = [];
  let cursor = 0;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(source))) {
    if (match.index > cursor) {
      tokens.push({type: "text", text: source.slice(cursor, match.index)});
    }
    const word = match[4];
    const type: HighlightToken["type"] = match[1]
      ? "comment"
      : match[2]
        ? "string"
        : match[3]
          ? "number"
          : keywords.has(word)
            ? "keyword"
            : "text";
    tokens.push({type, text: match[0]});
    cursor = pattern.lastIndex;
  }
  if (cursor < source.length) tokens.push({type: "text", text: source.slice(cursor)});
  return tokens;
}

const runtimeLib = {
  formatTime,
  highlightCode,
  parseMarkdown,
  safeHttpUrl,
  sumMessageHeights,
  virtualWindow,
};

export default runtimeLib;
