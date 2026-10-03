import {useSyncExternalStore} from "react";
import {createExternalStore} from "../state/createModuleStore";
import type {StreamActivityMessage} from "../protocol/chatEvents";
import type {AgentSummary, ChildActivityHint} from "../protocol/children";
import {normalizeActivityStatus} from "./activityModel";
import {traceActionLabel} from "./traceLabels";

/*
 * Live subagent steps. Child runs broadcast `activity` frames whose session_id
 * is the child's chat id; those frames never enter the parent transcript, so
 * the roster keeps only the latest display-safe step per child chat.
 */
export type ChildActivity = ChildActivityHint & Readonly<{text: string; callId: string}>;
const MAX_CHILDREN = 256;
const store = createExternalStore<Readonly<Record<string, ChildActivity>>>({});

export function noteChildActivity(message: StreamActivityMessage): void {
  const chatId = message.session_id || "";
  if (message.source !== "subagent" || !chatId) return;
  // The UI notification repeats tool:start without call_id; the authoritative frame carries it.
  if (message.event === "tool:start" && !message.call_id) return;
  const ts = typeof message.ts === "number" && message.ts > 0 ? message.ts : Date.now() / 1000;
  const current = store.getState(), prior = current[chatId];
  if (prior && prior.ts > ts) return;
  const next: Record<string, ChildActivity> = {...current};
  delete next[chatId];
  next[chatId] = {event: message.event, tool: message.tool || "", status: message.status || "", title: message.title || "",
    text: message.text || "", ts, runId: message.run_id || "", callId: message.call_id || ""};
  const keys = Object.keys(next);
  for (const key of keys.slice(0, Math.max(0, keys.length - MAX_CHILDREN))) delete next[key];
  store.replaceState(next);
}

/** Newest step for this child's current run: live frames win over the snapshot hint. */
export function useChildActivity(agent: AgentSummary): ChildActivityHint | ChildActivity | null {
  const live = useSyncExternalStore(store.subscribe, () => store.getState()[agent.chatId], () => undefined);
  if (agent.status !== "running") return null;
  const current = (value: ChildActivityHint | null | undefined) => !!value
    && (!agent.runId || !value.runId || value.runId === agent.runId)
    && (!agent.startedAt || value.ts >= agent.startedAt);
  const hint = current(agent.lastActivity) ? agent.lastActivity! : null;
  const frame = current(live) ? live! : null;
  if (frame && hint) return frame.ts >= hint.ts ? frame : hint;
  return frame || hint;
}

const compact = (value: string, limit = 120) => {
  const text = value.replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit - 1).trimEnd()}…` : text;
};

/** One short present-tense line describing what a running child is doing. */
export function childStepLabel(activity: ChildActivityHint | ChildActivity): string {
  const text = "text" in activity ? activity.text : "";
  switch (activity.event) {
    case "task:thinking": return "Thinking";
    case "task:start": return "Starting";
    case "task:done": return "Finishing";
    case "tool:start":
    case "tool:result": {
      const status = activity.event === "tool:start" ? "running" : normalizeActivityStatus(activity.status, activity.event);
      return compact(traceActionLabel({id: activity.tool || "step", kind: "tool", label: activity.title || activity.tool, tool: activity.tool, status, ts: activity.ts}, true));
    }
    default: return compact(activity.title || text || "Working");
  }
}

export function __resetChildActivityForTests(): void {
  store.replaceState({});
}
