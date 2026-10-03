import {createReconnectRefresh, pushWireStatus} from "./connectionUi";
import type { WsCommand } from "./protocol";
import {createModuleStore} from "./state/createModuleStore";
import {relativeTimeLabel} from "./state/storePrimitives";
import type {
  GoalLoopDetail,
  GoalLoopSummary,
  GoalsState,
  RuntimeContext,
} from "./types";

const store = createModuleStore<GoalsState>({
  initialState: {
    connected: false,
    loops: [],
    loopActiveCount: 0,
    selectedLoopId: "",
    selectedLoop: null,
    draftLoopTitle: "",
    draftLoopGoal: "",
  },
});

function parseLoops(raw: unknown): GoalLoopSummary[] {
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

function parseLoopDetail(raw: unknown): GoalLoopDetail | null {
  if (!raw || typeof raw !== "object") return null;
  const row = raw as Record<string, unknown>;
  return {
    id: String(row.id || (row.meta as {id?: string} | undefined)?.id || ""),
    meta: row.meta as GoalLoopDetail["meta"],
    charter: row.charter as GoalLoopDetail["charter"],
    progress: row.progress as GoalLoopDetail["progress"],
    anchors: row.anchors as GoalLoopDetail["anchors"],
  };
}

export function setGoalsContext(next: RuntimeContext) {
  store.setContext(next);
}

export function sendGoals(payload: WsCommand) {
  return store.send(payload);
}

/** Sticky On-device / Offline badge — ignores brief reconnect blips. */
const refreshOnConnection = createReconnectRefresh(refreshGoalsQuiet);
export function setGoalsConnection(status: string) {
  refreshOnConnection(status);
  pushWireStatus("goals", status, connected => store.setState({connected}));
}

export function notifyGoals(message: string) {
  store.getContext()?.notify(message);
}

export function relativeTimeGoals(ts: number | string | undefined | null): string {
  const context = store.getContext();
  if (context?.relativeTime) return context.relativeTime(ts);
  return relativeTimeLabel(ts);
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
    notifyGoals("Add a title or objective for the goal run");
    return false;
  }
  if (!sendGoals({
    type: "goals:loops:create",
    title: title || goal.slice(0, 80),
    goal,
    activate: true,
  })) {
    notifyGoals("Backend offline — goal run was not created");
    return false;
  }
  store.setState({draftLoopTitle: "", draftLoopGoal: ""});
  notifyGoals("Project run created");
  return true;
}

export function selectLoop(id: string) {
  if (!id) {
    store.setState({selectedLoopId: "", selectedLoop: null});
    return;
  }
  store.setState({selectedLoopId: id});
  sendGoals({type: "goals:loops:get", id});
}

export function controlLoop(id: string, action: string) {
  if (!id || !action) return;
  sendGoals({type: "goals:loops:control", id, action});
}

export function ingestGoals(message: Record<string, unknown>) {
  const type = String(message.type || "");
  const state = store.getState();

  if (type === "goals:loops") {
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

  if (type === "goals:loop") {
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

}

export function refreshGoals() {
  sendGoals({type: "goals:loops:list"});
  const selectedLoopId = store.getState().selectedLoopId;
  if (selectedLoopId) {
    sendGoals({type: "goals:loops:get", id: selectedLoopId});
  }
  notifyGoals("Goals refreshed");
}

export function refreshGoalsQuiet() {
  sendGoals({type: "goals:loops:list"});
}

export function useGoalsState() {
  return store.useStore();
}
