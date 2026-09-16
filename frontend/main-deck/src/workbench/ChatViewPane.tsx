import {useEffect,useRef,useState} from "react";
import {createPortal} from "react-dom";
import {createExternalStore} from "../state/createModuleStore";
import {closePane} from "./workbenchStore";
import {openPalette} from "../state/paletteStore";

const slots=new Map<string,HTMLElement>();
const revision=createExternalStore(0);
export function ChatViewPane({chatId}:{chatId:string;title:string}) {
  const element=useRef<HTMLDivElement>(null);
  useEffect(()=>{
    if(!element.current)return;const slot=element.current;slots.set(chatId,slot);revision.replaceState(revision.getState()+1);
    return()=>{if(slots.get(chatId)===slot)slots.delete(chatId);revision.replaceState(revision.getState()+1);};
  },[chatId]);
  return <div ref={element} className="workbench-chat-slot" data-chat-view-id={chatId}/>;
}

/** Keep each browsing context alive while its layout slot moves or becomes a tab. */
export function PersistentChatViews({views,dragging}:{views:readonly {id:string;title:string}[];dragging:boolean}) {
  const version=revision.useStore();
  const [rects,setRects]=useState<Record<string,{left:number;top:number;width:number;height:number}>>({});
  useEffect(()=>{
    let frame=0;
    const measure=()=>{
      frame=0;const next:typeof rects={};
      for(const [id,slot] of slots){const rect=slot.getBoundingClientRect();if(slot.isConnected&&rect.width>2&&rect.height>2)next[id]={left:rect.left,top:rect.top,width:rect.width,height:rect.height};}
      setRects(previous=>JSON.stringify(previous)===JSON.stringify(next)?previous:next);
    };
    const schedule=()=>{if(!frame)frame=requestAnimationFrame(measure);};
    const resize=new ResizeObserver(schedule);for(const slot of slots.values())resize.observe(slot);
    const mutation=new MutationObserver(schedule);mutation.observe(document.body,{attributes:true,subtree:true,attributeFilter:["style","class","hidden"]});
    window.addEventListener("resize",schedule);window.addEventListener("scroll",schedule,true);measure();
    return()=>{cancelAnimationFrame(frame);resize.disconnect();mutation.disconnect();window.removeEventListener("resize",schedule);window.removeEventListener("scroll",schedule,true);};
  },[version]);
  return createPortal(<>{views.map(view=><iframe key={view.id} className="workbench-chat-frame" title={`Chat: ${view.title}`}
    style={rects[view.id] ? {...rects[view.id],visibility:"visible",pointerEvents:dragging?"none":"auto"} : {visibility:"hidden",pointerEvents:"none"}}
    src={`variant1://app/frontend/main-deck/index.html?detached_chat=${encodeURIComponent(view.id)}&docked=1`}
    onLoad={event=>{
      try {const child=event.currentTarget.contentWindow;if(!child||child.location.origin!==location.origin)return;
        child.document.addEventListener("keydown",e=>{if((e.ctrlKey||e.metaKey)&&!e.shiftKey&&["k","w"].includes(e.key.toLowerCase())){e.preventDefault();e.stopPropagation();if(e.key.toLowerCase()==="w")closePane(`chatview:${view.id}`);else openPalette();}},true);
      }catch{/* Only the trusted app frame participates in layout shortcuts. */}
    }} allow="microphone"/>)}</>,document.body);
}
