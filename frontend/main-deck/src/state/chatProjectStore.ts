import {createModuleStore} from "./createModuleStore";
import type {RuntimeContext} from "../types";

export type ChatProject = {root: string; name: string};
const store = createModuleStore<{projects: Record<string, ChatProject | null>; pending: Record<string,string>; errors: Record<string,string>}>({initialState:{projects:{},pending:{},errors:{}}});
export const useChatProjects = store.useStore;
export const getChatProjects = store.getState;
export const setChatProjectContext = (ctx: RuntimeContext) => store.setContext(ctx);
export function parseChatProject(value: unknown): ChatProject | null {
  const p = value as Partial<ChatProject> | null;
  return p && typeof p.root === "string" && p.root ? {root:p.root,name:String(p.name || p.root.split(/[\\/]/).pop() || p.root)} : null;
}
export function ingestChatProjects(message: Record<string,unknown>): void {
  const type = message.type;
  if(type === "chat:project:result") {
    const id=String(message.chat_id || ""), state=store.getState();
    if(state.pending[id] !== message.request_id)return;
    const pending={...state.pending};delete pending[id];
    const error=message.error as {message?:string}|undefined;
    store.setState({pending,errors:{...state.errors,[id]:message.ok ? "" : error?.message || "Project could not be selected"},
      ...(message.ok ? {projects:{...state.projects,[id]:parseChatProject(message.project)}} : {})});
  } else if(type === "chat:session" || type === "chat:sessions") {
    const rows=type === "chat:session" ? [message.session] : message.items;
    if(!Array.isArray(rows))return;
    const projects={...store.getState().projects};
    for(const value of rows) { const row=value as Record<string,unknown>|null;if(row?.id && "project" in row)projects[String(row.id)]=parseChatProject(row.project); }
    store.setState({projects});
  }
}
export async function chooseChatProject(chatId: string): Promise<void> {
  if(!chatId || store.getState().pending[chatId])return;
  const root=await window.variant1Deck?.pickFolder?.();
  if(root)setChatProject(chatId,root);
}
export function setChatProject(chatId:string,root:string|null):boolean {
  if(!chatId || store.getState().pending[chatId])return false;
  const request_id=globalThis.crypto.randomUUID();
  store.setState({pending:{...store.getState().pending,[chatId]:request_id},errors:{...store.getState().errors,[chatId]:""}});
  const finish=(error:string)=>{if(store.getState().pending[chatId]!==request_id)return;const pending={...store.getState().pending};delete pending[chatId];store.setState({pending,errors:{...store.getState().errors,[chatId]:error}});};
  if(!store.send({type:"chat:project:set",chat_id:chatId,root,request_id})){finish("Backend disconnected");return false;}
  setTimeout(()=>finish("No project acknowledgement received. Select the chat again to refresh."),15000);
  return true;
}
