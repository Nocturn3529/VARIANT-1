import {createReconnectRefresh, pushWireStatus} from "./connectionUi";
import type {WsCommand} from "./protocol";
import {createModuleStore} from "./state/createModuleStore";
import type {RuntimeContext} from "./types";

export type AutomationTrigger = {
  type?: string;
  time?: string;
  day?: string;
  seconds?: number;
  expr?: string;
  path?: string;
  event?: string;
  title_contains?: string;
  token?: string;
};

export type AutomationRuntime = {
  runtime_id: string;
  assignment_state: "pinned" | "unassigned";
  lifecycle_state: string;
  action_surface: string;
  trust_profile: string;
  graph_revision: string;
  catalog_release_id: string;
  busy: boolean;
  mutation_enabled: false;
  kernel: {
    state: string;
    generation?: number;
    pid?: number | null;
  };
  warning: string;
  error: string;
};

export type AutomationItem = {
  id: string;
  name: string;
  prompt: string;
  enabled: boolean;
  schedule: string;
  schedule_error?: string;
  schedule_runnable?: boolean;
  last_run?: number | string | null;
  trigger: AutomationTrigger;
  misfire_policy: "latest" | "skip";
  runtime?: AutomationRuntime;
};

export type AutomationRun = {
  automation_id: string;
  status: string;
  summary: string;
  started_at?: number;
  finished_at?: number;
};

export type AutomationFilter = "all" | "active" | "scheduled" | "event";

type AutomationState = Readonly<{
  connected: boolean;
  items: AutomationItem[];
  history: AutomationRun[];
  historyCount: number;
  filter: AutomationFilter;
  builderOpen: boolean;
  editing: AutomationItem | null;
  editorGeneration: number;
  pendingSave: {requestId: string; generation: number; label: string} | null;
  saveError: string;
}>;

const store = createModuleStore<AutomationState>({
  initialState: {
    connected: false,
    items: [],
    history: [],
    historyCount: 0,
    filter: "all",
    builderOpen: false,
    editing: null,
    editorGeneration: 0,
    pendingSave: null,
    saveError: "",
  },
});
const patch = store.setState;
export const getAutomationState = store.getState;

function send(payload: WsCommand): boolean {
  return store.send(payload);
}

function parseItems(raw: unknown): AutomationItem[] {
  if (!Array.isArray(raw)) return [];
  return raw.map(entry => {
    const row = (entry || {}) as Record<string, unknown>;
    const trigger = row.trigger && typeof row.trigger === "object"
      ? row.trigger as AutomationTrigger
      : {};
    const misfirePolicy: AutomationItem["misfire_policy"] =
      row.misfire_policy === "skip" ? "skip" : "latest";
    const runtimeRow = row.runtime && typeof row.runtime === "object"
      ? row.runtime as Record<string, unknown>
      : null;
    const kernelRow = runtimeRow?.kernel && typeof runtimeRow.kernel === "object"
      ? runtimeRow.kernel as Record<string, unknown>
      : {};
    const runtime: AutomationRuntime | undefined = runtimeRow ? {
      runtime_id: String(runtimeRow.runtime_id || ""),
      assignment_state: runtimeRow.assignment_state === "pinned" ? "pinned" : "unassigned",
      lifecycle_state: String(runtimeRow.lifecycle_state || ""),
      action_surface: String(runtimeRow.action_surface || ""),
      trust_profile: String(runtimeRow.trust_profile || ""),
      graph_revision: String(runtimeRow.graph_revision || ""),
      catalog_release_id: String(runtimeRow.catalog_release_id || ""),
      busy: !!runtimeRow.busy,
      mutation_enabled: false,
      kernel: {
        state: String(kernelRow.state || "absent"),
        generation: kernelRow.generation == null ? undefined : Number(kernelRow.generation),
        pid: kernelRow.pid == null ? null : Number(kernelRow.pid),
      },
      warning: String(runtimeRow.warning || ""),
      error: String(runtimeRow.error || ""),
    } : undefined;
    return {
      id: String(row.id || ""),
      name: String(row.name || "Untitled automation"),
      prompt: String(row.prompt || ""),
      enabled: !!row.enabled,
      schedule: String(row.schedule || "Unknown trigger"),
      schedule_error: String(row.schedule_error || ""),
      schedule_runnable: row.schedule_runnable == null ? !!row.enabled : !!row.schedule_runnable,
      last_run: row.last_run as number | string | null | undefined,
      trigger,
      misfire_policy: misfirePolicy,
      runtime,
    };
  }).filter(item => item.id);
}

function parseHistory(raw: unknown): AutomationRun[] {
  if (!Array.isArray(raw)) return [];
  return raw.map(entry => {
    const row = (entry || {}) as Record<string, unknown>;
    return {
      automation_id: String(row.automation_id || ""),
      status: String(row.status || "unknown"),
      summary: String(row.summary || ""),
      started_at: row.started_at == null ? undefined : Number(row.started_at),
      finished_at: row.finished_at == null ? undefined : Number(row.finished_at),
    };
  });
}

export function setAutomationContext(next: RuntimeContext) {
  store.setContext(next);
}

const refreshOnConnection = createReconnectRefresh(refreshAutomations);
export function setAutomationConnection(status: string) {
  refreshOnConnection(status);
  pushWireStatus("automations", status, connected => patch({connected}));
}

export function ingestAutomations(message: Record<string, unknown>) {
  if (message.type === "automation:accepted") {
    const {pendingSave, editorGeneration} = store.getState();
    const requestId = String(message.request_id || "");
    if (pendingSave && requestId === pendingSave.requestId && pendingSave.generation === editorGeneration) {
      store.getContext()?.notify(pendingSave.label, "utility:automations");
      closeAutomationBuilder();
    }
    return;
  }
  if (message.type === "automation:error") {
    const {pendingSave, editorGeneration} = store.getState();
    const requestId = String(message.request_id || "");
    if (pendingSave && requestId === pendingSave.requestId && pendingSave.generation === editorGeneration) {
      const detail = typeof message.error === "string" ? message.error : typeof message.message === "string" ? message.message : "Automation was not saved";
      patch({pendingSave: null, saveError: detail});
      store.getContext()?.notify(detail, "utility:automations");
    }
    return;
  }
  if (message.type === "automations") {
    const items = parseItems(message.items);
    patch({items, connected: true});
    return;
  }
  if (message.type === "automations:history") {
    const history = parseHistory(message.items);
    patch({
      history,
      historyCount: Number(message.count ?? history.length),
      connected: true,
    });
  }
}

export function refreshAutomations() {
  send({type: "automation:list"});
  send({type: "automations:history", limit: 100});
}

export function setAutomationFilter(filter: AutomationFilter) {
  patch({filter});
}

export function openAutomationBuilder(item: AutomationItem | null = null) {
  patch({builderOpen: true, editing: item, editorGeneration: store.getState().editorGeneration + 1, pendingSave: null, saveError: ""});
}

export function closeAutomationBuilder() {
  patch({builderOpen: false, editing: null, editorGeneration: store.getState().editorGeneration + 1, pendingSave: null, saveError: ""});
}

export function saveAutomation(fields: {
  name: string;
  prompt: string;
  trigger: AutomationTrigger;
  misfire_policy: "latest" | "skip";
}) {
  const state = store.getState();
  if (!state.builderOpen || state.pendingSave) return false;
  const name = fields.name.trim();
  const prompt = fields.prompt.trim();
  if (!name || !prompt) {
    patch({saveError: "Add both a name and an instruction"});
    return false;
  }
  const payload = {
    name,
    prompt,
    trigger: fields.trigger,
    misfire_policy: fields.misfire_policy,
  };
  const requestId = `automation-save-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  patch({pendingSave: {requestId, generation: state.editorGeneration, label: state.editing ? "Automation updated" : "Automation created"}, saveError: ""});
  let sent = false;
  if (state.editing) {
    sent = send({type: "automation:update", request_id: requestId, id: state.editing.id, ...payload});
  } else {
    sent = send({type: "automation:add", request_id: requestId, enabled: true, ...payload});
  }
  if (!sent) {
    if (store.getState().pendingSave?.requestId === requestId) patch({pendingSave: null, saveError: "Backend offline — automation was not saved"});
    return false;
  }
  return true;
}

export function runAutomation(id: string) {
  if (send({type: "automation:run", id})) {
    store.getContext()?.notify("Automation queued", "utility:automations");
  }
}

export function toggleAutomation(id: string, enabled: boolean) {
  send({type: "automation:update", id, enabled});
}

export function removeAutomation(id: string, name: string) {
  if (!window.confirm(`Delete automation “${name}”?`)) return;
  send({type: "automation:remove", id});
}

export function automationKind(item: AutomationItem): "scheduled" | "event" {
  return item.trigger.type === "webhook"
    ? "event"
    : "scheduled";
}

export function relativeAutomationTime(value: number | string | null | undefined): string {
  const timestamp = Number(value || 0);
  if (!timestamp) return "Never";
  const seconds = Math.max(0, Date.now() / 1000 - timestamp);
  if (seconds < 60) return "Now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  if (seconds < 604800) return `${Math.floor(seconds / 86400)}d ago`;
  return new Date(timestamp * 1000).toLocaleDateString();
}

export function useAutomationState() {
  return store.useStore();
}
