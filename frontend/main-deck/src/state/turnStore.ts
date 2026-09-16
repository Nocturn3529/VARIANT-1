export type TurnEndStatus = "complete" | "cancelled" | "error" | string;
export type TurnSnapshot = Readonly<{
  active: boolean; clientId: string; source: string; sessionId: string; startedAt: number;
  lastEndStatus: TurnEndStatus; admissionId: string; runId: string;
}>;
export type BeginTurnOptions = {clientId?: string; source?: string; sessionId?: string; admissionId?: string; runId?: string};
export type EndTurnOptions = {status?: TurnEndStatus; force?: boolean};
type TurnListener = (snapshot: TurnSnapshot, previous: TurnSnapshot) => void;
const turns = new Map<string, TurnSnapshot>();
const settledIdentities = new Map<string, string[]>();
const listeners = new Set<TurnListener>();
let focusedSession = "";
let eventSession: string | undefined;
const key = () => eventSession ?? focusedSession;
function empty(sessionId: string): TurnSnapshot {
  return {active:false, clientId:"", source:"chat", sessionId, startedAt:0, lastEndStatus:"complete", admissionId:"", runId:""};
}
function current(): TurnSnapshot {
  const id = key();
  if (!turns.has(id)) turns.set(id, empty(id));
  return turns.get(id)!;
}
function publish(snapshot: TurnSnapshot, previous: TurnSnapshot): void {
  for (const listener of listeners) {
    try { listener(snapshot, previous); } catch (error) { console.error("[Main Deck] turn subscriber failed", error); }
  }
}
export function focusTurnSession(id: string): void { focusedSession = id; }
/** Scope synchronous event projection without changing the chat the user sees. */
export function withTurnSession(id: string, callback: () => void): void {
  const prior = eventSession; eventSession = id;
  try { callback(); } finally { eventSession = prior; }
}
export function activeTurnSessionIds(): string[] { return [...turns].filter(([, turn]) => turn.active).map(([id]) => id).filter(Boolean); }
export function beginTurn(options: BeginTurnOptions = {}): boolean {
  const id = options.sessionId || key();
  if (eventSession === undefined) focusedSession = id;
  const previous = turns.get(id) || empty(id);
  if (previous.active) return false;
  const snapshot = {...empty(id), active:true, clientId:options.clientId || "", source:options.source || "chat",
    startedAt:Date.now(), admissionId:options.admissionId || "", runId:options.runId || ""};
  turns.set(id, snapshot);publish(snapshot, previous);return true;
}
export function bindTurnIdentity(admissionId = "", runId = ""): void {
  const previous = current();
  if (!admissionId && !runId) return;
  const snapshot = {...previous, admissionId:admissionId || previous.admissionId, runId:runId || previous.runId};
  turns.set(key(), snapshot);publish(snapshot, previous);
}
export function endTurn(options: EndTurnOptions = {}): boolean {
  const previous = current();
  if (!previous.active && !options.force) return false;
  const identities=[...(settledIdentities.get(key()) || []),
    ...(previous.admissionId?[`admission:${previous.admissionId}`]:[]),...(previous.runId?[`run:${previous.runId}`]:[])];
  settledIdentities.set(key(),[...new Set(identities)].slice(-128));
  const snapshot = {...previous, active:false, lastEndStatus:options.status || "complete",
    ...(options.force ? {clientId:"", sessionId:"", admissionId:"", runId:"", startedAt:0} : {})};
  turns.set(key(), snapshot);publish(snapshot, previous);return true;
}
/** Preserve terminal provenance for durable commits, but never restart its live stream. */
export function isSettledTurnEvent(message:{admission_id?:string;run_id?:string}):boolean {
  // Run-only frames cannot distinguish generations of a deliberately resumed
  // logical run; the currently active fresh admission remains their owner.
  if(!message.admission_id && message.run_id && current().active && current().runId===message.run_id)return false;
  const identity=message.admission_id?`admission:${message.admission_id}`:message.run_id?`run:${message.run_id}`:"";
  return !!identity && !!settledIdentities.get(key())?.includes(identity);
}
export function turnMatchesEvent(message: {type?: unknown; client_id?: unknown; source?: unknown; session_id?: unknown; admission_id?: unknown; run_id?: unknown}): boolean {
  const state = current();
  const eventChat = String(message.session_id || ""), source = String(message.source || "");
  if (eventChat && state.sessionId && eventChat !== state.sessionId) return false;
  if (source && source !== state.source) return false;
  const admission = String(message.admission_id || ""), run = String(message.run_id || "");
  const freshStart = message.type === "start" && !state.active;
  if (!freshStart && admission && state.admissionId && admission !== state.admissionId) return false;
  if (!freshStart && run && state.runId && run !== state.runId) return false;
  const client = String(message.client_id || "");
  if (client && state.clientId && client !== state.clientId && !(admission && admission === state.admissionId)) return false;
  if (!client && !source && !eventChat) return state.active && state.source === "chat";
  return true;
}
export const getTurnSnapshot = current;
export function subscribeTurn(listener: TurnListener): () => void { listeners.add(listener);return () => listeners.delete(listener); }
export const turnController = {
  isActive: () => current().active, getClientId: () => current().clientId, getSource: () => current().source,
  getSessionId: () => current().sessionId, begin:beginTurn, end:endTurn, bind:bindTurnIdentity,
  matchesEvent:turnMatchesEvent, subscribe:subscribeTurn, snapshot:getTurnSnapshot,
};
export function __resetTurnStoreForTests(): void {
  const previous = current(); turns.clear(); settledIdentities.clear();focusedSession="";eventSession=undefined;publish(current(), previous);
}
