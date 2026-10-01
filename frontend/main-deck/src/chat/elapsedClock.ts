import {useSyncExternalStore} from "react";

const listeners=new Set<()=>void>();
let now=Date.now(),timer:number|undefined;
function subscribe(listener:()=>void) {
  listeners.add(listener);
  if(timer===undefined){now=Date.now();timer=window.setInterval(()=>{now=Date.now();for(const callback of listeners)callback();},1000);}
  return ()=>{listeners.delete(listener);if(!listeners.size && timer!==undefined){window.clearInterval(timer);timer=undefined;}};
}
const inactive=()=>()=>{};
const snapshot=()=>now;
const stopped=()=>0;
export function useElapsed(active:boolean,startedAt:number):number {
  const value=useSyncExternalStore(active?subscribe:inactive,active?snapshot:stopped,stopped);
  return active?Math.max(0,value-startedAt):0;
}
