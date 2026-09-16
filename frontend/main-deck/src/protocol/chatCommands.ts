/**
 * Outbound Chat WebSocket commands (composer / session / speech).
 * Other destinations keep using the open `WsCommand` envelope.
 */

export type ChatWireAttachment = {
  name: string;
  kind: string;
  mime: string;
  data?: string;
  text?: string;
  path?: string;
};

export type ChatSendCommand = {
  type: "chat";
  text: string;
  client_id: string;
  session_id: string;
  admission_id?: string;
  run_id?: string;
  delivery?: "steer" | "follow_up";
  /** Stable client correlation id for active-turn input admission. */
  ticket_id?: string;
  attachments?: ChatWireAttachment[];
  resume?: boolean;
};

export type ChatCancelCommand = {type: "cancel"; session_id: string; admission_id?: string; run_id?: string};
export type ChatPauseCommand = {type: "chat:pause" | "chat:resume"; session_id: string; admission_id?: string; run_id?: string; request_id: string};
export type ChatQueueCommand = {type:"chat:queue:get";session_id:string;request_id:string}
  | {type:"chat:queue:continue"|"chat:queue:remove";session_id:string;ticket_id:string;expected_revision:number;request_id:string};

export type ChatSessionGetCommand = {
  type: "chat:session:get";
  id?: string;
};

export type ChatSessionsListCommand = {type: "chat:sessions"};

export type ChatSessionAnnotateCommand = {
  type: "chat:session:annotate";
  id: string;
  run_id?: string;
  steps: unknown[];
  receipt?: unknown;
};

export type ChatRuntimeGetCommand = {
  type: "chat:runtime:get";
  id: string;
};

export type ChatRuntimeActionCommand = {
  type: "chat:runtime:action";
  id: string;
  action: "stop_cell" | "restart_kernel" | "stop_kernel" | "reset_session_tools";
};

export type ChatRuntimeMutationSetCommand = {
  type: "chat:runtime:mutation:set";
  id: string;
  enabled: boolean;
  request_id: string;
  expected_revision: number;
};

export type ChatTtsPreviewCommand = {
  type: "tts:preview";
  purpose: "chat" | string;
  request_id: string;
  session_id?: string;
  text: string;
  voice?: string;
};

export type ChatWsCommand =
  | import("./children").ChildrenCommand
  | import("./goals").GoalCommand
  | ChatSendCommand
  | ChatCancelCommand
  | ChatPauseCommand
  | ChatQueueCommand
  | ChatSessionGetCommand
  | ChatSessionsListCommand
  | ChatSessionAnnotateCommand
  | ChatRuntimeGetCommand
  | ChatRuntimeActionCommand
  | ChatRuntimeMutationSetCommand
  | ChatTtsPreviewCommand
  | {type: "tts:preview:cancel"; request_id: string}
  | {type: "mode:set"; scope: "session"; id: string; mode: "local" | "cloud"; provider: string; model: string; reasoning_effort?: string}
  | {type: "reasoning:effort:set"; id: string; effort: string};
