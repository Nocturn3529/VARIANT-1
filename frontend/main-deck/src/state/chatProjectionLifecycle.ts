/** Coordinate frontend projections only; this never retires a backend session. */
type Reason="idle"|"deleted";
const cleaners=new Set<(chatId:string,reason:Reason)=>void>();
const retainers=new Set<(chatId:string)=>boolean>();
export function registerChatProjectionCleanup(clean:(chatId:string,reason:Reason)=>void):void {cleaners.add(clean);}
export function registerChatProjectionRetention(retain:(chatId:string)=>boolean):void {retainers.add(retain);}
export function canReleaseChatProjection(chatId:string):boolean {
  for(const retain of retainers)try{if(retain(chatId))return false;}catch{return false;}
  return true;
}
export function releaseChatProjection(chatId:string,reason:Reason):void {
  for(const clean of cleaners)try{clean(chatId,reason);}catch(error){console.error("Chat projection cleanup failed",error);}
}
