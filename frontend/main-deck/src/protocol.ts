/**
 * Main Deck WebSocket boundary.
 *
 * The Python backend remains the protocol authority. Envelope types keep
 * commands/messages honest at the TypeScript boundary, while the module map
 * prevents every inbound event from being broadcast to every React store.
 *
 * High-churn Chat events are shaped via `parseChatWsMessage` (see
 * `protocol/chatEvents.ts`) at the DeckRuntime edge before stores see them.
 */

export type WsCommand<T extends string = string> = {
  type: T;
  [key: string]: unknown;
};

export type WsMessage<T extends string = string> = Readonly<{
  type: T;
  [key: string]: unknown;
}>;

export const REACT_MODULE_MESSAGE_TYPES = {
  "react-runtime-platform": [
    "hello", "config", "engine", "tools", "messaging:gateway", "messaging:error",
    "cloud:usage",
    "cloud:oauth:pending", "cloud:oauth:complete", "cloud:oauth:error",
    "cloud:oauth:busy",
    "cloud:oauth:cancelled", "cloud:oauth:disconnected",
    "cloud:credential:accepted", "cloud:credential:rejected", "cloud:credential:items",
    "cloud:custom-endpoints", "cloud:custom-endpoint:validated",
    "cloud:custom-endpoint:saved", "cloud:custom-endpoint:activated",
    "cloud:custom-endpoint:removed", "cloud:custom-endpoint:error",
    "inference:platform", "inference:install:job",
    "inference:install:jobs", "inference:install:cancelled",
  ],
  "react-runtime-general": [
    "hello", "config", "engine", "models", "models:error", "capabilities",
    "tts:voices", "tts:preview", "speech:accepted", "speech:rejected",
  ],
  "react-runtime-tools": [
    "tools", "tools:accepted", "tools:rejected", "searxng:status",
  ],
  "react-runtime-browser-host": [
    "browser:host:command", "browser:host:registered",
  ],
  "react-runtime-service-settings": ["service-settings:result"],
  "react-runtime-local-models": ["local-models:result"],
  "react-runtime-browser-settings": [
    "browser:settings", "browser:state", "browser:selection:result", "browser:resolve:result", "browser:recordings",
  ],
  "react-runtime-peers": ["peers:result","peer:changed","peers:grok:changed"],
  "react-runtime-plugins": [
    "extension-v2:accepted", "extension-v2:rejected",
  ],
  "react-runtime-about": ["hello", "config", "engine", "doctor:result"],
  "react-runtime-chat": [
    "children:snapshot", "children:detail", "children:rejected", "children:changed",
    "goal:accepted", "goal:rejected", "goal:current", "work:event",
    "hello", "orphaned_task", "config", "engine", "chat:session", "chat:runtime", "chat:appended", "chat:transcript_failed",
    "chat:session:error", "chat:queued", "chat:queue_rejected", "chat:queue_progress", "chat:queue_settled", "chat:rejected",
    "chat:runtime:mutation:set:done", "chat:runtime:mutation:set:rejected", "chat:pause_state", "chat:queue_snapshot", "chat:queue_result",
    "start", "token", "thinking", "tool:activity",
    "activity", "done", "run:settled", "cancelling", "error", "speak", "tts:preview",
  ],
  "react-runtime-sessions": [
    "chat:project:result",
    "chat:sessions", "chat:session:deleted", "chat:search:results", "chat:session:error", "chat:switch:result", "chat:new:result",
  ],
  "react-runtime-session-context": [
    "chat:context", "model:options", "model:options:error", "session:settings:ack", "session:settings:snapshot",
  ],
  "react-runtime-clarifications": [
    "clarification:request", "clarification:closed", "clarification:snapshot", "clarification:response:ack", "work:event",
    "chat:session", "chat:sessions",
  ],
  "react-runtime-mic": ["transcript"],
  "react-runtime-execution": [
    "execution:snapshot", "terminal:accepted", "terminal:rejected",
    "process:accepted", "process:rejected",
  ],
  "react-runtime-memory": [
    "memory:core", "memory:list", "memory:proposals",
    "memory:loops", "memory:loop", "memory:loop:promote",
    "memory:consolidate", "memory:export", "memory:error",
  ],
  "react-runtime-automations": [
    "automations", "automations:history", "automation:accepted", "automation:error",
  ],
  "react-runtime-overview": [
    "engine", "hardware:telemetry", "inference:telemetry",
    "cloud:usage", "model:usage", "model:request_manifest",
    "model:request_manifests",
  ],
} as const satisfies Record<string, readonly string[]>;

export type ReactRuntimeModuleId = keyof typeof REACT_MODULE_MESSAGE_TYPES;

// Chat family (high-churn) — re-export for a single import surface.
export type {
  ChatAppendedMessage,
  ChatHelloMessage,
  ChatQueuedMessage,
  ChatRuntimeMutationSetDoneMessage,
  ChatRuntimeMutationSetRejectedMessage,
  ChatRuntimeMessage,
  ChatQueueRejectedMessage,
  ChatQueueProgressMessage,
  ChatRejectedMessage,
  ChatSessionErrorMessage,
  ChatSessionMessage,
  ChatUnknownMessage,
  ChatWsMessage,
  RawWsEnvelope,
  RunSettledMessage,
  SpeakMessage,
  StreamActivityMessage,
  StreamCancellingMessage,
  StreamDoneMessage,
  StreamErrorMessage,
  StreamRouting,
  StreamStartMessage,
  StreamThinkingMessage,
  StreamTokenMessage,
  TtsPreviewMessage,
} from "./protocol/chatEvents";
export {
  CHAT_STREAM_TYPES,
  parseChatWsMessage,
  parseWsEnvelope,
} from "./protocol/chatEvents";

export type {
  ChatCancelCommand,
  ChatSendCommand,
  ChatRuntimeActionCommand,
  ChatRuntimeGetCommand,
  ChatRuntimeMutationSetCommand,
  ChatSessionAnnotateCommand,
  ChatSessionGetCommand,
  ChatTtsPreviewCommand,
  ChatWireAttachment,
  ChatWsCommand,
} from "./protocol/chatCommands";
