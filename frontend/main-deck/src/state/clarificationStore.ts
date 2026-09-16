import type {RuntimeContext} from "../types";
import {createModuleStore} from "./createModuleStore";
import {createRequestIdFactory} from "./storePrimitives";
import {getSessionState, subscribeSessions, useSessionState} from "./sessionStore";
import {subscribeTurn} from "./turnStore";

export type ClarificationOption = Readonly<{label: string; description: string}>;
export type ClarificationQuestion = Readonly<{id: string; question: string; header: string; multiSelect: boolean; options: readonly ClarificationOption[]}>;
export type PendingClarification = Readonly<{id: string; chatId: string; runId: string; kind: "clarification" | "goal_input"; questions: readonly ClarificationQuestion[]}>;
export type ClarificationDraft = Readonly<{index: number; selected: Record<string,string[]>; other: Record<string,string>}>;
type State = Readonly<{
  connected: boolean; byChat: Record<string, readonly PendingClarification[]>;
  drafts: Record<string, ClarificationDraft>; submissions: Record<string, {requestId:string; chatId:string}>;
  errors: Record<string,string>; lists: Record<string,{requestId:string; version:number}>; versions: Record<string,number>;
}>;
const initial = (): State => ({connected:false,byChat:{},drafts:{},submissions:{},errors:{},lists:{},versions:{}});
const store = createModuleStore<State>({initialState:initial()});
const requestId = createRequestIdFactory("question");
const timers = new Map<string, ReturnType<typeof setTimeout>>();
const emptyDraft: ClarificationDraft = {index:0,selected:{},other:{}};
let selectedChat = "";

function parseQuestion(value: unknown): ClarificationQuestion | null {
  if (!value || typeof value !== "object") return null;
  const row = value as Record<string, unknown>;
  const id=String(row.id || "").trim(), question=String(row.question || "").trim(), header=String(row.header || "Question").trim();
  if (!id || !question) return null;
  const options = (Array.isArray(row.options) ? row.options : []).flatMap(value => {
    if (!value || typeof value !== "object") return [];
    const option=value as Record<string,unknown>,label=String(option.label || "").trim();
    return label ? [{label,description:String(option.description || "")}] : [];
  });
  return {id,question,header,multiSelect:!!row.multiSelect,options};
}
function parsePending(value: unknown): PendingClarification | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const message = value as Record<string,unknown>;
  const id=String(message.id || ""),chatId=String(message.chat_id || message.session_id || "");
  const questions=(Array.isArray(message.questions) ? message.questions : []).map(parseQuestion).filter((row):row is ClarificationQuestion=>!!row);
  return id && chatId && questions.length ? {id,chatId,runId:String(message.run_id || ""),kind:message.kind==="goal_input" ? "goal_input" : "clarification",questions} : null;
}
function clearTimer(key: string) { clearTimeout(timers.get(key));timers.delete(key); }
function recordQuestions(chatId: string, pending: readonly PendingClarification[], event = false) {
  const state=store.getState(), drafts={...state.drafts}, errors={...state.errors}, submissions={...state.submissions};
  for (const item of pending) {
    const previous=state.byChat[chatId]?.find(row=>row.id===item.id);
    if (previous && JSON.stringify(previous.questions)!==JSON.stringify(item.questions)) {
      drafts[item.id]=emptyDraft;errors[item.id]="The question changed. Review it before answering.";
    }
  }
  for (const old of state.byChat[chatId] || []) {
    if (!pending.some(item=>item.id===old.id)) { delete drafts[old.id];delete errors[old.id];delete submissions[old.id];clearTimer("answer:"+old.id); }
  }
  store.setState({byChat:{...state.byChat,[chatId]:pending},drafts,errors,submissions,
    versions:event ? {...state.versions,[chatId]:(state.versions[chatId] || 0)+1} : state.versions});
}
export function setClarificationContext(context: RuntimeContext) { store.setContext(context); }
export function setClarificationConnection(status: string) {
  const connected=status==="connected";
  if (store.getState().connected===connected) return;
  store.setConnected(connected);
  if (connected) refreshClarification();
  else {
    for (const key of timers.keys()) clearTimer(key);
    store.setState({lists:{},submissions:{}});
  }
}
export function refreshClarification(chatId = getSessionState().displayedSessionId || ""): void {
  const state=store.getState();
  if (!chatId || !state.connected || state.lists[chatId]) return;
  const id=requestId("list"),key="list:"+chatId;
  store.setState({lists:{...state.lists,[chatId]:{requestId:id,version:state.versions[chatId] || 0}},
    errors:{...state.errors,[key]:""}});
  timers.set(key,setTimeout(()=>{
    const state=store.getState();
    if (state.lists[chatId]?.requestId!==id) return;
    const lists={...state.lists};delete lists[chatId];clearTimer(key);
    store.setState({lists,errors:{...state.errors,[key]:"Could not refresh this chat's questions."}});
  },15000));
  if (!store.send({type:"clarification:list",chat_id:chatId,request_id:id})) {
    const lists={...store.getState().lists};delete lists[chatId];clearTimer(key);store.setState({lists});
  }
}
export function ingestClarification(message: Record<string,unknown>): void {
  const type=String(message.type || ""),state=store.getState();
  const chatId=String(message.chat_id || message.session_id || "");
  if (type==="work:event") {
    const event=(message.event || {}) as Record<string,unknown>, aggregate=(event.aggregate || {}) as Record<string,unknown>;
    if (String(aggregate.kind || event.aggregate_kind || "")==="interaction") refreshClarification();
    return;
  }
  if (type==="chat:session" || type==="chat:sessions") { refreshClarification();return; }
  if (type==="clarification:snapshot") {
    const request=state.lists[chatId];
    if (!request || request.requestId!==message.request_id) return;
    const lists={...state.lists};delete lists[chatId];clearTimer("list:"+chatId);store.setState({lists});
    if (request.version!==(state.versions[chatId] || 0)) { refreshClarification(chatId);return; }
    if (!Array.isArray(message.pending)) return;
    const pending=message.pending.map(parsePending).filter((row):row is PendingClarification=>!!row && row.chatId===chatId);
    recordQuestions(chatId,pending);return;
  }
  if (type==="clarification:request") {
    const pending=parsePending(message);
    if (!pending) return;
    const rows=state.byChat[pending.chatId] || [];
    const next=rows.some(row=>row.id===pending.id) ? rows.map(row=>row.id===pending.id ? pending : row) : [...rows,pending];
    recordQuestions(pending.chatId,next,true);return;
  }
  if (type!=="clarification:closed" && type!=="clarification:response:ack") return;
  const id=String(message.id || ""), submission=state.submissions[id];
  if (!chatId || !id || !state.byChat[chatId]?.some(row=>row.id===id)) return;
  if (message.request_id && (!submission || submission.requestId!==message.request_id || submission.chatId!==chatId)) return;
  const status=String(message.status || "resolved");
  const submissions={...state.submissions};delete submissions[id];clearTimer("answer:"+id);
  store.setState({submissions});
  if (["resolved","cancelled","timed_out","skipped"].includes(status)) {
    recordQuestions(chatId,state.byChat[chatId].filter(row=>row.id!==id),true);
  } else {
    store.setState({errors:{...store.getState().errors,[id]:"This question changed before the answer was accepted."}});
    refreshClarification(chatId);
  }
}
export function updateClarificationDraft(id: string, update: ClarificationDraft | ((draft:ClarificationDraft)=>ClarificationDraft)): void {
  const state=store.getState(),chatId=getSessionState().displayedSessionId || "";
  if (!state.byChat[chatId]?.some(row=>row.id===id) || state.submissions[id]) return;
  const draft=typeof update === "function" ? update(state.drafts[id] || emptyDraft) : update;
  store.setState({drafts:{...state.drafts,[id]:draft},errors:{...state.errors,[id]:""}});
}
export function submitClarification(id: string, answers: Record<string,string|string[]>, skipped=false): boolean {
  const state=store.getState(),chatId=getSessionState().displayedSessionId || "";
  if (!state.byChat[chatId]?.some(row=>row.id===id) || state.submissions[id]) return false;
  if (!state.connected) {
    store.setState({errors:{...state.errors,[id]:"Reconnect to send this answer. Your draft is kept."}});return false;
  }
  const correlation=requestId("answer"),key="answer:"+id;
  store.setState({submissions:{...state.submissions,[id]:{requestId:correlation,chatId}},errors:{...state.errors,[id]:""}});
  timers.set(key,setTimeout(()=>{
    const state=store.getState();
    if (state.submissions[id]?.requestId!==correlation) return;
    const submissions={...state.submissions};delete submissions[id];clearTimer(key);
    store.setState({submissions,errors:{...state.errors,[id]:"The answer was not acknowledged. Refresh before retrying."}});
    refreshClarification(chatId);
  },15000));
  if (!store.send({type:"clarification:response",chat_id:chatId,request_id:correlation,id,answers,skipped})) {
    clearTimer(key);const submissions={...store.getState().submissions};delete submissions[id];store.setState({submissions});return false;
  }
  return true;
}
subscribeSessions(()=>{
  const id=getSessionState().displayedSessionId || "";
  if (selectedChat!==id) { selectedChat=id;refreshClarification(id); }
});
subscribeTurn((snapshot,previous)=>{
  if (previous.active && !snapshot.active) refreshClarification(snapshot.sessionId || previous.sessionId);
});
export function useClarificationState() {
  const state=store.useStore(),chatId=useSessionState().displayedSessionId || "";
  const pending=state.byChat[chatId]?.[0] || null;
  return {pending,connected:state.connected,submitting:!!(pending && state.submissions[pending.id]),
    draft:pending ? state.drafts[pending.id] || emptyDraft : emptyDraft,
    error:pending ? state.errors[pending.id] || "" : state.errors["list:"+chatId] || ""};
}
export function useQuestionChatIds(): string[] {
  const state=store.useStore();return Object.keys(state.byChat).filter(id=>state.byChat[id].length);
}
export const getClarificationState = store.getState;
export function __resetClarificationForTests() {
  for (const key of timers.keys()) clearTimer(key);
  selectedChat="";store.setContext(null);store.replaceState(initial());
}
