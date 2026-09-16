import {createReconnectRefresh, pushWireStatus} from "./connectionUi";
import type { WsCommand } from "./protocol";
import {createModuleStore} from "./state/createModuleStore";
import {relativeTimeLabel} from "./state/storePrimitives";
import type {
  MemoryArchiveItem,
  MemoryCoreFact,
  MemoryLoopDetail,
  MemoryLoopSummary,
  MemoryProposal,
  MemoryState,
  RuntimeContext,
} from "./types";

const store = createModuleStore<MemoryState>({
  initialState: {
    connected: false,
    core: [],
    coreCount: 0,
    coreCap: 40,
    shownFacts: 5,
    archival: [],
    proposals: [],
    archiveQuery: "",
    shownArchive: 25,
    loops: [],
    loopActiveCount: 0,
    selectedLoopId: "",
    selectedLoop: null,
    draftLoopTitle: "",
    draftLoopGoal: "",
    exportState: "JSONL",
    draftFact: "",
  },
});

function parseCore(raw: unknown): MemoryCoreFact[] {
  if (!Array.isArray(raw)) return [];
  return raw.map(entry => {
    const row = (entry || {}) as Record<string, unknown>;
    return {
      text: String(row.text || ""),
      ts: row.ts == null ? null : row.ts as number | string,
    };
  }).filter(item => item.text);
}

function parseArchive(raw: unknown): MemoryArchiveItem[] {
  if (!Array.isArray(raw)) return [];
  return raw.map(entry => {
    const row = (entry || {}) as Record<string, unknown>;
    return {
      id: String(row.id || ""),
      text: String(row.text || ""),
      type: row.type == null ? undefined : String(row.type),
      created: row.created == null ? null : row.created as number | string,
    };
  });
}

function parseProposals(raw: unknown): MemoryProposal[] {
  if (!Array.isArray(raw)) return [];
  return raw.flatMap(entry => {
    if (!entry || typeof entry !== "object") return [];
    const row = entry as Record<string, unknown>;
    const proposalId = String(row.proposal_id || "");
    const content = String(row.content || "");
    if (!proposalId || !content) return [];
    return [{
      proposalId,
      chatId: String(row.chat_id || ""),
      kind: String(row.kind || "add"),
      content,
      createdAt: row.created_at as number | string | null ?? null,
      metadata: row.metadata && typeof row.metadata === "object"
        ? row.metadata as Record<string, unknown>
        : {},
    }];
  });
}

function parseLoops(raw: unknown): MemoryLoopSummary[] {
  if (!Array.isArray(raw)) return [];
  return raw.map(entry => {
    const row = (entry || {}) as Record<string, unknown>;
    return {
      id: String(row.id || ""),
      title: String(row.title || "Untitled"),
      status: String(row.status || "active"),
      created: row.created == null ? null : row.created as number | string,
      updated: row.updated == null ? null : row.updated as number | string,
      goal: row.goal == null ? undefined : String(row.goal),
      done_count: row.done_count == null ? 0 : Number(row.done_count),
      blocked_count: row.blocked_count == null ? 0 : Number(row.blocked_count),
      next_count: row.next_count == null ? 0 : Number(row.next_count),
      session_id: row.session_id == null ? undefined : String(row.session_id),
    };
  }).filter(item => item.id);
}

function parseLoopDetail(raw: unknown): MemoryLoopDetail | null {
  if (!raw || typeof raw !== "object") return null;
  const row = raw as Record<string, unknown>;
  return {
    id: String(row.id || (row.meta as {id?: string} | undefined)?.id || ""),
    meta: row.meta as MemoryLoopDetail["meta"],
    charter: row.charter as MemoryLoopDetail["charter"],
    progress: row.progress as MemoryLoopDetail["progress"],
    anchors: row.anchors as MemoryLoopDetail["anchors"],
  };
}

export function setMemoryContext(next: RuntimeContext) {
  store.setContext(next);
}

export function sendMemory(payload: WsCommand) {
  return store.send(payload);
}

/** Sticky On-device / Offline badge — ignores brief reconnect blips. */
const refreshOnConnection = createReconnectRefresh(refreshMemoryQuiet);
export function setMemoryConnection(status: string) {
  refreshOnConnection(status);
  pushWireStatus("memory", status, connected => store.setState({connected}));
}

export function notifyMemory(message: string) {
  store.getContext()?.notify(message);
}

export function relativeTimeMemory(ts: number | string | undefined | null): string {
  const context = store.getContext();
  if (context?.relativeTime) return context.relativeTime(ts);
  return relativeTimeLabel(ts);
}

export function setDraftFact(value: string) {
  store.setState({draftFact: value});
}

export function setArchiveQuery(value: string) {
  store.setState({archiveQuery: value});
}

export function toggleShownFacts() {
  const state = store.getState();
  const next = state.shownFacts >= state.core.length ? 5 : state.core.length;
  store.setState({shownFacts: next});
}

export function toggleShownArchive() {
  const state = store.getState();
  const filtered = filteredArchive(state);
  const next = state.shownArchive >= filtered.length
    ? 25
    : Math.min(filtered.length, state.shownArchive + 25);
  store.setState({shownArchive: next});
}

export function addCoreFact() {
  const state = store.getState();
  const text = state.draftFact.trim();
  if (!text) return;
  if (!sendMemory({type: "memory:core:set", text})) {
    notifyMemory("Backend offline — fact was not saved");
    return false;
  }
  store.setState({draftFact: ""});
  return true;
}

export function deleteCoreFact(text: string) {
  if (!text) return;
  if (!sendMemory({type: "memory:core:delete", text})) {
    notifyMemory("Backend offline — fact was not deleted");
    return false;
  }
  return true;
}

export function editCoreFact(prior: string, next: string) {
  if (next && next.trim() !== prior) {
    if (!sendMemory({type: "memory:core:update", prior, text: next.trim()})) {
      notifyMemory("Backend offline — fact changes were not saved");
      return false;
    }
    return true;
  }
  return false;
}

export function deleteArchive(id: string) {
  if (!id) return;
  if (!sendMemory({type: "memory:delete", id})) {
    notifyMemory("Backend offline — archival memory was not deleted");
    return false;
  }
  return true;
}

export function exportMemory() {
  if (!sendMemory({type: "memory:export"})) {
    notifyMemory("Backend offline — memory export was not started");
    return false;
  }
  store.setState({exportState: "Exporting…"});
  return true;
}

export async function openExportFolder() {
  const result = await store.getContext()?.api?.openAppPath?.("dataDir");
  if (!result?.ok) notifyMemory(result?.reason || "Could not open VARIANT-1 data folder");
}

export function tidyMemory() {
  sendMemory({type: "memory:consolidate"});
}

export function approveMemoryProposal(proposalId: string, content?: string) {
  if (!sendMemory({type: "memory:proposal:approve", proposal_id: proposalId, content})) {
    notifyMemory("Backend offline — memory approval was not saved");
    return false;
  }
  return true;
}

export function rejectMemoryProposal(proposalId: string) {
  if (!sendMemory({type: "memory:proposal:reject", proposal_id: proposalId})) {
    notifyMemory("Backend offline — memory rejection was not saved");
    return false;
  }
  return true;
}

export function setDraftLoopTitle(value: string) {
  store.setState({draftLoopTitle: value});
}

export function setDraftLoopGoal(value: string) {
  store.setState({draftLoopGoal: value});
}

export function createLoop() {
  const state = store.getState();
  const title = state.draftLoopTitle.trim();
  const goal = state.draftLoopGoal.trim();
  if (!title && !goal) {
    notifyMemory("Add a title or objective for the goal run");
    return false;
  }
  if (!sendMemory({
    type: "memory:loops:create",
    title: title || goal.slice(0, 80),
    goal,
    activate: true,
  })) {
    notifyMemory("Backend offline — goal run was not created");
    return false;
  }
  store.setState({draftLoopTitle: "", draftLoopGoal: ""});
  notifyMemory("Project run created");
  return true;
}

export function selectLoop(id: string) {
  if (!id) {
    store.setState({selectedLoopId: "", selectedLoop: null});
    return;
  }
  store.setState({selectedLoopId: id});
  sendMemory({type: "memory:loops:get", id});
}

export function controlLoop(id: string, action: string) {
  if (!id || !action) return;
  sendMemory({type: "memory:loops:control", id, action});
}

export function filteredArchive(
  snapshot: MemoryState = store.getState(),
): MemoryArchiveItem[] {
  const query = snapshot.archiveQuery.trim().toLowerCase();
  return snapshot.archival.filter(item =>
    !query || `${item.text || ""} ${item.type || ""}`.toLowerCase().includes(query),
  );
}

export function ingestMemory(message: Record<string, unknown>) {
  const type = String(message.type || "");
  const state = store.getState();

  if (type === "memory:core") {
    const core = message.items ? parseCore(message.items) : state.core;
    const coreCount = message.count == null ? core.length : Number(message.count);
    const coreCap = Number(message.cap || state.coreCap || 40);
    store.setState({connected: true, core, coreCount, coreCap});
    return;
  }

  if (type === "memory:list") {
    const archival = parseArchive(message.items);
    store.setState({connected: true, archival});
    return;
  }

  if (type === "memory:proposals") {
    store.setState({connected: true, proposals: parseProposals(message.items)});
    return;
  }

  if (type === "memory:loops") {
    const loops = parseLoops(message.items);
    const loopActiveCount = message.active_count == null
      ? loops.filter(l => l.status === "active").length
      : Number(message.active_count);
    let selectedLoopId = state.selectedLoopId;
    if (selectedLoopId && !loops.some(l => l.id === selectedLoopId)) {
      selectedLoopId = "";
    }
    store.setState({
      connected: true,
      loops,
      loopActiveCount,
      selectedLoopId,
      selectedLoop: selectedLoopId ? state.selectedLoop : null,
    });
    return;
  }

  if (type === "memory:loop") {
    const item = parseLoopDetail(message.item);
    if (item?.id) {
      store.setState({
        connected: true,
        selectedLoopId: item.id,
        selectedLoop: item,
      });
    } else if (message.deleted) {
      store.setState({selectedLoopId: "", selectedLoop: null});
    }
    return;
  }

  if (type === "memory:loop:promote") {
    const ok = message.ok !== false && !message.error;
    const msgText = message.message == null ? "" : String(message.message);
    const err = message.error == null ? "" : String(message.error);
    if (ok) {
      notifyMemory(msgText ? msgText.split("\n")[0].slice(0, 160) : "Promotion finished");
      if (message.item) {
        const item = parseLoopDetail(message.item);
        if (item?.id) {
          store.setState({selectedLoopId: item.id, selectedLoop: item, connected: true});
        }
      }
      // Refresh archival list after real promote.
      if (!message.dry_run) {
        sendMemory({type: "memory:list"});
        sendMemory({type: "memory:loops:list"});
      }
    } else {
      notifyMemory(err || "Could not promote lessons");
    }
    return;
  }

  if (type === "memory:consolidate") {
    const removed = Number(message.removed || 0);
    notifyMemory(removed ? `Tidied ${removed} duplicate memories` : "Memory is already tidy");
    refreshMemory();
    return;
  }

  if (type === "memory:export") {
    const count = Number(message.count || 0);
    const path = message.path == null ? "" : String(message.path);
    store.setState({exportState: `${count} records`});
    notifyMemory(
      count
        ? `Exported ${count} memory records to ${path || "the data folder"}`
        : "No structured memories to export yet",
    );
    return;
  }

  if (type === "memory:error") {
    notifyMemory(String(message.error || "Memory operation failed"));
  }
}

export function refreshMemory() {
  sendMemory({type: "memory:core:get"});
  sendMemory({type: "memory:list"});
  sendMemory({type: "memory:proposals"});
  sendMemory({type: "memory:loops:list"});
  const selectedLoopId = store.getState().selectedLoopId;
  if (selectedLoopId) {
    sendMemory({type: "memory:loops:get", id: selectedLoopId});
  }
  notifyMemory("Memory refreshed");
}

export function refreshMemoryQuiet() {
  sendMemory({type: "memory:core:get"});
  sendMemory({type: "memory:list"});
  sendMemory({type: "memory:proposals"});
  sendMemory({type: "memory:loops:list"});
}

export function useMemoryState() {
  return store.useStore();
}
