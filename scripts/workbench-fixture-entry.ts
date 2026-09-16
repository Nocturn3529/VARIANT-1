/** Isolated UI fixture. No real files, credentials, backend, or native actions. */
const root = "C:\\review-fixture";
const files = new Map([
  [`${root}\\sample.txt`, {text: "Original sample contents.\n", mtimeMs: 1}],
  [`${root}\\notes\\note.md`, {text: "# Sample note\n\nA nested file for keyboard checks.\n", mtimeMs: 1}],
]);
let facts = [{text: "Use concise answers."}];
let sessionTitle = "Welcome to VARIANT-1";
let browserDefault = {selection: {mode: "embedded"} as Record<string, unknown>, revision: 1};
const browserChats = new Map<string, {selection: Record<string, unknown>; revision: number}>();
const browserProfiles = Array.from({length: 8}, (_, index) => ({id: `fixture-profile-${index}`, label: `Sample profile ${index + 1}`, directory_name: `Profile ${index + 1}`}));
const sessions = () => [{id: "fixture-welcome", title: sessionTitle, pinned: true, updated_at: Date.now() / 1000}];
class FixtureSocket extends EventTarget {
  static OPEN = 1;
  readyState = 0;
  constructor() { super(); setTimeout(() => { this.readyState = 1; this.dispatchEvent(new Event("open")); }, 0); }
  emit(value: unknown) { setTimeout(() => this.dispatchEvent(new MessageEvent("message", {data: JSON.stringify(value)})), 0); }
  send(raw: string) {
    const message = JSON.parse(raw);
    if (message.type === "browser:settings:get") this.emit({type: "browser:settings", request_id: message.request_id,
      default: browserDefault, browsers: [{id: "fixture-chrome", label: "Chrome (fixture)", profiles: browserProfiles}]});
    if (message.type === "browser:state:get") {
      const choice = browserChats.get(message.chat_id) || {...browserDefault};
      if (!browserChats.has(message.chat_id)) browserChats.set(message.chat_id, choice);
      this.emit({type: "browser:state", request_id: message.request_id, chat_id: message.chat_id, state: "idle", message: "No browser operation is running in this fixture.",
        ...choice, selection_source: "chat", actions: []});
    }
    if (message.type === "browser:selection:set") {
      const prior = message.scope === "default" ? browserDefault : browserChats.get(message.chat_id) || browserDefault;
      const next = {selection: message.selection as Record<string, unknown>, revision: prior.revision + 1};
      if (message.scope === "default") browserDefault = next; else browserChats.set(message.chat_id, next);
      this.emit({type: "browser:selection:result", request_id: message.request_id, ok: true, scope: message.scope, chat_id: message.chat_id, ...next});
    }
    if (message.type === "chat:session:rename") {
      sessionTitle = message.title;
      this.emit({type: "chat:sessions", items: sessions()});
    }
    if (message.type === "chat:sessions") this.emit({type: "chat:sessions", items: sessions()});
    if (message.type === "memory:core:update") facts = facts.map(fact => fact.text === message.prior ? {text: message.text} : fact);
    if (message.type === "memory:core:get" || message.type === "memory:core:update") this.emit({type: "memory:core", items: facts, count: facts.length, cap: 40});
  }
  close() { this.readyState = 3; this.dispatchEvent(new Event("close")); }
}
Object.defineProperty(window, "WebSocket", {value: FixtureSocket});

function samplePdf() {
  const stream = "BT /F1 18 Tf 50 740 Td (VARIANT-1 PDF preview check) Tj ET";
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    `<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`,
  ];
  let pdf = "%PDF-1.4\n";
  const offsets = [0];
  objects.forEach((body, index) => { offsets.push(pdf.length); pdf += `${index + 1} 0 obj\n${body}\nendobj\n`; });
  const xref = pdf.length;
  pdf += `xref\n0 6\n0000000000 65535 f \n${offsets.slice(1).map(offset => `${String(offset).padStart(10, "0")} 00000 n \n`).join("")}`;
  pdf += `trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return `data:application/pdf;base64,${btoa(pdf)}`;
}
window.variant1Deck = {
  getBackendInfo: async () => ({port: 8771, token: "isolated-fixture"}),
  getWorkbenchRoot: async () => ({ok: true, path: root}),
  readWorkbenchDirectory: async path => ({ok: true, entries: path === root ? [
    {name: "notes", path: `${root}\\notes`, directory: true},
    {name: "sample.txt", path: `${root}\\sample.txt`, directory: false},
    {name: "sample.pdf", path: `${root}\\sample.pdf`, directory: false},
  ] : [{name: "note.md", path: `${root}\\notes\\note.md`, directory: false}]}),
  readWorkbenchFile: async path => ({ok: true, ...(files.get(path) || {text: "", mtimeMs: 1}), dataUrl: path.endsWith(".pdf") ? samplePdf() : ""}),
  writeWorkbenchFile: async (path, text, expected) => {
    const previous = files.get(path);
    if (expected !== undefined && previous?.mtimeMs !== expected) return {ok: false, conflict: true};
    const mtimeMs = (previous?.mtimeMs || 0) + 1;
    files.set(path, {text, mtimeMs});
    return {ok: true, mtimeMs};
  },
  renameWorkbenchPath: async (path, name) => {
    const file = files.get(path);
    if (!file) return {ok: false, error: "Sample file not found"};
    const cut = Math.max(path.lastIndexOf("\\"), path.lastIndexOf("/"));
    const next = (cut >= 0 ? path.slice(0, cut + 1) : "") + name;
    files.set(next, file); files.delete(path);
    return {ok: true, path: next};
  },
  getWorkbenchGitStatus: async () => ({ok: false, error: "No repository in this fixture"}),
  onWorkbenchPathChanged: () => () => {},
  watchWorkbenchPath: async () => ({id: "fixture-watch"}),
  stopWorkbenchWatch: async () => ({ok: true}),
};
document.addEventListener("securitypolicyviolation", event => console.error("FIXTURE_CSP_BLOCK", event.violatedDirective, event.blockedURI, event.sourceFile, event.lineNumber, event.columnNumber));
await import("../frontend/main-deck/src/main");
