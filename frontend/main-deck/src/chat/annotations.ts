/** Bounded, run-correlated delivery of client trace enrichment. */
import type {ChatSessionAnnotateCommand} from "../protocol/chatCommands";
import {getChatState, patchChatState, sendChat, withCachedChatState} from "./stateCore";
import {registerChatProjectionCleanup,registerChatProjectionRetention} from "../state/chatProjectionLifecycle";

type Pending = {command: ChatSessionAnnotateCommand; attempts: number; timer?: ReturnType<typeof setTimeout>};
const pending = new Map<string, Pending>();
registerChatProjectionRetention(id=>[...pending.values()].some(entry=>entry.command.id===id));
registerChatProjectionCleanup(id=>{
  for(const [identity,entry] of pending)if(entry.command.id===id){if(entry.timer)clearTimeout(entry.timer);pending.delete(identity);}
});
const key = (sessionId: string, runId: string) => `${sessionId}\0${runId}`;
let online = true;

function mark(entry: Pending, status: "pending" | "saved" | "failed") {
  withCachedChatState(entry.command.id, () => {
    const state = getChatState();
    const messages = state.messages.map(message => message.role === "assistant" && message.runId === entry.command.run_id
      ? {...message, tracePersistence: status} : message);
    if (messages.some((message, index) => message !== state.messages[index])) patchChatState({messages});
  });
}

function dispatch(entry: Pending) {
  if (pending.get(key(entry.command.id, entry.command.run_id!)) !== entry) return;
  if (entry.timer) clearTimeout(entry.timer);
  entry.timer = undefined;
  if (!online) return;
  if (entry.attempts >= 3) {mark(entry, "failed"); return;}
  entry.attempts++;
  if (!sendChat(entry.command)) {online = false; return;}
  if (pending.get(key(entry.command.id, entry.command.run_id!)) === entry)
    entry.timer = setTimeout(() => dispatch(entry), 5000);
}

export function persistTrace(command: ChatSessionAnnotateCommand): void {
  // Legacy rows without a run identity cannot be retried against "latest".
  if (!command.run_id) return;
  const identity = key(command.id, command.run_id), prior = pending.get(identity);
  if (prior?.timer) clearTimeout(prior.timer);
  if (pending.size >= 128 && !prior) {
    const oldest = pending.keys().next().value!;
    const evicted = pending.get(oldest)!;
    if (evicted.timer) clearTimeout(evicted.timer);
    mark(evicted, "failed");pending.delete(oldest);
  }
  const entry: Pending = {command:{...command, request_id:globalThis.crypto.randomUUID()}, attempts:0};
  pending.set(identity, entry);mark(entry, "pending");dispatch(entry);
}

export function acknowledgeTrace(message: {id: string; run_id: string; request_id: string; ok: boolean}): void {
  const identity = key(message.id, message.run_id), entry = pending.get(identity);
  if (!entry || message.request_id !== entry.command.request_id) return;
  if (entry.timer) clearTimeout(entry.timer);
  entry.timer = undefined;
  mark(entry, message.ok ? "saved" : "failed");
  if (message.ok) pending.delete(identity);
}

export function traceAnnotationConnection(connected: boolean): void {
  if (online === connected) return;
  online = connected;
  for (const entry of pending.values()) {
    if (entry.timer) clearTimeout(entry.timer);
    entry.timer = undefined;
    if (connected) {entry.attempts=0;mark(entry, "pending");dispatch(entry);}
  }
}

export function resetTraceAnnotations(): void {
  for (const entry of pending.values()) if (entry.timer) clearTimeout(entry.timer);
  pending.clear();online=true;
}
