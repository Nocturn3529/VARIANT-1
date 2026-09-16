import {useEffect,useRef,useState} from "react";
import {useSurfaceDocument} from "../ui/SurfaceDocument";
import {getPeerState,peerRequestKey,requestPeer,usePeers} from "./peerStore";

/** Mounted only by open overlays. Push invalidations coalesce; hidden views do no work. */
export function usePeerRefresh(chatId:string,kind:"messages"|"grok") {
  const state=usePeers(),owner=useSurfaceDocument();
  const [visibility,setVisibility]=useState(0);
  const applied=useRef("");
  const changed=(kind==="grok" ? state.scopes[chatId]?.grokChanged : state.scopes[chatId]?.changed) || 0;
  const operations=kind==="grok" ? ["grok:status"] as const : ["list","inbox"] as const;
  const pending=operations.some(operation=>state.requests[peerRequestKey(chatId,operation==="inbox" ? {operation,direction:"all",limit:100} : {operation})]?.phase==="pending");
  useEffect(()=>{
    const refresh=()=>setVisibility(value=>value+1);
    owner.addEventListener("visibilitychange",refresh);
    return ()=>owner.removeEventListener("visibilitychange",refresh);
  },[owner]);
  useEffect(()=>{
    if(!state.connected){applied.current="";return;}
    const key=JSON.stringify([chatId,changed,visibility]);
    if(owner.hidden || pending || applied.current===key)return;
    const timer=setTimeout(()=>{
      applied.current=key;
      if(kind==="grok")requestPeer(chatId,{operation:"grok:status"});
      else {
        requestPeer(chatId,{operation:"list"});
        requestPeer(chatId,{operation:"inbox",direction:"all",limit:100});
        const scope=getPeerState().scopes[chatId];
        if(scope?.cursor)requestPeer(chatId,{operation:"inbox",after:scope.cursor,direction:"all",limit:100});
        for(const id of scope?.changedIds || [])requestPeer(chatId,{operation:"inspect",message_id:id});
      }
    },120);
    return ()=>clearTimeout(timer);
  },[chatId,kind,state.connected,changed,visibility,owner,pending]);
}
