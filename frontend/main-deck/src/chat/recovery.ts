import {getChatState,patchChatState,withCachedChatState,getDisplayedChatState,cachedChatStates} from "./stateCore";

const checkpoints=new Map<string,Readonly<Record<string,unknown>>>();
const dismissed=new Map<string,string>();
function identity(task:Readonly<Record<string,unknown>>):string {
  return JSON.stringify([task.task_id,task.updated_at]);
}
export function recordRecoveryNotice(task:Readonly<Record<string,unknown>>|undefined):void {
  const owner=String(task?.session_id || task?.chat_id || "");
  if(!task || !owner || typeof task.task_id!=="string" || !task.task_id)return;
  const previous=checkpoints.get(owner);
  if(previous && Number(previous.updated_at)>Number(task.updated_at))return;
  if(checkpoints.size>=64 && !checkpoints.has(owner))checkpoints.delete(checkpoints.keys().next().value!);
  checkpoints.set(owner,task);
  withCachedChatState(owner,restoreRecoveryNotice);
}
export function restoreRecoveryNotice():void {
  const state=getChatState(),id=state.sessionId;
  if(!id)return;
  const task=checkpoints.get(id);
  patchChatState({orphanedTask:task && dismissed.get(id)!==identity(task) && !state.turnActive?task:null});
}
export function dismissRecoveryNotice():void {
  const state=getChatState();
  if(state.sessionId && state.orphanedTask) {
    if(dismissed.size>=64 && !dismissed.has(state.sessionId))dismissed.delete(dismissed.keys().next().value!);
    dismissed.set(state.sessionId,identity(state.orphanedTask));
  }
  patchChatState({orphanedTask:null});
}
export function resetRecoveryNotices():void {checkpoints.clear();dismissed.clear();}
export function invalidateRecoveryNotices():void {
  checkpoints.clear();
  const states=new Map([getDisplayedChatState(),...cachedChatStates()].map(state=>[state.sessionId,state]));
  for(const state of states.values())if(state.sessionId && state.orphanedTask)withCachedChatState(state.sessionId,()=>patchChatState({orphanedTask:null}));
}
