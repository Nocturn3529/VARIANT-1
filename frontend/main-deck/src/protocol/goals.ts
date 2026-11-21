/** One standing goal per chat. The backend owns it; the composer mirrors it. */
export const GOAL_STATUSES = ["active", "paused", "blocked", "budget_limited", "usage_limited", "complete"] as const;
export type GoalStatus = typeof GOAL_STATUSES[number];
export const MAX_GOAL_OBJECTIVE = 32000;
export const isGoalTurnSource = (source?: string) => source === "goal" || source === "goal:submit" || source === "goal:continue";
// The backend counts Unicode code points, including each non-BMP character once.
export const goalLength = (text: string) => Array.from(text).length;

export type ChatGoal = Readonly<{
  goal_id: string;
  session_id: string;
  /** The goal:submit request that created it; settles a submit whose reply was lost. */
  request_id: string;
  objective: string;
  status: GoalStatus;
  token_budget: number | null;
  tokens_used: number;
  time_used_s: number;
  status_reason: string;
  /** A goal turn is running now. */
  continuing: boolean;
  /** Saved and waiting for the chat to go idle before its first turn. */
  submission_pending: boolean;
  created_at: number;
  updated_at: number;
  completed_at: number | null;
  completed_run_id: string | null;
  version: number;
}>;

const count = (value: unknown) => typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : 0;

export function parseChatGoal(value: unknown): ChatGoal | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const row = value as Record<string, unknown>;
  if (typeof row.goal_id !== "string" || !row.goal_id || typeof row.session_id !== "string" || !row.session_id
    || typeof row.objective !== "string" || !GOAL_STATUSES.includes(row.status as GoalStatus)
    || !Number.isSafeInteger(row.version) || Number(row.version) < 1) return null;
  return {goal_id: row.goal_id, session_id: row.session_id, request_id: typeof row.request_id === "string" ? row.request_id : "",
    objective: row.objective, status: row.status as GoalStatus,
    token_budget: Number.isSafeInteger(row.token_budget) && Number(row.token_budget) > 0 ? Number(row.token_budget) : null,
    tokens_used: count(row.tokens_used), time_used_s: count(row.time_used_s),
    status_reason: typeof row.status_reason === "string" ? row.status_reason : "",
    continuing: row.continuing === true, submission_pending: row.submission_pending === true,
    created_at: count(row.created_at), updated_at: count(row.updated_at),
    completed_at: typeof row.completed_at === "number" && Number.isFinite(row.completed_at) ? row.completed_at : null,
    completed_run_id: typeof row.completed_run_id === "string" && row.completed_run_id ? row.completed_run_id : null,
    version: Number(row.version)};
}

/** Anything short of complete still holds the chat's one goal slot. */
export const goalUnfinished = (goal: ChatGoal | null | undefined): goal is ChatGoal => !!goal && goal.status !== "complete";
/** A spent budget is final: only Clear or a new /goal moves on. */
export const goalCanResume = (goal: ChatGoal) => ["paused", "blocked", "usage_limited"].includes(goal.status);
export const goalCanEdit = (goal: ChatGoal) => goal.status !== "complete";

export function goalStatusLabel(goal: ChatGoal): string {
  if (goal.status === "active") return goal.submission_pending ? "Goal set" : goal.continuing ? "Continuing goal…" : "Pursuing goal";
  return {paused: "Paused goal", blocked: "Goal stalled", budget_limited: "Goal limited",
    usage_limited: "Goal usage limited", complete: "Goal achieved"}[goal.status];
}

export function formatGoalTokens(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(value >= 10_000_000 ? 0 : 1)}M`;
  if (value >= 1000) return `${(value / 1000).toFixed(value >= 10_000 ? 0 : 1)}k`;
  return String(Math.round(value));
}

export function formatGoalDuration(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  const hours = Math.floor(total / 3600), minutes = Math.floor(total % 3600 / 60), rest = total % 60;
  return hours ? `${hours}h ${minutes}m` : minutes ? `${minutes}m ${rest}s` : `${rest}s`;
}

export type GoalAction = "pause" | "resume" | "clear" | "edit";

export type GoalDraft = Readonly<{sessionId: string; objective: string; draft: string; draftRevision: number}>;
export type GoalReplacement = GoalDraft & {expectedGoalId?: string};
export type PendingGoal = {requestId: string; uncertain?: boolean; observedUpdate?: boolean} & (
  | {operation: "submit"; submission: GoalDraft; expectedGoalId?: string}
  | {operation: GoalAction; goalId: string; version: number; objective?: string}
);

export type ChatGoalState = {
  goal: ChatGoal | null;
  /** The goal shown matches the backend's latest answer for this chat. */
  synced: boolean;
  error?: string;
  readError?: string;
  readRequestId?: string;
  /** Only a read opened to reconcile this particular write may settle it. */
  readForRequestId?: string;
  /** A push or reply landed while a read was in flight, so that read may be older. */
  readStale?: boolean;
  pending?: PendingGoal;
  confirmedRequestId?: string;
  /** The composer is in goal mode (the Goal chip is shown). */
  composing?: boolean;
  /** A /goal over an unfinished goal waits for "Replace current goal?". */
  confirmReplace?: GoalReplacement;
};

export type GoalCommand =
  | ({type: "goal:submit"; session_id: string; request_id: string; objective: string} & (
    | {replace?: false; expected_goal_id?: never}
    | {replace: true; expected_goal_id: string}
  ))
  | {type: "goal:current:get"; session_id: string; request_id?: string}
  | {type: "goal:control"; session_id: string; request_id: string; goal_id: string; action: GoalAction; expected_version: number; objective?: string};

export type GoalMessage = Readonly<{
  type: "goal:accepted" | "goal:rejected" | "goal:current";
  session_id: string;
  request_id: string;
  operation: string;
  /** Undefined when an accepted reply carried no readable goal. */
  goal: ChatGoal | null | undefined;
  reason_code: string;
  error: string;
}>;

export type GoalUpdatedMessage = Readonly<{type: "goal:updated"; session_id: string; goal: ChatGoal | null}>;

function replyGoal(result: unknown): ChatGoal | null | undefined {
  if (!result || typeof result !== "object" || !("goal" in result)) return undefined;
  const value = (result as Record<string, unknown>).goal;
  return value === null ? null : parseChatGoal(value) ?? undefined;
}

export function parseGoalMessage(row: Record<string, unknown>): GoalMessage | GoalUpdatedMessage | null {
  if (typeof row.session_id !== "string" || !row.session_id) return null;
  if (row.type === "goal:updated") {
    const goal = row.goal === null ? null : parseChatGoal(row.goal);
    if (goal ? goal.session_id !== row.session_id : row.goal !== null) return null;
    return {type: "goal:updated", session_id: row.session_id, goal};
  }
  if (!["goal:accepted", "goal:rejected", "goal:current"].includes(String(row.type)) || typeof row.request_id !== "string") return null;
  const goal = replyGoal(row.result);
  if (goal && goal.session_id !== row.session_id) return null;
  return {type: row.type as GoalMessage["type"], session_id: row.session_id, request_id: row.request_id,
    operation: typeof row.operation === "string" ? row.operation : "", goal,
    reason_code: typeof row.reason_code === "string" ? row.reason_code : "",
    error: typeof row.error === "string" ? row.error : ""};
}
