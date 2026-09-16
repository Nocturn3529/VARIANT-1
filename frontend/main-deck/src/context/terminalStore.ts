import {getSessionState} from "../state/sessionStore";
import {createModuleStore} from "../state/createModuleStore";
import {createRequestIdFactory} from "../state/storePrimitives";
import type {RuntimeContext} from "../types";

const MAX_SCROLLBACK_CHARS = 2_000_000;
const POLL_INTERVAL_MS = 90;

export type TerminalSummary = Readonly<{
  id: string;
  state: string;
  cwd: string;
  profile: string;
  transport: string;
  truePty: boolean;
  outputCursor: number;
  cols: number;
  rows: number;
}>;

export type ProcessSummary = Readonly<{
  id: string;
  state: string;
  cwd: string;
  command: string;
  pid: number;
  outputCursor: number;
  exitCode: number | null;
}>;

export type TerminalState = Readonly<{
  notice?: string;
  output: string;
  outputStart: number;
  running: boolean;
  opening: boolean;
  supported: boolean;
  connected: boolean;
  terminals: readonly TerminalSummary[];
  processes: readonly ProcessSummary[];
  activeId: string;
  selectedProcessId: string;
  processOutput: string;
  error: string;
}>;

function createTerminalRuntime(ownerChatId:string) {
let observing=0;
const store = createModuleStore<TerminalState>({
  initialState: {
    output: "", outputStart: 0, running: false, opening: false, supported: false, connected: false,
    terminals: [], processes: [], activeId: "", selectedProcessId: "",
    processOutput: "", error: "",
  },
});
let creating: Promise<boolean> | null = null;
let lifecycleGeneration = 0;
let pollTimer: number | null = null;
const readsInFlight = new Set<string>();
const readRequests = new Map<string, string>();
const readTimers=new Map<string,ReturnType<typeof setTimeout>>();
const buffers = new Map<string, string>();
const bufferStarts = new Map<string, number>();
const resizePending = new Map<string,{requestId:string;timer:ReturnType<typeof setTimeout>}>();
const resizeDesired = new Map<string,{cols:number;rows:number}>();
const cursors = new Map<string, number>();
const processBuffers = new Map<string, string>();
const processCursors = new Map<string, number>();
const terminalDecoders=new Map<string,TextDecoder>(),processDecoders=new Map<string,TextDecoder>();
const pendingOpen = new Map<string, (ok: boolean) => void>();
const pendingClose = new Map<string, string>();
const closedTerminals = new Set<string>();
const dismissedProcesses = new Set<string>();
try {for(const id of JSON.parse(window.localStorage?.getItem(`variant1.dismissed-processes.${ownerChatId}`) || "[]"))if(typeof id==="string")dismissedProcesses.add(id);}catch {/* optional */}
try { for(const id of JSON.parse(window.localStorage?.getItem(`variant1.closed-terminals.${ownerChatId}`) || "[]"))if(typeof id==="string")closedTerminals.add(id); } catch { /* optional */ }

function dismissTerminal(id: string): void {
  closedTerminals.add(id);
  try { window.localStorage?.setItem(`variant1.closed-terminals.${ownerChatId}`,JSON.stringify([...closedTerminals].slice(-1000))); } catch { /* optional */ }
  buffers.delete(id); terminalDecoders.delete(id); bufferStarts.delete(id); cursors.delete(id); readsInFlight.delete(id);
  clearTimeout(resizePending.get(id)?.timer);resizePending.delete(id);resizeDesired.delete(id);
  try { window.localStorage?.removeItem(`variant1.terminal.scrollback.${id}`); } catch { /* optional */ }
  const state = store.getState();
  const terminals = state.terminals.filter(item => item.id !== id);
  const activeId = state.activeId === id ? terminals[0]?.id || "" : state.activeId;
  patch({terminals, activeId, output: buffers.get(activeId) || "", outputStart:bufferStarts.get(activeId) || 0, running: terminalLive(terminals.find(item => item.id === activeId))});
  schedulePoll(0);
}

const patch = store.setState;
const requestId = createRequestIdFactory("");

function terminalFrom(value: unknown): TerminalSummary | null {
  if (!value || typeof value !== "object") return null;
  const row = value as Record<string, unknown>;
  const id = String(row.id || row.terminal_id || "").trim();
  if (!id) return null;
  const dimensions = (row.dimensions || {}) as Record<string, unknown>;
  const capabilities = (row.capabilities || {}) as Record<string, unknown>;
  return {
    id,
    state: String(row.state || "unknown_effect"),
    cwd: String(row.cwd || ""),
    profile: String(row.profile || "terminal"),
    transport: String(row.transport || "unknown"),
    truePty: !!capabilities.true_pty,
    outputCursor: Number(row.output_cursor || 0),
    cols: Number(dimensions.cols || 120),
    rows: Number(dimensions.rows || 30),
  };
}

function processFrom(value: unknown): ProcessSummary | null {
  if (!value || typeof value !== "object") return null;
  const row = value as Record<string, unknown>;
  const id = String(row.id || row.process_id || "").trim();
  if (!id) return null;
  const recipe = row.recipe && typeof row.recipe === "object"
    ? row.recipe as Record<string, unknown> : {};
  const argv = Array.isArray(recipe.argv) ? recipe.argv.map(String) : [];
  return {
    id,
    state: String(row.state || "unknown_effect"),
    cwd: String(recipe.cwd || ""),
    command: argv.join(" "),
    pid: Number(row.pid || 0),
    outputCursor: Number(row.output_cursor || 0),
    exitCode: row.exit_code == null ? null : Number(row.exit_code),
  };
}

function terminalLive(terminal: TerminalSummary | undefined): boolean {
  return !!terminal && ["starting", "running", "terminating"].includes(terminal.state);
}

function upsertTerminal(terminal: TerminalSummary): void {
  if (closedTerminals.has(terminal.id)) return;
  if(!terminalLive(terminal) && [...pendingClose.values()].includes(terminal.id)) {
    for(const [request,id] of pendingClose)if(id===terminal.id)pendingClose.delete(request);
    dismissTerminal(terminal.id);return;
  }
  const state = store.getState();
  const next = [...state.terminals];
  const index = next.findIndex(item => item.id === terminal.id);
  if (index >= 0) next[index] = terminal;
  else next.unshift(terminal);
  const activeId = state.activeId || terminal.id;
  patch({
    terminals: next,
    activeId,
    running: terminalLive(next.find(item => item.id === activeId)),
    output: buffers.get(activeId) || "", outputStart:bufferStarts.get(activeId) || 0,
    error: "",
  });
}

function appendFor(terminalId: string, value: string): void {
  const id = String(terminalId || "");
  if (!id) return;
  const combined = `${buffers.get(id) || ""}${String(value)}`;
  const start=(bufferStarts.get(id) || 0)+Math.max(0,combined.length-MAX_SCROLLBACK_CHARS);
  const next=combined.slice(-MAX_SCROLLBACK_CHARS);
  buffers.set(id,next);bufferStarts.set(id,start);
  if (id === store.getState().activeId) patch({output:next,outputStart:start});
}

function stopPolling(): void {
  if (pollTimer != null) window.clearTimeout(pollTimer);
  pollTimer = null;
  readsInFlight.clear();
  for(const key of readRequests.keys())requestOwners.delete(key);
  for(const timer of readTimers.values())clearTimeout(timer);readTimers.clear();
  readRequests.clear();
}

function schedulePoll(delay = POLL_INTERVAL_MS): void {
  if (pollTimer != null) window.clearTimeout(pollTimer);
  pollTimer = null;
  const state = store.getState();
  if ((ownerChatId && !observing) || !state.connected || !store.getContext() || (!state.activeId && !state.selectedProcessId)) return;
  pollTimer = window.setTimeout(pollActive, delay);
}

function pollActive(): void {
  pollTimer = null;
  const state = store.getState();
  const entityId = state.selectedProcessId || state.activeId;
  if (!state.connected || !store.getContext() || !entityId
      || readsInFlight.has(entityId)) {
    schedulePoll();
    return;
  }
  const id = entityId;
  const process = !!state.selectedProcessId;
  const readRequestId = requestId(process ? "process-read" : "terminal-read");
  readsInFlight.add(id);
  readRequests.set(readRequestId, id);
  readTimers.set(readRequestId,setTimeout(()=>{readTimers.delete(readRequestId);readRequests.delete(readRequestId);requestOwners.delete(readRequestId);readsInFlight.delete(id);schedulePoll();},5000));
  const sent = store.send({
    type: process ? "process:logs" : "terminal:read", request_id: readRequestId,
    ...(process ? {process_id: id} : {terminal_id: id}),
    after_cursor: process ? (processCursors.get(id) || 0) : (cursors.get(id) || 0),
    max_bytes: 262_144, max_frames: 500,
  });
  if (!sent) {
    readsInFlight.delete(id);
    readRequests.delete(readRequestId);clearTimeout(readTimers.get(readRequestId));readTimers.delete(readRequestId);
  }
  schedulePoll(sent ? POLL_INTERVAL_MS : 500);
}

function setTerminalContext(context: RuntimeContext): void {
  store.setContext({...context,send: command => {
    const tagged: {type:string;[key:string]:unknown}={...command, ...(ownerChatId ? {chat_id:ownerChatId} : {})};
    if(tagged.request_id) requestOwners.set(String(tagged.request_id),ownerChatId);
    return context.send(tagged);
  }});
}

function setTerminalConnection(status: string): void {
  const connected = status === "connected";
  patch({connected, supported: connected, ...(connected ? {} : {running: false})});
  if (connected) {
    store.send({type: "execution:get", request_id: requestId("execution-get")});
    schedulePoll(0);
  } else {
    lifecycleGeneration += 1;
    stopPolling();
    for(const pending of resizePending.values())clearTimeout(pending.timer);resizePending.clear();
    creating = null; patch({opening:false});
    pendingOpen.forEach(resolve => resolve(false));
    pendingOpen.clear(); pendingClose.clear();
  }
}

function ingestOutputPage(value: unknown, responseRequestId = ""): void {
  if(responseRequestId && !readRequests.has(responseRequestId))return;
  clearTimeout(readTimers.get(responseRequestId));readTimers.delete(responseRequestId);
  if (!value || typeof value !== "object") return;
  const page = value as Record<string, unknown>;
  const entity = page.entity && typeof page.entity === "object"
    ? page.entity as Record<string, unknown>
    : {};
  const entityKind = String(entity.kind || "terminal");
  const terminalId = String(
    entity.id
    || readRequests.get(responseRequestId)
    || store.getState().activeId
    || "",
  );
  if (!terminalId) return;
  if (entityKind !== "process" && closedTerminals.has(terminalId)) {
    readsInFlight.delete(terminalId); readRequests.delete(responseRequestId); return;
  }
  if(entityKind==="process" && dismissedProcesses.has(terminalId)){readsInFlight.delete(terminalId);readRequests.delete(responseRequestId);return;}
  const frames = Array.isArray(page.frames) ? page.frames : [];
  let appended="",processText="";
  for (const raw of frames) {
    if (!raw || typeof raw !== "object") continue;
    const frame = raw as Record<string, unknown>;
    let text=typeof frame.text==="string" ? frame.text : "";
    if(typeof frame.data_base64==="string"){
      const decoders=entityKind==="process" ? processDecoders : terminalDecoders;
      let decoder=decoders.get(terminalId);if(!decoder){decoder=new TextDecoder();decoders.set(terminalId,decoder);}
      try{text=decoder.decode(Uint8Array.from(atob(frame.data_base64),char=>char.charCodeAt(0)),{stream:true});}catch{/* Legacy text remains a fallback for malformed frames. */}
    }
    if (text) {
      if (entityKind === "process") {
        processText+=text;
      } else appended+=text;
    }
    else if (frame.artifact_ref) {
      const notice = `\r\n[older output: ${String(frame.artifact_ref)}]\r\n`;
      if (entityKind === "process") {
        processText+=notice;
      } else appended+=notice;
    }
  }
  if(appended)appendFor(terminalId,appended);
  if(processText)processBuffers.set(terminalId,`${processBuffers.get(terminalId) || ""}${processText}`.slice(-MAX_SCROLLBACK_CHARS));
  const cursor = Number(page.next_cursor || page.cursor || 0);
  if (cursor >= 0 && terminalId) {
    if (entityKind === "process") processCursors.set(terminalId, cursor);
    else cursors.set(terminalId, cursor);
  }
  if (entityKind === "process" && terminalId === store.getState().selectedProcessId) {
    patch({processOutput: processBuffers.get(terminalId) || ""});
  }
  readsInFlight.delete(terminalId);
  if (responseRequestId) readRequests.delete(responseRequestId);
}

function ingestTerminal(message: Record<string, unknown>): void {
  const type = String(message.type || "");
  const state = store.getState();
  if (type === "execution:snapshot") {
    const terminals = Array.isArray(message.terminals)
      ? (message.terminals.map(terminalFrom).filter(Boolean) as TerminalSummary[]).filter(item => !closedTerminals.has(item.id)) : [];
    for(const terminal of terminals)if(!terminalLive(terminal) && [...pendingClose.values()].includes(terminal.id))upsertTerminal(terminal);
    const visibleTerminals=terminals.filter(t=>!closedTerminals.has(t.id));
    const processes = Array.isArray(message.processes)
      ? (message.processes.map(processFrom).filter(Boolean) as ProcessSummary[]).filter(item=>!dismissedProcesses.has(item.id)) : [];
    terminals.forEach(item => cursors.set(item.id, cursors.get(item.id) || 0));
    const activeId = visibleTerminals.some(item => item.id === state.activeId)
      ? state.activeId : visibleTerminals.find(terminalLive)?.id || visibleTerminals[0]?.id || "";
    patch({
      terminals:visibleTerminals, processes, activeId,
      running: terminalLive(terminals.find(item => item.id === activeId)),
      output: buffers.get(activeId) || "", outputStart:bufferStarts.get(activeId) || 0, supported: true, error: "",
    });
    for(const id of resizeDesired.keys())flushResize(id);
    schedulePoll(0);
    return;
  }
  if (type === "process:accepted") {
    const operation = String(message.operation || "");
    if (operation === "logs") {
      ingestOutputPage(message.result, String(message.request_id || ""));
    } else {
      const process = processFrom(message.result);
      if (process && !dismissedProcesses.has(process.id)) patch({
        processes: [process, ...state.processes.filter(item => item.id !== process.id)],
      });
    }
    schedulePoll(POLL_INTERVAL_MS);
    return;
  }
  if (type === "terminal:accepted") {
    const operation = String(message.operation || "");
    const result = message.result;
    if (operation === "read") {
      ingestOutputPage(result, String(message.request_id || ""));
      return;
    }
    const terminal = terminalFrom(result);
    if(operation==="resize"){
      const match=[...resizePending].find(([,pending])=>pending.requestId===message.request_id);
      if(!match)return;
      clearTimeout(match[1].timer);resizePending.delete(match[0]);
      if(terminal)upsertTerminal(terminal);
      flushResize(match[0]);return;
    }
    const closeId = String(message.request_id || "");
    if (operation === "close" && terminal && pendingClose.get(closeId) === terminal.id) {
      if (!terminalLive(terminal)) { pendingClose.delete(closeId); dismissTerminal(terminal.id); return; }
    }
    if (terminal) {
      if (operation === "open") {
        buffers.set(terminal.id, buffers.get(terminal.id) || "");
        if(!cursors.has(terminal.id)){cursors.set(terminal.id,0);terminalDecoders.delete(terminal.id);}
        patch({activeId: terminal.id, selectedProcessId: "", processOutput: "", output: buffers.get(terminal.id) || "",outputStart:bufferStarts.get(terminal.id) || 0});
      }
      upsertTerminal(terminal);
    }
    if(operation === "signal") {
      const receipt=(result as {signal_receipt?:{supported?:boolean;accepted?:boolean}}|undefined)?.signal_receipt;
      patch({notice:receipt?.accepted ? "Ctrl+C sent. The program may keep running; Close terminal ends the session." : "",error:receipt && (!receipt.supported || !receipt.accepted) ? "The terminal could not accept this interrupt." : ""});
    }
    const id = String(message.request_id || "");
    const resolve = pendingOpen.get(id);
    if (resolve) { pendingOpen.delete(id); resolve(!!terminal); }
    schedulePoll(0);
    return;
  }
  if (type === "terminal:rejected") {
    const id = String(message.request_id || "");
    pendingOpen.get(id)?.(false);
    pendingOpen.delete(id);
    pendingClose.delete(id);
    const resized=[...resizePending].find(([,pending])=>pending.requestId===id);
    if(resized){clearTimeout(resized[1].timer);resizePending.delete(resized[0]);resizeDesired.delete(resized[0]);}
    const readTerminalId = readRequests.get(id);
    if (readTerminalId) readsInFlight.delete(readTerminalId);
    readRequests.delete(id);clearTimeout(readTimers.get(id));readTimers.delete(id);
    patch({error: String(message.error || "Terminal operation failed")});
    schedulePoll(500);
  }
  if (type === "process:rejected") {
    const id = String(message.request_id || "");
    const readProcessId = readRequests.get(id);
    if (readProcessId) readsInFlight.delete(readProcessId);
    readRequests.delete(id);clearTimeout(readTimers.get(id));readTimers.delete(id);
    patch({error: String(message.error || "Process operation failed")});
    schedulePoll(500);
  }
}

function disposeTerminalRuntime() {
  lifecycleGeneration += 1;
  stopPolling();
  for(const pending of resizePending.values())clearTimeout(pending.timer);resizePending.clear();resizeDesired.clear();
  store.setContext(null);
  creating = null; patch({opening:false});
  pendingOpen.forEach(resolve => resolve(false));
  pendingOpen.clear();
  for(const timer of readTimers.values())clearTimeout(timer);readTimers.clear();
  buffers.clear();bufferStarts.clear();processBuffers.clear();cursors.clear();processCursors.clear();terminalDecoders.clear();processDecoders.clear();
  readsInFlight.clear();readRequests.clear();pendingClose.clear();
  patch({output:"",outputStart:0,processOutput:"",terminals:[],processes:[],activeId:"",selectedProcessId:""});
  const state = store.getState();
  if (state.connected || state.supported || state.running) {
    patch({connected: false, supported: false, running: false});
  }
}

function openBackendTerminal(cwd?: string): Promise<boolean> {
  if (!store.getContext()) return Promise.resolve(false);
  const id = requestId("terminal-open");
  const promise = new Promise<boolean>(resolve => {
    pendingOpen.set(id, resolve);
    window.setTimeout(() => {
      const pending = pendingOpen.get(id);
      if (!pending) return;
      pendingOpen.delete(id);
      pending(false);
    }, 10_000);
  });
  if (!store.send({
    type: "terminal:open", request_id: id, cwd: cwd || undefined,
    profile: "powershell", cols: 120, rows: 30,
  })) {
    pendingOpen.get(id)?.(false);
    pendingOpen.delete(id);
  }
  return promise;
}

async function openNewTerminal(cwd?: string): Promise<boolean> {
  if (!store.getState().connected) return false;
  if (creating) return creating;
  const generation = lifecycleGeneration;
  patch({opening:true});
  const attempt = openBackendTerminal(cwd).catch(() => false);
  let owned: Promise<boolean>;
  owned = attempt.finally(() => {
    if (creating === owned) creating = null;
    if (generation === lifecycleGeneration) patch({opening:false});
  });
  creating = owned;
  return owned;
}

function clearTerminalOutput(): void {
  const id=store.getState().activeId;
  if(id)window.dispatchEvent(new CustomEvent("variant1:terminal-clear-scrollback",{detail:{id,chatId:ownerChatId}}));
}

function selectTerminal(terminalId: string): void {
  const id = String(terminalId || "");
  const state = store.getState();
  const terminal = state.terminals.find(item => item.id === id);
  if (!terminal) return;
  patch({
    activeId: id, selectedProcessId: "", processOutput: "",
    output: buffers.get(id) || "", outputStart:bufferStarts.get(id) || 0, running: terminalLive(terminal), error: "",
  });
  schedulePoll(0);
}

function selectProcess(processId: string): void {
  const id = String(processId || "");
  if (!store.getState().processes.some(item => item.id === id)) return;
  patch({selectedProcessId: id, processOutput: processBuffers.get(id) || "", error: ""});
  schedulePoll(0);
}

function clearProcessSelection(): void {
  if (!store.getState().selectedProcessId) return;
  patch({selectedProcessId: "", processOutput: ""});
  schedulePoll(0);
}

function interruptTerminal(): boolean {
  const state = store.getState();
  if (state.connected && store.getContext() && state.activeId) {
    return store.send({
      type: "terminal:signal", request_id: requestId("terminal-signal"),
      terminal_id: state.activeId, name: "interrupt",
    });
  }
  return false;
}

function killTerminal(): boolean {
  const state = store.getState();
  const terminal = state.terminals.find(item => item.id === state.activeId);
  if (terminal && !terminalLive(terminal)) { dismissTerminal(terminal.id); return true; }
  if ([...pendingClose.values()].includes(state.activeId)) return false;
  if (state.connected && store.getContext() && state.activeId) {
    const id = requestId("terminal-close");
    pendingClose.set(id, state.activeId);
    const sent = store.send({
      type: "terminal:close", request_id: id,
      terminal_id: state.activeId, force: true,
    });
    if (!sent) pendingClose.delete(id);
    else window.setTimeout(()=>{
      if(!pendingClose.has(id))return;
      pendingClose.delete(id);
      patch({error:"Terminal close was not confirmed. Refresh its status before trying again."});
      refreshExecution();
    },15000);
    return sent;
  }
  return false;
}

/** Terminal protocol replies belong to their originating live PTY. Never create one. */
function writeTerminalInputFor(terminalId: string, data: string): boolean {
  const state = store.getState();
  const terminal = state.terminals.find(item => item.id === terminalId);
  if (!state.connected || !store.getContext() || !terminalLive(terminal)) return false;
  return store.send({type:"terminal:write", request_id:requestId("terminal-write"), terminal_id:terminalId, data});
}

function flushResize(id:string):boolean {
  if(resizePending.has(id))return true;
  const size=resizeDesired.get(id),state=store.getState(),terminal=state.terminals.find(row=>row.id===id);
  if(!size || !state.connected || !terminalLive(terminal))return false;
  if(terminal?.cols===size.cols && terminal.rows===size.rows){resizeDesired.delete(id);return true;}
  const key=requestId("terminal-resize");
  const timer=setTimeout(()=>{if(resizePending.get(id)?.requestId===key){resizePending.delete(id);requestOwners.delete(key);flushResize(id);}},2000);
  resizePending.set(id,{requestId:key,timer});
  if(store.send({type:"terminal:resize",request_id:key,terminal_id:id,...size}))return true;
  clearTimeout(timer);resizePending.delete(id);return false;
}
function resizeTerminalFor(id:string,cols:number,rows:number):boolean {
  if(!Number.isInteger(cols)||!Number.isInteger(rows)||cols<2||rows<2)return false;
  resizeDesired.set(id,{cols,rows});return flushResize(id);
}
function resizeTerminal(cols:number,rows:number):boolean {return resizeTerminalFor(store.getState().activeId,cols,rows);}

function dismissProcess(id=store.getState().selectedProcessId):boolean {
  const process=store.getState().processes.find(row=>row.id===id);
  if(!process || !processFinished(process.state))return false;
  dismissedProcesses.add(id);
  try{window.localStorage?.setItem(`variant1.dismissed-processes.${ownerChatId}`,JSON.stringify([...dismissedProcesses].slice(-1000)));}catch{/* optional */}
  processBuffers.delete(id);processDecoders.delete(id);processCursors.delete(id);readsInFlight.delete(id);
  for(const [key,owner] of readRequests)if(owner===id){readRequests.delete(key);requestOwners.delete(key);}
  const state=store.getState();
  patch({processes:state.processes.filter(row=>row.id!==id),...(state.selectedProcessId===id ? {selectedProcessId:"",processOutput:""} : {})});
  schedulePoll();return true;
}
function dismissExitedProcesses(){for(const row of store.getState().processes)if(processFinished(row.state))dismissProcess(row.id);}

function refreshExecution(): boolean {
  return store.send({type: "execution:get", request_id: requestId("execution-get")});
}

function activateTerminalSession(): void { refreshExecution(); schedulePoll(0); }

function getTerminalSnapshot(): TerminalState { return store.getState(); }
function useTerminalState(): TerminalState {
  return store.useStore();
}

function observe(visible: boolean) { observing += visible ? 1 : -1; observing=Math.max(0,observing); if(observing)schedulePoll(0);else stopPolling(); }
return {setTerminalContext,setTerminalConnection,ingestTerminal,disposeTerminalRuntime,openNewTerminal,clearTerminalOutput,selectTerminal,selectProcess,clearProcessSelection,interruptTerminal,killTerminal,writeTerminalInputFor,resizeTerminal,refreshExecution,activateTerminalSession,getTerminalSnapshot,useTerminalState,observe,resizeTerminalFor,dismissProcess,dismissExitedProcesses};
}

let sharedContext: RuntimeContext | null = null;
let sharedConnection = "offline";
const requestOwners = new Map<string,string>();
const runtimes = new Map<string,ReturnType<typeof createTerminalRuntime>>();
const deletedChats=new Set<string>();
let retiredRuntime:ReturnType<typeof createTerminalRuntime>|null=null;
function runtime(chatId = getSessionState().displayedSessionId || "") {
  if(deletedChats.has(chatId))return retiredRuntime ||= createTerminalRuntime("");
  let value=runtimes.get(chatId);
  if(!value) { value=createTerminalRuntime(chatId);runtimes.set(chatId,value);if(sharedContext)value.setTerminalContext(sharedContext);value.setTerminalConnection(sharedConnection); }
  return value;
}
export function setTerminalContext(ctx:RuntimeContext) {sharedContext=ctx;for(const r of runtimes.values())r.setTerminalContext(ctx);}
export function setTerminalConnection(status:string) {sharedConnection=status;if(!runtimes.size){runtime();return;}for(const r of runtimes.values())r.setTerminalConnection(status);}
export function ingestTerminal(message:Record<string,unknown>) {
  const scope=message.scope as {chat_id?:string}|undefined;
  const id=String(message.chat_id || scope?.chat_id || requestOwners.get(String(message.request_id || "")) || getSessionState().displayedSessionId || "");
  if(deletedChats.has(id))return;
  runtime(id).ingestTerminal(message);
  if(message.request_id)requestOwners.delete(String(message.request_id));
}
export function disposeTerminalRuntime(){for(const r of runtimes.values())r.disposeTerminalRuntime();runtimes.clear();requestOwners.clear();deletedChats.clear();retiredRuntime=null;sharedContext=null;sharedConnection="offline";}
export function disposeTerminalChat(chatId:string):void {
  deletedChats.add(chatId);
  const value=runtimes.get(chatId);
  for(const terminal of value?.getTerminalSnapshot().terminals || [])try{window.localStorage?.removeItem(`variant1.terminal.scrollback.${terminal.id}`);}catch{/* optional */}
  value?.disposeTerminalRuntime();runtimes.delete(chatId);
  for(const [key,owner] of requestOwners)if(owner===chatId)requestOwners.delete(key);
  for(const key of [`variant1.closed-terminals.${chatId}`,`variant1.dismissed-processes.${chatId}`])try{window.localStorage?.removeItem(key);}catch{/* optional */}
}
export function openNewTerminal(cwd?:string,chatId?:string){return runtime(chatId).openNewTerminal(cwd);}
export function clearTerminalOutput(chatId?:string){runtime(chatId).clearTerminalOutput();}
export function selectTerminal(id:string,chatId?:string){runtime(chatId).selectTerminal(id);}
export function selectProcess(id:string,chatId?:string){runtime(chatId).selectProcess(id);}
export function clearProcessSelection(chatId?:string){runtime(chatId).clearProcessSelection();}
export function interruptTerminal(chatId?:string){return runtime(chatId).interruptTerminal();}
export function killTerminal(chatId?:string){return runtime(chatId).killTerminal();}
export function writeTerminalInputFor(id:string,data:string,chatId?:string){return runtime(chatId).writeTerminalInputFor(id,data);}
export function resizeTerminal(cols:number,rows:number,chatId?:string){return runtime(chatId).resizeTerminal(cols,rows);}
export function refreshExecution(chatId?:string){return runtime(chatId).refreshExecution();}
export function activateTerminalSession(){runtime().activateTerminalSession();}
export function getTerminalSnapshot(chatId?:string){return runtime(chatId).getTerminalSnapshot();}
export function useTerminalState(chatId?:string){return runtime(chatId).useTerminalState();}
export function observeTerminal(chatId:string){const value=runtime(chatId);value.observe(true);return ()=>value.observe(false);}

export function processFinished(state:string):boolean{return ["exited","terminated","failed","stopped","completed","cancelled"].includes(state);}
export function dismissProcess(id:string,chatId?:string){return runtime(chatId).dismissProcess(id);}
export function dismissExitedProcesses(chatId?:string){runtime(chatId).dismissExitedProcesses();}
export function resizeTerminalFor(id:string,cols:number,rows:number,chatId?:string){return runtime(chatId).resizeTerminalFor(id,cols,rows);}
