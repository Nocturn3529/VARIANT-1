import type { WsCommand } from "./protocol";
import { createModuleStore } from "./state/createModuleStore";
import type { AboutState, DoctorFinding, RuntimeContext } from "./types";

const store = createModuleStore<AboutState>({
  initialState: {
    connected: false,
    version: "",
    packaged: false,
    updateLabel: "Development build",
    updateChecking: false,
    updateCheckComplete: false,
    paths: {},
    health: {
      backend: "Waiting",
      backendDetail: "Local backend",
      model: "Waiting",
      modelDetail: "No model selected",
      memory: "Waiting",
      memoryDetail: "0 archival records",
      scheduler: "Waiting",
      schedulerDetail: "0 scheduled tasks",
    },
    findings: null,
  },
});

export function setAboutContext(next: RuntimeContext) {
  store.setContext(next);
}

export function setAboutConnection(status: string) {
  const open = status === "connected";
  const state = store.getState();
  store.setState({connected: open, health: {...state.health, backend: open ? "Connected" : "Offline",
    backendDetail: open ? "Authenticated local WebSocket" : "Backend connection unavailable"}});
}

export function sendAbout(payload: WsCommand) {
  return store.send(payload);
}

export function notifyAbout(message: string) {
  store.getContext()?.notify(message);
}

function applyHealthFromConfig(message: Record<string, unknown>) {
  const state = store.getState();
  const type = String(message.type || "");
  const work = (message.work || {}) as Record<string, unknown>;
  const modelReady = message.model_ready != null
    ? !!message.model_ready
    : type === "hello"
      ? !!message.engine_ready
      : message.ready != null
      ? !!message.ready
      : state.health.model === "Ready";
  const model = String(
    message.mode === "cloud"
      ? message.cloud_model || message.model || "Cloud model"
      : message.model || state.health.modelDetail,
  );
  const memoryKnown = "memory" in message;
  const workKnown = message.work != null && typeof message.work === "object";
  store.setState({
    connected: true,
    health: {
      backend: "Connected",
      backendDetail: "Authenticated local WebSocket",
      model: modelReady ? "Ready" : "Not ready",
      modelDetail: model,
      memory: memoryKnown ? (message.memory ? "Healthy" : "Unavailable") : state.health.memory,
      memoryDetail: memoryKnown
        ? `${Number(message.memory_count || 0)} archival records`
        : state.health.memoryDetail,
      scheduler: workKnown
        ? (work.scheduler_running ? "Running" : "Stopped")
        : state.health.scheduler,
      schedulerDetail: workKnown
        ? `${Number(work.active_jobs || 0)} active jobs`
        : state.health.schedulerDetail,
    },
  });
}

export function ingestAbout(message: Record<string, unknown>) {
  const type = String(message.type || "");
  if (type === "config" || type === "engine" || type === "hello") {
    applyHealthFromConfig(message);
    return;
  }
  if (type === "doctor:result") {
    const findings = Array.isArray(message.findings)
      ? (message.findings as DoctorFinding[])
      : [];
    store.setState({findings});
    notifyAbout(message.summary === "ok" ? "Health check passed" : "Health check complete");
  }
}

export async function loadAboutInfo() {
  try {
    const info = await store.getContext()?.api?.getAppInfo?.();
    if (!info) return;
    const current = store.getState();
    store.setState({
      version: String(info.version || ""),
      packaged: !!info.packaged,
      updateLabel: current.updateChecking || current.updateCheckComplete
        ? current.updateLabel
        : info.packaged ? "Installed build" : "Development build",
      paths: {...(info.paths || {})},
    });
  } catch {
    /* ignore */
  }
}

export function refreshAbout() {
  sendAbout({type: "config:get"});
  void loadAboutInfo();
}

export async function checkForUpdates() {
  store.setState({updateChecking: true, updateLabel: "Checking…"});
  try {
    const result = await store.getContext()?.api?.checkForUpdates?.();
    if (!result) {
      store.setState({updateChecking: false, updateCheckComplete: true,
        updateLabel: "Update check unavailable"});
      return;
    }
    let label = "Update check unavailable";
    if (result.ok) {
      label = result.available && result.version
        ? `Update ${result.version} available`
        : "Up to date";
    }
    else if (result.reason === "dev_mode") label = "Development build";
    else if (result.reason === "updater_unconfigured") label = "Update feed not configured";
    store.setState({updateLabel: label, updateChecking: false, updateCheckComplete: true});
  } catch {
    store.setState({updateLabel: "Update check unavailable", updateChecking: false,
      updateCheckComplete: true});
  }
}

export async function openAboutPath(key: string) {
  const result = await store.getContext()?.api?.openAppPath?.(key);
  if (!result?.ok) notifyAbout(result?.reason || "Could not open path");
}

export function runHealthCheck() {
  sendAbout({type: "doctor:run"});
  notifyAbout("Running health checks…");
}

export function useAboutState() {
  return store.useStore();
}
