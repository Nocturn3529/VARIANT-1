import type {InputQueueSnapshot,InputQueueItem} from "../protocol/chatQueue";
/**
 * Chat domain types (wire parity with the canonical Conversation session API).
 */

/** Composer / bubble attachment (images + small text files + path refs). */
export type ChatAttachment = {
  id: string;
  name: string;
  kind: "image" | "text" | "path" | "folder";
  mime: string;
  /** Base64 without data-URL prefix (images). */
  data?: string;
  /** Inlined text body (text files). */
  text?: string;
  /** Absolute filesystem path (native dialog or preload webUtils bridge). */
  path?: string;
  /** Object URL for local thumbnail preview (revoked on remove/send). */
  previewUrl?: string;
  size: number;
};

/** File, URL, screen, or command cited by a turn (parsed from tool activity). */
export type ChatEvidenceKind = "file" | "folder" | "url" | "screen" | "search" | "command";

export type ChatEvidence = {
  id: string;
  kind: ChatEvidenceKind;
  label: string;
  value: string;
  tool?: string;
};

/** Step row for the live turn or a finished assistant bubble. */
export type ChatTurnStep = {
  summaryState?: "running" | "done" | "discarded" | "cancelled";
  summaryRevision?: number;
  peerMessage?:import("../protocol/peerActivity").PeerActivity;
  id: string;
  kind: "tool" | "note" | "step" | "thinking";
  label: string;
  detail?: string;
  status?: "running" | "ok" | "error" | "done";
  tool?: string;
  /** Provider/host call identity; repeated uses of one tool never share it. */
  callId?: string;
  /** Original host status retained after projection to the small UI vocabulary. */
  rawStatus?: string;
  /** Bounded input and result projections shown only when a row is expanded. */
  argsPreview?: string;
  resultPreview?: string;
  /** Millisecond activity boundaries. */
  startedAt?: number;
  completedAt?: number;
  durationMs?: number;
  admissionMs?: number;
  /** Stable merge key for start→result updates (for example tool:web_search). */
  key?: string;
  /** Paths, URLs, and screens referenced by this step. */
  evidence?: ChatEvidence[];
  ts: number;
};

/** Backend-settled summary of one assistant turn, with a short-lived client preview. */
export type ChatTurnReceipt = {
  model: string;
  provider: string;
  route: "local" | "cloud" | "";
  durationMs: number;
  promptTokens: number | null;
  cachedInputTokens: number | null;
  toolCount: number;
  measurement: string;
};

export type ChatMessage = {
  runId?: string;
  role: "user" | "assistant";
  text: string;
  origin?:{kind:"peer";peer_id:string;message_id:string};
  peerDisplay?:{display_name:string;content:string};
  ts?: number;
  /** Local-only optimistic / stream markers (not persisted). */
  streaming?: boolean;
  localId?: string;
  /** Groups optimistic bubbles until chat:appended supplies the durable turn. */
  optimisticTurnId?: string;
  /** Original composer text (display text may be an attachment-only label). */
  optimisticDraft?: string;
  optimisticOwnsComposer?: boolean;
  /** Short-lived wire-capable retry copy; removed when the optimistic turn settles. */
  optimisticAttachmentRetry?: ChatAttachment[];
  /** Durable active-input ticket used to match steering/follow-up rows. */
  ticketId?: string;
  /** True after the backend admitted this active input into the owning run. */
  activeInputAccepted?: boolean;
  /** Whether an admitted active input is waiting or has reached the model. */
  activeInputState?: "queued" | "delivered";
  /** Local-only attachment previews on the optimistic user bubble. */
  attachments?: ChatAttachment[];
  /** STEPS strip for this reply (persisted + rehydrated when available). */
  steps?: ChatTurnStep[];
  /** How this user row entered an active turn. */
  delivery?: "steer" | "follow_up";
  /** Client-built cost/model summary on the assistant row. */
  receipt?: ChatTurnReceipt;
  /** The reply was visible but the backend could not commit its transcript. */
  durability?: "durable" | "failed";
};

export type PendingActiveInput = {
  ownsComposer?: boolean;
  optimisticTurnId: string;
  localId: string;
  text: string;
  delivery: "steer" | "follow_up";
};

export type MutationTogglePending = {
  requestId: string;
  enabled: boolean;
  /** Snapshot revisions must advance past this value before settling the request. */
  baseRevision: number;
};

export type ChatSessionMeta = {
  id: string;
  title: string;
  created_at?: number;
  updated_at?: number;
  message_count?: number;
  pinned?: boolean;
  archived?: boolean;
};

export type SubtitleState = "ready" | "working" | "idle" | "offline";

export type ChatRuntimeState = {
  inputQueue?: InputQueueSnapshot;
  pauseState?: "pausing" | "paused" | "running" | "idle";
  pauseRevision?: number;
  busy?: boolean;
  activeAdmissionId?: string;
  activeRunId?: string;
  actionSurface: string;
  trustProfile: string;
  catalogReleaseId: string;
  selectedCategoryId: string;
  mountRevision: number;
  overlayRevision: number;
  kernelState: string;
  kernelGeneration: number;
  activeSlots: number;
  probationSlots: number;
  activeChildren: number;
  queuedInputs: number;
  continuationState: string;
  warning: string;
  mutationEnabled: boolean;
  mutationEffectiveEnabled: boolean;
  mutationAuthorityRevision: number;
  mutationToggleAvailable: boolean;
  mutationToggleLocked: boolean;
  mutationToggleReason: string;
};

/** On-demand / auto TTS playback phase for a single reply. */
export type SpeechPhase = "idle" | "loading" | "playing";

export type ChatState = {
  agentTeam: import("../protocol/children").AgentTeamState;
  goal: import("../protocol/goals").ComposerGoalState;
  inputQueue: {
    snapshot:InputQueueSnapshot|null;
    synced:boolean;
    recent:readonly InputQueueItem[];
    refreshRequestId?:string;
    error?:string;
    action?:{operation:"continue"|"remove";requestId:string;ticket:InputQueueItem;revision:number;uncertain?:boolean};
    continuation?:{ticket:InputQueueItem;requestId:string;groupId:string;baseRevision:number;started:boolean;finished:boolean};
  };
  connected: boolean;
  clientId: string;
  sessionId: string | null;
  title: string;
  messages: ChatMessage[];
  /** True while a reply is being streamed for this client. */
  turnActive: boolean;
  streaming: boolean;
  streamText: string;
  subtitle: string;
  subtitleState: SubtitleState;
  lastError: string;
  /** Draft text for the composer. */
  draft: string;
  /** Pending composer attachments (cleared on send). */
  attachments: ChatAttachment[];
  /** Files being read/encoded before they can be included in a send. */
  attachmentsPreparing: number;
  deliveryMode: "steer" | "follow_up";
  stopPending: boolean;
  pause: {
    admissionId: string;
    runId: string;
    state: "pausing" | "paused" | "running" | "idle";
    revision: number;
    synced: boolean;
    pending?: {requestId:string; action:"pause"|"resume"; baseRevision:number};
  } | null;
  /** Which assistant bubble is loading/playing TTS (speechKey). */
  speechKey: string | null;
  speechPhase: SpeechPhase;
  /** Live STEPS for the in-flight turn (cleared when the reply commits). */
  turnSteps: ChatTurnStep[];
  /** Follow-ups acknowledged by the backend for the active turn. */
  queuedFollowUps: number;
  /** Local group currently collecting optimistic initial/follow-up/final bubbles. */
  activeTurnId: string | null;
  /** Terminal persistence can arrive before the visible done frame. */
  pendingTurnCommit?: {owner: string; appended?: import("../protocol/chatEvents").ChatAppendedMessage; failed?: boolean};
  /** Active inputs sent but not yet acknowledged/rejected by the backend. */
  pendingActiveInputs: PendingActiveInput[];
  /** One correlated per-chat mutation-authority change awaiting settlement. */
  mutationTogglePending: MutationTogglePending | null;
  runtime: ChatRuntimeState | null;
  orphanedTask: Readonly<Record<string, unknown>> | null;
};
