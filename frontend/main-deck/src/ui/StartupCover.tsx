import {StartupCells} from "../motion/StartupCells";
import {useEffect,useLayoutEffect,useState} from "react";
import {useChatState} from "../chatStore";
import {useSessionState} from "../state/sessionStore";

export function StartupCover() {
  const chat=useChatState(),sessions=useSessionState();
  const [finished,setFinished]=useState(!window.variant1Deck?.getBackendInfo || new URLSearchParams(window.location.search).has("fixture"));
  const [slow,setSlow]=useState(false);
  useLayoutEffect(()=>{document.getElementById("variant1-boot")?.remove();},[]);
  useLayoutEffect(()=>{document.body.dataset.startup=finished ? "ready" : "loading";return()=>{delete document.body.dataset.startup;};},[finished]);
  useEffect(()=>{if(chat.connected && !sessions.loading && (sessions.displayedSessionId || !sessions.items.length))setFinished(true);},[chat.connected,sessions.loading,sessions.displayedSessionId,sessions.items.length]);
  useEffect(()=>{if(finished)return;const timer=setTimeout(()=>setSlow(true),30000);return()=>clearTimeout(timer);},[finished]);
  if(finished)return null;
  return <div className="startup-cover" role="status" aria-live="polite">
    <StartupCells/>
    <strong>VARIANT-1</strong><div className="startup-cover__line">Loading</div>
    {slow ? <><p>{"Startup is taking longer than usual. You can inspect the connection while it continues."}</p><button onClick={()=>setFinished(true)}>Show connection details</button></>:null}
  </div>;
}
