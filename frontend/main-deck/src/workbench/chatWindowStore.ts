import {createExternalStore} from "../state/createModuleStore";
import type {RuntimeApi} from "../types";
type ChatWindow={chat_id:string;title:string;focused:boolean};
const store=createExternalStore<{revision:number;windows:ChatWindow[];error:string}>({revision:-1,windows:[],error:""});
export const useChatWindows=store.useStore;
export function ingestChatWindows(raw:unknown):void {
  const value=raw as {revision?:number;windows?:ChatWindow[]}|null;
  if(!value || !Number.isSafeInteger(value.revision) || (value.revision ?? -1)<store.getState().revision || !Array.isArray(value.windows))return;
  store.replaceState({revision:value.revision!,windows:value.windows.filter(row=>row && typeof row.chat_id==="string" && typeof row.title==="string"),error:""});
}
export function installChatWindowInventory(api:RuntimeApi|null):()=>void {
  let active=true;
  const stop=api?.onChatWindowsChanged?.(value=>{if(active)ingestChatWindows(value);});
  void api?.listChatWindows?.().then(value=>{if(active)ingestChatWindows(value);}).catch(()=>{if(active)store.setState({error:"Could not read detached chat windows."});});
  return()=>{active=false;stop?.();};
}
