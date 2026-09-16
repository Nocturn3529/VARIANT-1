/** Overview destination store for live dashboards, native sections, and active-view polling. */
import type { WsCommand } from "./protocol";
import {createModuleStore} from "./state/createModuleStore";
import type { RuntimeContext } from "./types";

export type OverviewTelemetry = {
  inference: Record<string, unknown> | null;
  hardware: Record<string, unknown> | null;
  cloudUsage: Record<string, unknown> | null;
  modelUsage: Record<string, unknown> | null;
  modelRequests: ModelRequestReceipt[];
  modelRequestWindow: {
    droppedEvents: number;
    publishFailures: number;
  };
  active: boolean;
};

export type ModelRequestReceipt = {
  manifestId: string;
  schema: string;
  logicalCallId: string;
  attempt: number;
  capturedAt: number;
  route: {
    provider: string;
    model: string;
    physicalMode: string;
    selectedMode: string;
    apiStyle: string;
  };
  counts: {
    sourceMessages: number | null;
    renderedMessages: number | null;
    requestedTools: number | null;
    renderedTools: number | null;
    requestedImages: number | null;
    renderedImages: number | null;
  };
  budget: {
    estimatedInputTokens: number | null;
    contextLimitTokens: number | null;
    outputReserveTokens: number | null;
    remainingMarginTokens: number | null;
    overBudget: boolean | null;
  };
  contextLineage: {
    consideredCount: number | null;
    selectedCount: number | null;
    droppedCount: number | null;
    selections: Array<{
      kind: string;
      source: string;
      trust: string;
      consideredCount: number | null;
      selectedCount: number | null;
      droppedCount: number | null;
    }>;
    items: Array<{
      kind: string;
      source: string;
      trust: string;
      decision: string;
    }>;
    transforms: Array<{
      kind: string;
      inputCount: number | null;
      outputCount: number | null;
      affectedCount: number | null;
      charsBefore: number | null;
      charsAfter: number | null;
    }>;
    transformKinds: string[];
  } | null;
  provenance: {
    contextReceiptAvailable: boolean;
    selectionAvailable: boolean;
    observationProjectionAvailable: boolean;
    supersessionAvailable: boolean;
    compressionAvailable: boolean;
    usageAvailable: boolean;
  };
  projectionTransformKinds: string[];
  usage: {
    linked: boolean;
    providerReported: boolean;
    estimated: boolean;
    measurement: string;
    status: string;
    promptTokens: number | null;
    completionTokens: number | null;
    totalTokens: number | null;
    cachedTokens: number | null;
    reasoningTokens: number | null;
    cacheWriteTokens: number | null;
    toolPromptTokens: number | null;
    costUsd: number | null;
  } | null;
  privacy: {
    status: "metadata_only" | "retained_data" | "legacy_unknown";
    policy: string;
    exactPayloadReference: boolean;
  };
};

type UnknownRecord = Record<string, unknown>;

export const MODEL_REQUEST_RECEIPT_LIMIT = 32;

function asRecord(value: unknown): UnknownRecord | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as UnknownRecord
    : null;
}

function firstRecord(...values: unknown[]): UnknownRecord | null {
  for (const value of values) {
    const record = asRecord(value);
    if (record) return record;
  }
  return null;
}

function finiteNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

function nonNegativeNumber(value: unknown): number | null {
  const parsed = finiteNumber(value);
  return parsed === null ? null : Math.max(0, parsed);
}

function firstNumber(record: UnknownRecord | null, keys: string[]): number | null {
  if (!record) return null;
  for (const key of keys) {
    const parsed = finiteNumber(record[key]);
    if (parsed !== null) return parsed;
  }
  return null;
}

function firstNonNegativeNumber(
  record: UnknownRecord | null,
  keys: string[],
): number | null {
  const parsed = firstNumber(record, keys);
  return parsed === null ? null : Math.max(0, parsed);
}

function firstString(record: UnknownRecord | null, keys: string[]): string {
  if (!record) return "";
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) {
      return value.trim().slice(0, 300);
    }
  }
  return "";
}

function firstBoolean(record: UnknownRecord | null, keys: string[]): boolean | null {
  if (!record) return null;
  for (const key of keys) {
    if (typeof record[key] === "boolean") return record[key] as boolean;
  }
  return null;
}

function firstArray(...values: unknown[]): unknown[] | null {
  for (const value of values) {
    if (Array.isArray(value)) return value;
  }
  return null;
}

function countFrom(
  record: UnknownRecord | null,
  numberKeys: string[],
  arrayKeys: string[] = [],
): number | null {
  const direct = firstNonNegativeNumber(record, numberKeys);
  if (direct !== null) return direct;
  if (!record) return null;
  for (const key of arrayKeys) {
    if (Array.isArray(record[key])) return (record[key] as unknown[]).length;
  }
  return null;
}

function safeKind(value: unknown): string {
  if (typeof value !== "string") return "";
  const normalized = value.trim().toLowerCase().replace(/[^a-z0-9_.:-]+/g, "_");
  return normalized.slice(0, 80);
}

function transformKinds(...values: unknown[]): string[] {
  const kinds: string[] = [];
  const add = (value: unknown, fallback = "") => {
    const record = asRecord(value);
    const kind = record
      ? safeKind(record.kind ?? record.type ?? record.operation)
      : safeKind(value);
    const next = kind || fallback;
    if (next && !kinds.includes(next)) kinds.push(next);
  };

  for (const value of values) {
    if (Array.isArray(value)) {
      for (const item of value) add(item);
    } else if (value !== undefined && value !== null) {
      add(value);
    }
  }
  return kinds.slice(0, 12);
}

function decisionCounts(
  selection: UnknownRecord | null,
  lineage: UnknownRecord | null,
): {considered: number | null; selected: number | null; dropped: number | null} {
  let considered = countFrom(
    selection,
    ["considered", "considered_count", "candidate_count", "available_count", "input_count", "total_count"],
    ["candidates", "considered_items"],
  );
  let selected = countFrom(
    selection,
    ["kept", "selected", "selected_count", "kept_count", "included_count"],
    ["selected", "selected_items", "kept", "kept_items"],
  );
  let dropped = countFrom(
    selection,
    ["dropped", "dropped_count", "excluded_count", "omitted_count"],
    ["dropped", "dropped_items", "excluded", "excluded_items"],
  );

  const decisions = firstArray(
    selection?.decisions,
    selection?.items,
    lineage?.decisions,
    lineage?.items,
  );
  if (decisions) {
    let selectedFromItems = 0;
    let droppedFromItems = 0;
    let classified = 0;
    for (const item of decisions) {
      const record = asRecord(item);
      const decision = safeKind(
        record?.decision ?? record?.disposition ?? record?.status,
      );
      if (["selected", "keep", "kept", "include", "included"].includes(decision)) {
        selectedFromItems += 1;
        classified += 1;
      } else if (["dropped", "drop", "excluded", "omit", "omitted"].includes(decision)) {
        droppedFromItems += 1;
        classified += 1;
      }
    }
    if (considered === null) considered = decisions.length;
    if (selected === null && classified) selected = selectedFromItems;
    if (dropped === null && classified) dropped = droppedFromItems;
  }

  if (considered === null && selected !== null && dropped !== null) {
    considered = selected + dropped;
  }
  return {considered, selected, dropped};
}

function normalizeContextLineage(raw: UnknownRecord): ModelRequestReceipt["contextLineage"] {
  const provenance = asRecord(raw.provenance);
  const lineageEnvelope = firstRecord(
    raw.context_lineage,
    raw.contextLineage,
    raw.lineage,
    provenance?.context_lineage,
    provenance?.contextLineage,
  );
  const lineage = firstRecord(
    lineageEnvelope?.context,
    lineageEnvelope?.context_selection,
    lineageEnvelope,
    raw.context_selection,
  );
  if (!lineage) return null;

  const selection = firstRecord(
    lineage.selection,
    lineage.context_selection,
    lineage.selection_summary,
    lineage,
  );
  const counts = decisionCounts(selection, lineage);
  const selections = (firstArray(lineage.selections) || [])
    .map(value => {
      const record = asRecord(value);
      if (!record) return null;
      const itemCounts = decisionCounts(record, null);
      return {
        kind: safeKind(record.kind),
        source: safeKind(record.source),
        trust: safeKind(record.trust),
        consideredCount: itemCounts.considered,
        selectedCount: itemCounts.selected,
        droppedCount: itemCounts.dropped,
      };
    })
    .filter((value): value is NonNullable<typeof value> => value !== null)
    .slice(0, 8);
  const items = (firstArray(lineage.items) || [])
    .map(value => {
      const record = asRecord(value);
      if (!record) return null;
      const kind = safeKind(record.kind);
      if (!kind) return null;
      return {
        kind,
        source: safeKind(record.source),
        trust: safeKind(record.trust),
        decision: safeKind(record.decision),
      };
    })
    .filter((value): value is NonNullable<typeof value> => value !== null)
    .slice(0, 12);
  const transforms = (firstArray(lineage.transforms, lineage.transformations) || [])
    .map(value => {
      const record = asRecord(value);
      if (!record) return null;
      const kind = safeKind(record.kind ?? record.type ?? record.operation);
      if (!kind) return null;
      return {
        kind,
        inputCount: firstNonNegativeNumber(record, ["input_count"]),
        outputCount: firstNonNegativeNumber(record, ["output_count"]),
        affectedCount: firstNonNegativeNumber(record, ["affected_count"]),
        charsBefore: firstNonNegativeNumber(record, ["chars_before"]),
        charsAfter: firstNonNegativeNumber(record, ["chars_after"]),
      };
    })
    .filter((value): value is NonNullable<typeof value> => value !== null)
    .slice(0, 12);
  const kinds = transformKinds(
    lineage.transforms,
    lineage.transformations,
    lineage.projections,
  );
  if (lineage.compression) {
    const compression = transformKinds(lineage.compression);
    kinds.push(...compression.length ? compression : ["compression"]);
  }
  if (lineage.supersession) {
    const supersession = transformKinds(lineage.supersession);
    kinds.push(...supersession.length ? supersession : ["supersession"]);
  }
  const uniqueKinds = [...new Set(kinds)].slice(0, 12);
  const hasCounts = counts.considered !== null
    || counts.selected !== null
    || counts.dropped !== null;
  return hasCounts || selections.length || items.length || uniqueKinds.length
    ? {
        consideredCount: counts.considered,
        selectedCount: counts.selected,
        droppedCount: counts.dropped,
        selections,
        items,
        transforms,
        transformKinds: uniqueKinds,
      }
    : null;
}

function normalizeProvenance(raw: UnknownRecord): ModelRequestReceipt["provenance"] {
  const provenance = asRecord(raw.provenance) || {};
  const usage = asRecord(raw.usage);
  return {
    contextReceiptAvailable: firstBoolean(provenance, ["context_receipt_available"]) ?? false,
    selectionAvailable: firstBoolean(provenance, ["selection_available"]) ?? false,
    observationProjectionAvailable:
      firstBoolean(provenance, ["observation_projection_available"]) ?? false,
    supersessionAvailable: firstBoolean(provenance, ["supersession_available"]) ?? false,
    compressionAvailable:
      firstBoolean(provenance, ["compression_receipt_available", "compression_available"])
      ?? false,
    usageAvailable: firstBoolean(provenance, ["usage_available"])
      ?? (usage !== null),
  };
}

function normalizeUsage(raw: UnknownRecord): ModelRequestReceipt["usage"] {
  const completion = asRecord(raw.completion);
  const lineage = firstRecord(raw.lineage, raw.context_lineage);
  const envelope = firstRecord(
    raw.provider_usage,
    raw.provider_reported_usage,
    raw.usage,
    completion?.usage,
    lineage?.usage,
  );
  if (!envelope) return null;
  const usage = firstRecord(
    envelope.provider_reported,
    envelope.reported,
    envelope.tokens,
    envelope,
  );
  if (!usage) return null;
  const details = firstRecord(
    usage.prompt_tokens_details,
    usage.input_tokens_details,
  );
  const explicitlyLinked = firstBoolean(envelope, ["linked", "available", "reported"])
    ?? firstBoolean(usage, ["linked", "available", "reported"]);
  const measurement = (firstString(envelope, ["measurement"]) || firstString(usage, ["measurement"])).toLowerCase();
  const estimated = measurement === "estimated" || measurement.startsWith("estimated_")
    || (firstBoolean(envelope, ["estimated"]) ?? firstBoolean(usage, ["estimated"])) === true;
  const providerReported = firstBoolean(envelope, ["provider_reported"])
    ?? firstBoolean(usage, ["provider_reported"])
    ?? (measurement === "provider_reported");
  const promptTokens = firstNonNegativeNumber(
    usage,
    ["prompt_tokens", "input_tokens", "promptTokenCount"],
  );
  const completionTokens = firstNonNegativeNumber(
    usage,
    ["completion_tokens", "output_tokens", "candidatesTokenCount"],
  );
  const totalTokens = firstNonNegativeNumber(
    usage,
    ["total_tokens", "totalTokenCount"],
  );
  const cachedTokens = firstNonNegativeNumber(
    usage,
    ["cached_tokens", "cached_prompt_tokens", "cached_input_tokens"],
  ) ?? firstNonNegativeNumber(details, ["cached_tokens"]);
  const reasoningTokens = firstNonNegativeNumber(
    usage,
    ["reasoning_tokens", "thoughts_tokens", "thoughtsTokenCount"],
  );
  const cacheWriteTokens = firstNonNegativeNumber(
    usage,
    ["cache_write_input_tokens", "cache_creation_input_tokens"],
  );
  const toolPromptTokens = firstNonNegativeNumber(
    usage,
    ["tool_prompt_tokens"],
  );
  const costUsd = nonNegativeNumber(
    usage.cost_usd ?? usage.estimated_cost_usd ?? envelope.cost_usd,
  );

  return {
    linked: explicitlyLinked ?? true,
    providerReported,
    estimated,
    measurement,
    status: firstString(envelope, ["status", "state", "measurement"])
      || firstString(usage, ["status", "state"]),
    promptTokens,
    completionTokens,
    totalTokens: totalTokens
      ?? (promptTokens !== null && completionTokens !== null
        ? promptTokens + completionTokens
        : null),
    cachedTokens,
    reasoningTokens,
    cacheWriteTokens,
    toolPromptTokens,
    costUsd,
  };
}

function normalizePrivacy(raw: UnknownRecord): ModelRequestReceipt["privacy"] {
  const privacy = asRecord(raw.privacy);
  if (!privacy) {
    return {status: "legacy_unknown", policy: "", exactPayloadReference: false};
  }
  const sensitiveFlags = [
    "prompt_text_stored",
    "tool_values_stored",
    "image_data_stored",
    "headers_stored",
    "url_stored",
  ];
  const knownFlags = sensitiveFlags.filter(key => typeof privacy[key] === "boolean");
  const retained = sensitiveFlags.some(key => privacy[key] === true);
  const exactPayloadReference = privacy.exact_payload_ref !== null
    && privacy.exact_payload_ref !== undefined
    && privacy.exact_payload_ref !== false
    || privacy.exact_payload_reference === true;
  return {
    status: retained || exactPayloadReference
      ? "retained_data"
      : knownFlags.length
        ? "metadata_only"
        : "legacy_unknown",
    policy: firstString(privacy, ["policy"]),
    exactPayloadReference,
  };
}

/**
 * Reduce an inbound receipt to a strict metadata allowlist. Raw prompt text,
 * tool arguments/results, ordered message bodies, image bytes, and arbitrary
 * future fields never enter Overview state.
 */
export function normalizeModelRequestManifest(value: unknown): ModelRequestReceipt | null {
  const raw = asRecord(value);
  if (!raw) return null;
  const manifestId = firstString(raw, ["manifest_id", "manifestId", "id"]);
  if (!manifestId) return null;

  const route = asRecord(raw.route);
  const messages = asRecord(raw.messages);
  const sourceMessages = firstRecord(messages?.source, raw.source_messages);
  const renderedMessages = firstRecord(messages?.rendered, raw.rendered_messages);
  const tools = asRecord(raw.tools);
  const images = asRecord(raw.images);
  const budget = asRecord(raw.budget);

  return {
    manifestId,
    schema: firstString(raw, ["schema", "schema_version"]),
    logicalCallId: firstString(raw, ["logical_call_id", "logicalCallId"]),
    attempt: Math.max(0, Math.trunc(firstNumber(raw, ["attempt"]) ?? 0)),
    capturedAt: Math.max(0, firstNumber(raw, ["captured_at", "capturedAt"]) ?? 0),
    route: {
      provider: firstString(route, ["provider"]) || "unknown",
      model: firstString(route, ["model"]) || "unknown model",
      physicalMode: firstString(route, ["physical_mode", "physicalMode"]),
      selectedMode: firstString(route, ["selected_mode", "selectedMode"]),
      apiStyle: firstString(route, ["api_style", "apiStyle", "transport"]),
    },
    counts: {
      sourceMessages: countFrom(sourceMessages, ["count", "message_count"]),
      renderedMessages: countFrom(renderedMessages, ["count", "message_count"]),
      requestedTools: countFrom(tools, ["requested_count", "source_count"]),
      renderedTools: countFrom(tools, ["rendered_count"]),
      requestedImages: countFrom(images, ["requested_count", "source_count"]),
      renderedImages: countFrom(images, ["rendered_count"]),
    },
    budget: {
      estimatedInputTokens: firstNonNegativeNumber(
        budget,
        ["estimated_input_tokens_lower_bound", "estimated_input_tokens"],
      ),
      contextLimitTokens: firstNonNegativeNumber(budget, ["context_limit_tokens"]),
      outputReserveTokens: firstNonNegativeNumber(budget, ["output_reserve_tokens"]),
      remainingMarginTokens: firstNumber(
        budget,
        ["remaining_margin_tokens", "margin_tokens"],
      ),
      overBudget: firstBoolean(budget, ["over_budget"]),
    },
    contextLineage: normalizeContextLineage(raw),
    provenance: normalizeProvenance(raw),
    projectionTransformKinds: transformKinds(raw.transforms, raw.projection_transforms),
    usage: normalizeUsage(raw),
    privacy: normalizePrivacy(raw),
  };
}

const store = createModuleStore<OverviewTelemetry>({
  initialState: {
    inference: null,
    hardware: null,
    cloudUsage: null,
    modelUsage: null,
    modelRequests: [],
    modelRequestWindow: {
      droppedEvents: 0,
      publishFailures: 0,
    },
    active: false,
  },
});
let inferenceTimer: ReturnType<typeof setInterval> | null = null;
let hardwareTimer: ReturnType<typeof setInterval> | null = null;
let lastOfflineNotify = 0;
let overviewView = "chat";
let overviewDetached = false;

export function setOverviewDetached(detached: boolean) {
  if (overviewDetached === detached) return;
  overviewDetached = detached;
  enterOverview(overviewView);
}

function clearTimers() {
  if (inferenceTimer) {
    clearInterval(inferenceTimer);
    inferenceTimer = null;
  }
  if (hardwareTimer) {
    clearInterval(hardwareTimer);
    hardwareTimer = null;
  }
}

/** Quiet while offline — hardware polls every 1s and must not toast-spam. */
function send(payload: WsCommand, {notify = false} = {}): boolean {
  if (store.send(payload)) return true;
  const context = store.getContext();
  if (notify && store.getState().active && context) {
    const now = Date.now();
    if (now - lastOfflineNotify > 8000) {
      lastOfflineNotify = now;
      context.notify("VARIANT-1 backend is reconnecting", "utility:overview");
    }
  }
  return false;
}

export function requestTelemetry({notify = false} = {}) {
  send({type: "inference:telemetry"}, {notify});
  send({type: "hardware:telemetry"}, {notify});
  send({type: "cloud:usage"}, {notify});
  send({type: "model:usage"}, {notify});
  send({type: "model:request_manifests"}, {notify});
}

export function setOverviewContext(next: RuntimeContext) {
  store.setContext(next);
}

/** Re-request live snapshots when the WebSocket comes back online. */
export function setOverviewConnection(status: string) {
  if (status === "connected" && store.getState().active) {
    requestTelemetry({notify: false});
  }
}

/**
 * Start / stop polling when the primary view changes.
 * Also safe to re-call on WebSocket reconnect while Overview is open.
 */
export function enterOverview(view: string) {
  overviewView = view;
  clearTimers();
  const active = view === "overview" || overviewDetached;
  const telemetry = store.getState();
  if (telemetry.active !== active) {
    store.setState({active});
  }
  if (!active) return;

  // Notify once if the socket is not up yet; polls stay quiet until open.
  requestTelemetry({notify: true});
  // Keep rolling request rate and engine-ready state current even while no
  // model tokens are being emitted.
  inferenceTimer = setInterval(() => {
    send({type: "inference:telemetry"});
    send({type: "cloud:usage"});
    send({type: "model:usage"});
  }, 5000);
  hardwareTimer = setInterval(() => send({type: "hardware:telemetry"}), 1000);
}

export function ingestOverview(message: Record<string, unknown>) {
  const type = String(message.type || "");
  const telemetry = store.getState();

  if (type === "engine" && telemetry.active) {
    send({type: "inference:telemetry"});
    return;
  }

  if (type === "hardware:telemetry") {
    store.setState({hardware: message});
    return;
  }
  if (type === "cloud:usage") {
    store.setState({cloudUsage: message});
    return;
  }
  if (type === "model:usage") {
    store.setState({modelUsage: message});
    return;
  }
  if (type === "model:request_manifest") {
    const receipt = normalizeModelRequestManifest(message);
    if (!receipt) return;
    store.setState({
      modelRequests: mergeModelRequestReceipts(telemetry.modelRequests, [receipt]),
    });
    return;
  }
  if (type === "model:request_manifests") {
    const incoming = Array.isArray(message.items)
      ? message.items
        .map(normalizeModelRequestManifest)
        .filter((item): item is ModelRequestReceipt => item !== null)
      : [];
    store.setState({
      modelRequests: mergeModelRequestReceipts(telemetry.modelRequests, incoming),
      modelRequestWindow: {
        droppedEvents: Math.max(
          0,
          Math.trunc(finiteNumber(message.dropped_events) ?? 0),
        ),
        publishFailures: Math.max(
          0,
          Math.trunc(finiteNumber(message.publish_failures) ?? 0),
        ),
      },
    });
    return;
  }
  if (type === "inference:telemetry") {
    store.setState({inference: message});
  }
}

export function mergeModelRequestReceipts(
  current: ModelRequestReceipt[],
  incoming: ModelRequestReceipt[],
): ModelRequestReceipt[] {
  const byId = new Map<string, ModelRequestReceipt>();
  for (const receipt of current) byId.set(receipt.manifestId, receipt);
  for (const receipt of incoming) byId.set(receipt.manifestId, receipt);
  return [...byId.values()]
    .sort((left, right) => (
      right.capturedAt - left.capturedAt
      || right.attempt - left.attempt
      || right.manifestId.localeCompare(left.manifestId)
    ))
    .slice(0, MODEL_REQUEST_RECEIPT_LIMIT);
}

export function useOverviewTelemetry() {
  return store.useStore();
}
