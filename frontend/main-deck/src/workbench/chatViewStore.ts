import {createExternalStore} from "../state/createModuleStore";
import {revealChatPane} from "./workbenchStore";
type ChatView={id:string;title:string};
const KEY="variant1.workbench.chat-views.v1";
function read():ChatView[]{try{const rows=JSON.parse(window.localStorage.getItem(KEY)||"[]");return Array.isArray(rows)?rows.filter(r=>r&&typeof r.id==="string"&&/^[\w.:-]{1,256}$/.test(r.id)&&typeof r.title==="string"):[];}catch{return [];}}
const store=createExternalStore<ChatView[]>(read());
export const useChatViews=store.useStore;
export const getChatViews=store.getState;
export const chatViewId=(id:string)=>`chatview:${id}`;
export function registerChatView(id:string,title:string):string {
  const current=store.getState();
  const next=current.some(row=>row.id===id) ? current.map(row=>row.id===id ? {...row,title} : row) : [...current,{id,title}];
  store.replaceState(next);try{window.localStorage.setItem(KEY,JSON.stringify(next));}catch{/* optional */}
  return chatViewId(id);
}
export function unregisterChatView(id:string):void {
  const next=store.getState().filter(row=>row.id!==id);
  store.replaceState(next);try{window.localStorage.setItem(KEY,JSON.stringify(next));}catch{/* optional */}
}
export function openChatView(id:string,title:string,placement:"right"|"bottom"|"center"="right"):void {
  revealChatPane(registerChatView(id,title),placement);
}
