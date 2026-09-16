import {parseInputQueueSnapshot,type InputQueueSnapshot,type InputQueueResult} from "./chatQueue";
import {parsePeerActivity,type PeerActivity} from "./peerActivity";
import {parseGoalMessage,parseGoalWorkEvent,type GoalMessage,type GoalWorkEvent} from "./goals";
import {parseChildrenMessage,parseChildrenChanged,type ChildrenMessage,type ChildrenChangedMessage} from "./children";
/**
 * High-churn Chat inbound events — shaped at the DeckRuntime edge.
 *
 * Python remains protocol authority; these types document the fields Chat
 * actually reads so stores stop re-parsing `Record<string, unknown>`.
 * Rare/settings messages stay on the loose `WsMessage` envelope.
 */

/** Minimal inbound envelope (compatible with protocol.WsMessage). */
export type RawWsEnvelope = {
  type: string;
  [key: string]: unknown;
};

// ── Shared routing ──────────────────────────────────────────────────────────

/** Stream isolation fields shared by start/token/done/activity/error. */
export type StreamRouting = {
  ticket_id?: string;
  request_id?: string;
  client_id?: string;
  source?: string;
  session_id?: string;
  admission_id?: string;
  run_id?: string;
};

export type ChatPauseState = "pausing" | "paused" | "running" | "idle";
export type ChatPauseStateMessage = Readonly<{
  type: "chat:pause_state";
  session_id: string;
  admission_id: string;
  run_id: string;
  pause_revision: number;
  state: ChatPauseState | null;
  accepted: boolean;
  request_id?: string;
  error?: string;
}>;

// ── Discriminated inbound messages (chat family) ────────────────────────────

export type OrphanedTaskPayload = Readonly<Record<string, unknown>>;
export type ChatHelloMessage = Readonly<{
  type: "hello";
  orphaned_task?: OrphanedTaskPayload;
} & StreamRouting>;
export type ChatOrphanedTaskMessage = Readonly<{
  type: "orphaned_task";
  task: OrphanedTaskPayload;
}>;
export type ChatConfigMessage = Readonly<{type: "config"} & StreamRouting>;
export type ChatEngineMessage = Readonly<{type: "engine"} & StreamRouting>;

/** Full session document; nested shape is owned by session apply. */
export type ChatSessionPayload = Record<string, unknown>;
export type ChatNavigationOutcome = Readonly<{
  request_id: string;
  requested_id: string;
  effective_id: string;
  status: "switched" | "fallback" | "rejected" | "created";
}>;

function parseNavigation(value: unknown): ChatNavigationOutcome | undefined {
  if (!value || typeof value !== "object") return;
  const row = value as Record<string, unknown>;
  if (typeof row.request_id !== "string" || typeof row.requested_id !== "string" || typeof row.effective_id !== "string"
    || !["switched", "fallback", "rejected", "created"].includes(String(row.status))) return;
  return {request_id: row.request_id, requested_id: row.requested_id, effective_id: row.effective_id,
    status: row.status as ChatNavigationOutcome["status"]};
}

export type ChatSessionMessage = Readonly<{
  type: "chat:session";
  session: ChatSessionPayload;
  navigation?: ChatNavigationOutcome;
}>;

export type ChatRuntimeMessage = Readonly<{
  type: "chat:runtime";
  id: string;
  runtime: Record<string, unknown>;
}>;

export type ChatSessionErrorMessage = Readonly<{
  type: "chat:session:error";
  error: string;
}>;

export type ChatRuntimeMutationSetDoneMessage = Readonly<{
  type: "chat:runtime:mutation:set:done";
  id: string;
  enabled: boolean;
  effective_enabled: boolean;
  request_id: string;
  authority_revision: number;
}>;

export type ChatRuntimeMutationSetRejectedMessage = Readonly<{
  type: "chat:runtime:mutation:set:rejected";
  id: string;
  enabled: boolean;
  request_id: string;
  error: string;
}>;

export type ChatQueuedMessage = Readonly<{
  queue?: InputQueueSnapshot|null;
  type: "chat:queued";
  id: string;
  delivery: "steer" | "follow_up";
  queue_size: number;
} & StreamRouting>;

export type ChatQueueRejectedMessage = Readonly<{
  type: "chat:queue_rejected";
  error: string;
  id?: string;
} & StreamRouting>;

export type ChatQueueProgressMessage = Readonly<{
  queue?: InputQueueSnapshot|null;
  type: "chat:queue_progress";
  id: string;
  delivery: "steer" | "follow_up";
  state: "delivered";
  session_id?: string;
  queue_size: number;
}>;

export type ChatQueueSettledMessage = Readonly<{
  type: "chat:queue_settled";
  ids: readonly string[];
  reason: string;
  session_id?: string;
  queue_size: number;
}>;

export type ChatRejectedMessage = Readonly<{
  type: "chat:rejected";
  error: string;
  text: string;
} & StreamRouting>;

export type ChatAppendedMessage = Readonly<{
  type: "chat:appended";
  session_id?: string;
  client_id?: string;
  source?: string;
  user?: unknown;
  assistant?: unknown;
  /** Genuine ordered rows for the just-committed run, including active input. */
  messages?: readonly unknown[];
} & StreamRouting>;

export type ChatTranscriptFailedMessage = Readonly<{
  type: "chat:transcript_failed";
  session_id?: string;
  ticket_ids: readonly string[];
  error: string;
  terminal_reply_visible: boolean;
} & StreamRouting>;

export type StreamStartMessage = Readonly<{
  type: "start";
} & StreamRouting>;

export type StreamTokenMessage = Readonly<{
  type: "token";
  token: string;
} & StreamRouting>;

export type StreamThinkingMessage = Readonly<{
  type: "thinking";
  text: string;
  summary_id?: string;
  summary_source?: "provider_summary";
  summary_revision?: number;
  ts?: number;
  status?: "running" | "done" | "discarded" | "cancelled";
} & StreamRouting>;

/** Main-chat STEPS activity for tool traces and task steps. */
export type StreamActivityMessage = Readonly<{
  peer_message?:PeerActivity;
  type: "activity" | "tool:activity";
  event: string;
  tool: string;
  call_id?: string;
  status: string;
  text: string;
  args_preview: string;
  title: string;
  surface: string;
  ts?: number;
  duration_ms?: number;
  admission_ms?: number;
  total_duration_ms?: number;
  receipt_id?: string;
  durable_replay?: boolean;
  step?: string;
  loop_id?: string;
  kind?: string;
} & StreamRouting>;

export type StreamDoneMessage = Readonly<{
  type: "done";
  text: string;
  cancelled: boolean;
  run_id?: string;
  status?: string;
  stop_reason?: string;
  terminal_reason?: string;
  cause_class?: string;
  length_recoveries?: number;
  settled?: boolean;
  durable?: boolean;
} & StreamRouting>;

export type RunSettledMessage = Readonly<{
  type: "run:settled";
  run_id: string;
  status: string;
  stop_reason: string;
  terminal_reason: string;
  cause_class: string;
  length_recoveries: number;
  settled: true;
  receipt: Readonly<Record<string, unknown>>;
} & StreamRouting>;

export type StreamCancellingMessage = Readonly<{
  type: "cancelling";
  accepted?: boolean;
  error?: string;
} & StreamRouting>;

export type StreamErrorMessage = Readonly<{
  type: "error";
  error: string;
} & StreamRouting>;

export type SpeakMessage = Readonly<{
  type: "speak";
  audio: string;
  mime_type: string;
  session_id: string;
}>;

export type TtsPreviewMessage = Readonly<{
  type: "tts:preview";
  purpose: string;
  request_id: string;
  audio: string;
  mime_type: string;
  session_id: string;
  error?: string;
  cancelled?: boolean;
}>;

/**
 * Fallback for types on the chat module map that we do not shape yet.
 * Uses a dedicated discriminant so it does not collapse switch narrowing
 * on the other members (`type: string` would match every case).
 */
export type ChatUnknownMessage = Readonly<{
  type: "chat:unknown";
  originalType: string;
} & StreamRouting>;

export type ChatWsMessage =
  | ChildrenChangedMessage
  | ChildrenMessage
  | GoalWorkEvent
  | GoalMessage
  | InputQueueSnapshot
  | InputQueueResult
  | ChatHelloMessage
  | ChatOrphanedTaskMessage
  | ChatConfigMessage
  | ChatEngineMessage
  | ChatSessionMessage
  | ChatRuntimeMessage
  | ChatPauseStateMessage
  | ChatSessionErrorMessage
  | ChatRuntimeMutationSetDoneMessage
  | ChatRuntimeMutationSetRejectedMessage
  | ChatQueuedMessage
  | ChatQueueRejectedMessage
  | ChatQueueProgressMessage
  | ChatQueueSettledMessage
  | ChatRejectedMessage
  | ChatAppendedMessage
  | ChatTranscriptFailedMessage
  | StreamStartMessage
  | StreamTokenMessage
  | StreamThinkingMessage
  | StreamActivityMessage
  | StreamDoneMessage
  | RunSettledMessage
  | StreamCancellingMessage
  | StreamErrorMessage
  | SpeakMessage
  | TtsPreviewMessage
  | ChatUnknownMessage;

/** Stream event types that must respect client/source isolation. */
export const CHAT_STREAM_TYPES = new Set([
  "start",
  "token",
  "thinking",
  "tool:activity",
  "activity",
  "done",
  "cancelling",
  "error",
]);

// ── Coercion helpers ────────────────────────────────────────────────────────

function optStr(value: unknown): string | undefined {
  if (value == null) return undefined;
  const s = String(value);
  return s ? s : undefined;
}

function str(value: unknown, fallback = ""): string {
  if (value == null) return fallback;
  return String(value);
}

function routing(row: Record<string, unknown>): StreamRouting {
  return {
    ...(typeof row.ticket_id==="string" && row.ticket_id ? {ticket_id:row.ticket_id} : {}),
    ...(typeof row.request_id==="string" && row.request_id ? {request_id:row.request_id} : {}),
    client_id: optStr(row.client_id),
    source: optStr(row.source),
    session_id: optStr(row.session_id),
    ...(typeof row.admission_id === "string" ? {admission_id:row.admission_id} : {}),
    ...(typeof row.run_id === "string" ? {run_id:row.run_id} : {}),
  };
}

/**
 * Shape a raw WS envelope into a Chat family message.
 * Always returns a message (loose fallback) so dispatch never drops frames.
 */
export function parseChatWsMessage(
  raw: RawWsEnvelope | Record<string, unknown>,
): ChatWsMessage {
  const row = raw as Record<string, unknown>;
  const type = str(row.type);
  const r = routing(row);

  switch (type) {
    case "children:changed":
      return parseChildrenChanged(row) || {type:"chat:unknown",originalType:type,...r};
    case "children:snapshot":
    case "children:detail":
    case "children:rejected":
      return parseChildrenMessage(row) || {type:"chat:unknown",originalType:type,...r};
    case "work:event":
      return parseGoalWorkEvent(row) || {type:"chat:unknown",originalType:type,...r};
    case "goal:accepted":
    case "goal:rejected":
    case "goal:current":
      return parseGoalMessage(row) || {type:"chat:unknown",originalType:type,...r};
    case "chat:queue_snapshot":
      return parseInputQueueSnapshot(row) || {type:"chat:unknown",originalType:type,...r};
    case "chat:queue_result":
      if(row.operation!=="continue" && row.operation!=="remove" && row.operation!=="get") return {type:"chat:unknown",originalType:type,...r};
      return {type,operation:row.operation,session_id:str(row.session_id),request_id:str(row.request_id),accepted:row.accepted===true,error:optStr(row.error),queue:parseInputQueueSnapshot(row.queue)};
    case "chat:pause_state":
      return {type, session_id:str(row.session_id), admission_id:str(row.admission_id), run_id:str(row.run_id),
        pause_revision: typeof row.pause_revision === "number" && Number.isSafeInteger(row.pause_revision) && row.pause_revision >= 0 ? row.pause_revision : -1,
        state: ["pausing","paused","running","idle"].includes(String(row.state)) ? row.state as ChatPauseState : null,
        accepted: row.accepted === true, request_id:optStr(row.request_id), error:optStr(row.error)};
    case "hello":
      return {
        type: "hello",
        orphaned_task: row.orphaned_task && typeof row.orphaned_task === "object"
          ? row.orphaned_task as OrphanedTaskPayload
          : undefined,
        ...r,
      };
    case "orphaned_task":
      return {type: "orphaned_task", task: {...row}};
    case "config":
      return {type: "config", ...r};
    case "engine":
      return {type: "engine", ...r};

    case "chat:session": {
      const session = row.session && typeof row.session === "object"
        ? row.session as ChatSessionPayload
        : {};
      return {type: "chat:session", session, navigation: parseNavigation(raw.navigation)};
    }
    case "chat:runtime":
      return {
        type: "chat:runtime",
        id: str(row.id),
        runtime: row.runtime && typeof row.runtime === "object"
          ? row.runtime as Record<string, unknown>
          : {},
      };
    case "chat:session:error":
      return {
        type: "chat:session:error",
        error: str(row.error, "Could not update session"),
      };
    case "chat:runtime:mutation:set:done":
      return {
        type: "chat:runtime:mutation:set:done",
        id: str(row.id),
        enabled: row.enabled === true,
        effective_enabled: row.effective_enabled === true,
        request_id: str(row.request_id),
        authority_revision: Math.max(0, Number(row.authority_revision) || 0),
      };
    case "chat:runtime:mutation:set:rejected":
      return {
        type: "chat:runtime:mutation:set:rejected",
        id: str(row.id),
        enabled: row.enabled === true,
        request_id: str(row.request_id),
        error: str(row.error, "Mutation authority change was rejected"),
      };
    case "chat:queued":
      return {
        type: "chat:queued",
        ...(row.queue!==undefined ? {queue:parseInputQueueSnapshot(row.queue)} : {}),
        ...r,
        id: str(row.id),
        delivery: row.delivery === "steer" ? "steer" : "follow_up",
        queue_size: Math.max(0, Number(row.queue_size) || 0),
      };
    case "chat:queue_rejected":
      return {
        type: "chat:queue_rejected",
        ...r,
        error: str(row.error, "Could not queue the follow-up"),
        id: optStr(row.id),
      };
    case "chat:queue_progress":
      return {
        type: "chat:queue_progress",
        ...(row.queue!==undefined ? {queue:parseInputQueueSnapshot(row.queue)} : {}),
        id: str(row.id),
        delivery: row.delivery === "follow_up" ? "follow_up" : "steer",
        state: "delivered",
        session_id: optStr(row.session_id),
        queue_size: Math.max(0, Number(row.queue_size) || 0),
      };
    case "chat:queue_settled":
      return {
        type: "chat:queue_settled",
        ids: Array.isArray(row.ids) ? row.ids.map(String).filter(Boolean) : [],
        reason: str(row.reason, "turn_ended_before_input_delivery"),
        session_id: optStr(row.session_id),
        queue_size: Math.max(0, Number(row.queue_size) || 0),
      };
    case "chat:rejected":
      return {
        type: "chat:rejected",
        ...r,
        error: str(row.error, "chat_rejected"),
        text: str(row.text, "Could not start this turn"),
      };
    case "chat:appended":
      return {
        type: "chat:appended",
        ...r,
        session_id: optStr(row.session_id),
        client_id: optStr(row.client_id),
        source: optStr(row.source),
        user: row.user,
        assistant: row.assistant,
        messages: Array.isArray(row.messages) ? row.messages : undefined,
      };
    case "chat:transcript_failed":
      return {
        type: "chat:transcript_failed",
        ...r,
        session_id: optStr(row.session_id),
        ticket_ids: Array.isArray(row.ticket_ids)
          ? row.ticket_ids.map(String).filter(Boolean)
          : [],
        error: str(row.error, "transcript_persistence_failed"),
        terminal_reply_visible: row.terminal_reply_visible === true,
      };


    case "start":
      return {type: "start", ...r};
    case "token":
      return {type: "token", token: str(row.token), ...r};
    case "thinking": {
      const publicSummary = row.summary_source === "provider_summary";
      const valid = /^summary_[a-f0-9]{32}$/.test(str(row.summary_id))
        && ["running", "done", "discarded", "cancelled"].includes(str(row.status))
        && (row.summary_revision === undefined || (Number.isSafeInteger(row.summary_revision) && Number(row.summary_revision) >= 0));
      if (publicSummary && !valid) return {type: "chat:unknown", originalType: type, ...r};
      return {type: "thinking", text: str(row.text), ...r,
        ...(publicSummary ? {summary_id: str(row.summary_id), summary_source: "provider_summary" as const,
          summary_revision: row.summary_revision as number | undefined, status: row.status as StreamThinkingMessage["status"],
          ts: typeof row.ts === "number" && Number.isFinite(row.ts) ? row.ts : undefined} : {})};
    }
    case "activity":
    case "tool:activity":
      return {
        type,
        event: str(row.event),
        peer_message:parsePeerActivity(row.peer_message),
        tool: str(row.tool),
        call_id: optStr(row.call_id),
        status: str(row.status),
        text: str(row.text),
        args_preview: str(row.args_preview),
        title: str(row.title),
        surface: str(row.surface),
        ts: row.ts == null ? undefined : Number(row.ts) || undefined,
        duration_ms: row.duration_ms == null ? undefined : Math.max(0, Number(row.duration_ms) || 0),
        admission_ms: row.admission_ms == null ? undefined : Math.max(0, Number(row.admission_ms) || 0),
        total_duration_ms: row.total_duration_ms == null ? undefined : Math.max(0, Number(row.total_duration_ms) || 0),
        receipt_id: optStr(row.receipt_id),
        durable_replay: row.durable_replay === true,
        step: row.step != null ? str(row.step) : undefined,
        loop_id: optStr(row.loop_id),
        kind: optStr(row.kind),
        ...r,
      };
    case "done":
      return {
        type: "done",
        text: str(row.text),
        cancelled: !!row.cancelled,
        run_id: optStr(row.run_id),
        status: optStr(row.status),
        stop_reason: optStr(row.stop_reason),
        terminal_reason: optStr(row.terminal_reason),
        cause_class: optStr(row.cause_class),
        length_recoveries: Math.max(0, Number(row.length_recoveries) || 0),
        settled: row.settled === true,
        durable: row.durable !== false,
        ...r,
      };
    case "run:settled":
      return {
        type: "run:settled",
        run_id: str(row.run_id),
        status: str(row.status, "unknown"),
        stop_reason: str(row.stop_reason),
        terminal_reason: str(row.terminal_reason),
        cause_class: str(row.cause_class, "unknown"),
        length_recoveries: Math.max(0, Number(row.length_recoveries) || 0),
        settled: true,
        receipt: row.receipt && typeof row.receipt === "object"
          ? row.receipt as Readonly<Record<string, unknown>>
          : {},
        ...r,
      };
    case "cancelling":
      return {type: "cancelling", ...r, ...(typeof row.accepted === "boolean" ? {accepted:row.accepted} : {}), ...(typeof row.error === "string" ? {error:row.error} : {})};
    case "error":
      return {
        type: "error",
        error: str(row.error, "VARIANT-1 backend error"),
        ...r,
      };

    case "speak":
      return {
        type: "speak",
        audio: str(row.audio),
        mime_type: str(row.mime_type, "audio/wav"),
        session_id: str(row.session_id),
      };
    case "tts:preview":
      return {
        type: "tts:preview",
        purpose: str(row.purpose, "preview"),
        request_id: str(row.request_id),
        audio: str(row.audio),
        mime_type: str(row.mime_type, "audio/wav"),
        session_id: str(row.session_id),
        error: row.error != null && row.error !== false
          ? str(row.error)
          : undefined,
        cancelled: Boolean(row.cancelled),
      };

    default:
      return {type: "chat:unknown", originalType: type || "unknown", ...r};
  }
}

/**
 * Loose envelope parse for BackendClient: require a non-empty string `type`.
 */
export function parseWsEnvelope(raw: unknown): RawWsEnvelope | null {
  if (!raw || typeof raw !== "object") return null;
  const type = (raw as Record<string, unknown>).type;
  if (typeof type !== "string" || !type) return null;
  return raw as RawWsEnvelope;
}
