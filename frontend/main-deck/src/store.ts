import type { WsCommand } from "./protocol";
import {createModuleStore} from "./state/createModuleStore";
import type {
  GatewayState,
  PlatformState,
  RuntimeContext,
  InstallJob,
} from "./types";
import {asRecord, createRequestIdFactory} from "./state/storePrimitives";
import {notifyToast} from "./state/toastStore";

const oauthRequestId = createRequestIdFactory("oauth-flow");
const credentialRequestId = createRequestIdFactory("credential-list");
const credentialRequests = new Map<string, {id: string; timer: ReturnType<typeof setTimeout>}>();
function forgetCredentialRequest(provider: string) {
  clearTimeout(credentialRequests.get(provider)?.timer); credentialRequests.delete(provider);
}
const store = createModuleStore<PlatformState>({
  initialState: {
    connected: false,
    config: {},
    tools: {},
    gateway: null,
    oauthFlow: {
      requestId: "",
      provider: "",
      phase: "idle",
      verificationUrl: "",
      userCode: "",
      error: "",
    },
    credentialReceipt: null,
    customEndpointReceipt: null,
  },
});

function installJob(value: unknown): InstallJob | null {
  if (!value || typeof value !== "object") return null;
  const row = value as Record<string, unknown>;
  if (typeof row.id !== "string" || !row.id || typeof row.runtime_id !== "string" || typeof row.status !== "string") return null;
  return {...row, id: row.id, runtime_id: row.runtime_id, status: row.status,
    operation: String(row.operation || "install"), target_id: String(row.target_id || ""),
    progress: Math.max(0, Math.min(100, Number(row.progress) || 0))} as InstallJob;
}
const installFinished = (job: InstallJob) => ["done", "error", "cancelled"].includes(job.status);

export function setContext(next: RuntimeContext) {
  store.setContext(next);
}

export function setPlatformConnection(status: string) {
  const open = status === "connected";
  if (store.getState().connected === open) return;
  store.setConnected(open);
  if (!open) {
    for (const provider of credentialRequests.keys()) forgetCredentialRequest(provider);
    store.setState({config: {...store.getState().config, credential_revision_by_provider: {}}});
  }
}

export function send(payload: WsCommand) {
  if (payload.type.startsWith("cloud:credential:") && payload.type !== "cloud:credential:list") forgetCredentialRequest(String(payload.provider || ""));
  return store.send(payload);
}

export function refreshProviderCredentials(provider: string) {
  if (!store.getState().connected || credentialRequests.has(provider)) return false;
  const id = credentialRequestId(provider);
  const timer = setTimeout(() => forgetCredentialRequest(provider), 15000);
  credentialRequests.set(provider, {id, timer});
  if (!send({type: "cloud:credential:list", provider, request_id: id})) {forgetCredentialRequest(provider); return false;}
  return true;
}

function mergeCredentialConfig(previous: PlatformState["config"], incoming: Record<string, unknown>): PlatformState["config"] {
  const config = {...previous, ...incoming, credentials_by_provider: {...previous.credentials_by_provider},
    credential_strategy_by_provider: {...previous.credential_strategy_by_provider}, credential_revision_by_provider: {...previous.credential_revision_by_provider}};
  const items = asRecord(incoming.credentials_by_provider), revisions = asRecord(incoming.credential_revision_by_provider), strategies = asRecord(incoming.credential_strategy_by_provider);
  const stale = new Set<string>();
  for (const [provider, rows] of Object.entries(items)) {
    if (!Array.isArray(rows)) continue;
    const revision = revisions[provider], before = previous.credential_revision_by_provider?.[provider];
    const versioned = typeof revision === "number" && Number.isSafeInteger(revision) && revision >= 0;
    if (before !== undefined && (!versioned || revision < before)) {stale.add(provider); continue;}
    config.credentials_by_provider[provider] = rows;
    if (typeof strategies[provider] === "string") config.credential_strategy_by_provider[provider] = strategies[provider];
    if (versioned) config.credential_revision_by_provider[provider] = revision;
  }
  if (config.providers && stale.size) config.providers = config.providers.map(provider => {
    const prior = previous.providers?.find(item => item.name === provider.name);
    return prior && stale.has(provider.name) ? {...provider, api_key_configured: prior.api_key_configured, credential_count: prior.credential_count} : provider;
  });
  return config;
}

export function setMessagingGatewayEnabled(enabled: boolean) {
  const previous = store.getState().gateway;
  if (previous) store.setState({gateway: {...previous, enabled}});
  if (send({type: "messaging:set", enabled})) return true;
  if (previous) store.setState({gateway: previous});
  notifyToast("Could not update messaging gateway — backend offline");
  return false;
}

export function setMessagingAdapterEnabled(adapterId: string, enabled: boolean) {
  const previous = store.getState().gateway;
  const adapter = previous?.adapters.find(item => item.id === adapterId);
  if (!previous || !adapter) return false;
  store.setState({gateway: {
    ...previous,
    adapters: previous.adapters.map(item => item.id === adapterId
      ? {...item, config: {...item.config, enabled}}
      : item),
  }});
  if (send({type: "messaging:set", adapter: adapterId, config: {enabled}})) return true;
  store.setState({gateway: previous});
  notifyToast(`Could not update ${adapter.display_name} — backend offline`);
  return false;
}

export function clearCustomEndpointFeedback() {
  store.setState({customEndpointReceipt: null});
}

export function beginOAuthFlow(provider: string) {
  const requestId = oauthRequestId(provider);
  store.setState({oauthFlow: {requestId, provider, phase: "starting", verificationUrl: "", userCode: "", error: ""}});
  const accepted = send({type: "cloud:oauth:start", provider, request_id: requestId, open_browser: true});
  if (!accepted) store.setState({oauthFlow: {...store.getState().oauthFlow, phase: "error", error: "VARIANT-1 is offline."}});
  return accepted;
}

export function dismissOAuthFlow() {
  store.setState({oauthFlow: {requestId: "", provider: "", phase: "idle", verificationUrl: "", userCode: "", error: ""}});
}

export function cancelOAuthFlow() {
  const flow = store.getState().oauthFlow;
  if (flow.phase === "starting" || flow.phase === "pending") {
    send({type: "cloud:oauth:cancel", provider: flow.provider, request_id: flow.requestId});
  }
  dismissOAuthFlow();
}

function mergeOAuthStatus(
  config: PlatformState["config"],
  provider: string,
  status: unknown,
  connected: boolean,
): PlatformState["config"] {
  const detail = status && typeof status === "object"
    ? {...status as Record<string, unknown>, connected}
    : {connected};
  const oauth = {...(config.oauth || {})};
  if (provider === "xai") {
    oauth.xai = connected;
    oauth.xai_detail = detail;
  } else if (provider === "openai-codex") {
    oauth.openai_codex = connected;
    oauth.openai_codex_detail = detail;
  }
  return {...config, oauth, oauth_by_provider: {...config.oauth_by_provider, [provider]: detail}};
}

export function ingest(message: Record<string, unknown>) {
  const type = String(message.type || "");
  const state = store.getState();
  if (type === "config" || type === "engine" || type === "hello") {
    const config = mergeCredentialConfig(state.config, message);
    const gateway = (message.messaging_gateway as GatewayState | undefined) || state.gateway;
    store.setState({
      config,
      gateway,
      connected: true,
    });
  } else if (type === "tools") {
    store.setState({tools: message});
  } else if (type === "messaging:gateway") {
    const gateway = message as unknown as GatewayState;
    store.setState({gateway});
    if (gateway.credential_error) notifyToast(gateway.credential_error);
    if (gateway.lifecycle_error) notifyToast(gateway.lifecycle_error);
  } else if (type === "messaging:error") {
    notifyToast(String(message.error || "Messaging operation failed"));
  } else if (type === "cloud:usage") {
    const usage = (message.cloud_usage || message) as PlatformState["config"]["cloud_usage"];
    store.setState({config: {...state.config, cloud_usage: usage}});
  } else if (type === "cloud:oauth:disconnected") {
    store.setState({config: mergeOAuthStatus(state.config, String(message.provider || ""), message.status, false)});
  } else if (["cloud:oauth:pending", "cloud:oauth:complete", "cloud:oauth:error", "cloud:oauth:busy", "cloud:oauth:cancelled"].includes(type)) {
    const flow = state.oauthFlow;
    if (flow.phase === "idle" || message.provider !== flow.provider || !flow.requestId || message.request_id !== flow.requestId) return;
    if (type === "cloud:oauth:cancelled") {
      if (message.cancelled === true) dismissOAuthFlow();
    } else if (type === "cloud:oauth:pending") {
      if (flow.phase !== "starting" && flow.phase !== "pending") return;
      store.setState({oauthFlow: {...flow, phase: "pending", verificationUrl: String(message.verification_url || ""), userCode: String(message.user_code || ""), error: ""}});
    } else if (type === "cloud:oauth:complete") {
      store.setState({config: mergeOAuthStatus(state.config, flow.provider, message.status, true),
        oauthFlow: {...flow, phase: "complete", verificationUrl: "", userCode: "", error: ""}});
    } else {
      if (flow.phase === "complete") return;
      store.setState({oauthFlow: {...flow, phase: "error", verificationUrl: "", userCode: "",
        error: String(message.error || (type === "cloud:oauth:busy" ? "A sign-in attempt is already running" : "Sign-in failed"))}});
    }
  } else if (type === "cloud:credential:items") {
    const provider = String(message.provider || "");
    if (!state.connected || !provider || !Array.isArray(message.items)) return;
    if (message.request_id) {
      if (credentialRequests.get(provider)?.id !== message.request_id) return;
      forgetCredentialRequest(provider);
    }
    store.setState({config: mergeCredentialConfig(state.config, {
      credentials_by_provider: {[provider]: message.items}, credential_strategy_by_provider: {[provider]: String(message.strategy || "priority")},
      credential_revision_by_provider: {[provider]: message.revision}})});
  } else if (type === "cloud:credential:accepted" || type === "cloud:credential:rejected") {
    store.setState({credentialReceipt: {
      requestId: String(message.request_id || ""),
      provider: String(message.provider || ""),
      operation: String(message.operation || "set") as NonNullable<PlatformState["credentialReceipt"]>["operation"],
      accepted: type === "cloud:credential:accepted",
      error: String(message.error || ""),
    }});
  } else if (type === "cloud:custom-endpoints") {
    const items = Array.isArray(message.items) ? message.items : [];
    store.setState({
      config: {
        ...state.config,
        custom_endpoints: items as PlatformState["config"]["custom_endpoints"],
      },
    });
  } else if (type === "cloud:custom-endpoint:validated") {
    store.setState({customEndpointReceipt: {
      requestId: String(message.request_id || ""),
      operation: "validate",
      accepted: true,
      validation: message as unknown as NonNullable<PlatformState["customEndpointReceipt"]>["validation"],
      endpoint: null,
      id: "",
      removed: false,
      fallbackMode: "",
      error: "",
    }});
  } else if (type === "cloud:custom-endpoint:error") {
    const operation = String(message.operation || "save");
    store.setState({customEndpointReceipt: {
      requestId: String(message.request_id || ""),
      operation: (["validate", "save", "activate", "remove"].includes(operation)
        ? operation : "save") as NonNullable<PlatformState["customEndpointReceipt"]>["operation"],
      accepted: false,
      validation: null,
      endpoint: null,
      id: String(message.id || ""),
      removed: false,
      fallbackMode: "",
      error: String(message.error || "Custom endpoint operation failed"),
    }});
  } else if (
    type === "cloud:custom-endpoint:saved"
    || type === "cloud:custom-endpoint:activated"
  ) {
    const endpoint = message.endpoint && typeof message.endpoint === "object"
      ? message.endpoint as NonNullable<PlatformState["customEndpointReceipt"]>["endpoint"]
      : null;
    store.setState({customEndpointReceipt: {
      requestId: String(message.request_id || ""),
      operation: type === "cloud:custom-endpoint:saved" ? "save" : "activate",
      accepted: true,
      validation: null,
      endpoint,
      id: String(endpoint?.id || ""),
      removed: false,
      fallbackMode: "",
      error: "",
    }});
    send({type: "cloud:custom-endpoints:list"});
  } else if (type === "cloud:custom-endpoint:removed") {
    store.setState({customEndpointReceipt: {
      requestId: String(message.request_id || ""),
      operation: "remove",
      accepted: true,
      validation: null,
      endpoint: null,
      id: String(message.id || ""),
      removed: !!message.removed,
      fallbackMode: String(message.fallback_mode || ""),
      error: "",
    }});
    send({type: "cloud:custom-endpoints:list"});
  } else if (type === "inference:platform") {
    store.setState({config: {...state.config, inference_platform: message}});
  } else if (type === "inference:install:job") {
    const job = installJob(message);
    if (!job) return;
    const platform = state.config.inference_platform || {};
    const jobs = (platform.install_jobs || []) as InstallJob[];
    const previous = jobs.find(row => row.id === job.id);
    store.setState({config: {...state.config, inference_platform: {...platform,
      install_jobs: [job, ...jobs.filter(row => row.id !== job.id)].slice(0, 30)}}});
    // Progress paints from the event; only a terminal transition needs fresh runtime facts.
    if (installFinished(job) && previous?.status !== job.status) send({type: "inference:platform:get"});
  } else if (type === "inference:install:jobs") {
    const jobs = (Array.isArray(message.items) ? message.items : []).map(installJob).filter((job): job is InstallJob => !!job).slice(0, 30);
    store.setState({config: {...state.config, inference_platform: {...state.config.inference_platform, install_jobs: jobs}}});
  } else if (type === "inference:install:cancelled") {
    // The acknowledgement has no job identity. Its following platform snapshot is authoritative.
    return;
  }
}

export function refresh() {
  send({type: "config:get"});
  send({type: "tools:get"});
  send({type: "messaging:get"});
  send({type: "cloud:usage"});
  send({type: "cloud:custom-endpoints:list"});
  send({type: "inference:platform:get"});
}

export function usePlatformState() {
  return store.useStore();
}
export const getPlatformState = store.getState;
