import {useEffect,useRef,useState} from "react";
import {createPortal} from "react-dom";
import {Overlay,OverlayHeader} from "../ui/Overlay";
import {useSurfaceDocument} from "../ui/SurfaceDocument";
import {refreshExecution,selectTerminal,useTerminalState} from "../context/terminalStore";
import {peerRequestKey,requestPeer,setGrokLaunchDraft,usePeers} from "./peerStore";
import {grokBindingError,parseGrokBinding} from "./grokBindings";
import {record} from "./peerModels";
import {grokDeliveryLabel} from "./grokSessions";
import {GrokDeliveryControl} from "./GrokDeliveryControl";
import {usePeerRefresh} from "./usePeerRefresh";
import {chatPaneId,revealPane} from "../workbench/workbenchStore";
import {detachedChatId} from "../runtime/viewIdentity";

function GrokDialog({chatId,onClose}:{chatId:string;onClose:()=>void}) {
  const peers=usePeers(),terminals=useTerminalState(chatId);
  const [actionId,setActionId]=useState<string|null>(null);
  const [openTerminal,setOpenTerminal]=useState("");
  const [terminalNotice,setTerminalNotice]=useState("");
  const applied=useRef(""),appliedSetup=useRef("");
  const draft=peers.grokLaunchDrafts[chatId] || {sessionId:"",cwd:""};
  const saved=peers.grokSessions[chatId];
  const savedDirectory=saved?.items.find(row=>row.session_id===draft.sessionId)?.cwd || "";
  const sessions=peers.requests[peerRequestKey(chatId,{operation:"grok:sessions"})];
  const nextPage=saved?.cursor ? peers.requests[peerRequestKey(chatId,{operation:"grok:sessions",cursor:saved.cursor})] : undefined;
  const status=peers.requests[peerRequestKey(chatId,{operation:"grok:status"})];
  const setup=peers.requests[peerRequestKey(chatId,{operation:"grok:setup"})];
  const launch=peers.requests[peerRequestKey(chatId,{operation:"grok:launch"})];
  const action=actionId ? Object.values(peers.requests).find(entry=>entry.chatId===chatId && entry.requestId===actionId) : undefined;
  const result=status?.result;
  const valid=result?.adapter==="grok-peer-bridge" && Array.isArray(result.items);
  const installationProfile=typeof result?.installation_profile_id==="string" ? result.installation_profile_id : "";
  const profileMismatch=!!installationProfile && installationProfile!==result?.profile_id;
  const bindings=valid ? (result.items as unknown[]).flatMap(raw=>{const binding=parseGrokBinding(raw,chatId);return binding?[binding]:[];}) : [];
  const readPending=status?.phase==="pending";
  const launchPending=launch?.phase==="pending" || launch?.phase==="unconfirmed";
  const setupPending=setup?.phase==="pending" || setup?.phase==="unconfirmed";
  const canPresentTerminal=!detachedChatId();
  usePeerRefresh(chatId,"grok");
  useEffect(()=>{if(peers.connected){requestPeer(chatId,{operation:"grok:sessions"});refreshExecution(chatId);}},[chatId,peers.connected]);
  useEffect(()=>{
    if(setup?.phase!=="accepted" || appliedSetup.current===setup.requestId)return;
    appliedSetup.current=setup.requestId;
    requestPeer(chatId,{operation:"grok:status"});requestPeer(chatId,{operation:"grok:sessions"});
  },[setup,chatId]);
  useEffect(()=>{
    if(!action || action.phase!=="accepted" || applied.current===action.requestId)return;
    applied.current=action.requestId;
    const binding=parseGrokBinding(action.result,chatId);
    if(binding?.terminal_id && (!binding.terminal_chat_id || binding.terminal_chat_id===chatId)) {
      setOpenTerminal(binding.terminal_id);setTerminalNotice("");refreshExecution(chatId);
    }
    requestPeer(chatId,{operation:"grok:status"});
  },[action,chatId]);
  useEffect(()=>{
    if(!openTerminal)return;
    const timer=setTimeout(()=>{setOpenTerminal("");setTerminalNotice("The terminal is not available in this chat yet. Refresh to check again.");},6000);
    return ()=>clearTimeout(timer);
  },[openTerminal]);
  useEffect(()=>{if(openTerminal && terminals.terminals.some(row=>row.id===openTerminal)){
    selectTerminal(openTerminal,chatId);setOpenTerminal("");
    if(canPresentTerminal){revealPane(chatPaneId("terminal",chatId),"bottom");onClose();}
    else setTerminalNotice("The terminal is ready. Open this chat in the main window to view its terminal.");
  }},[openTerminal,terminals.terminals,chatId,canPresentTerminal,onClose]);
  const refresh=()=>{requestPeer(chatId,{operation:"grok:status"});requestPeer(chatId,{operation:"grok:sessions"});refreshExecution(chatId);};
  return <Overlay className="grok-peer-dialog" labelledBy="grok-peer-title" onClose={onClose}>
    <OverlayHeader title="Grok Build" id="grok-peer-title" onClose={onClose}/>
    <div className="grok-peer-dialog__body">
      <p>Start Grok or resume a saved session in a built-in terminal. Resuming the same native session keeps its peer identity; a new session has a separate identity.</p>
      <form className="grok-launch-form" onSubmit={event=>{
        event.preventDefault();
        if(!peers.connected || !valid || result.installed!==true || result.available!==true || profileMismatch || launchPending || setupPending)return;
        setActionId(requestPeer(chatId,{operation:"grok:launch",...(draft.sessionId ? {session_id:draft.sessionId} : draft.cwd.trim() ? {cwd:draft.cwd.trim()} : {})}));
      }}>
        <label>Session<select aria-label="Grok session" value={draft.sessionId} disabled={launchPending} onChange={event=>{
          setGrokLaunchDraft(chatId,{...draft,sessionId:event.target.value});
        }}><option value="">New session</option>
          {draft.sessionId && !saved?.items.some(row=>row.session_id===draft.sessionId) ? <option value={draft.sessionId}>{draft.sessionId} · saved selection</option>:null}
          {(saved?.items || []).map(row=><option key={row.session_id} value={row.session_id}>{row.title}{row.cwd ? ` · ${row.cwd}` : ""}</option>)}
        </select></label>
        <label>{draft.sessionId ? "Saved session directory" : "Directory"}<input aria-label="Grok directory" value={draft.sessionId ? savedDirectory : draft.cwd} readOnly={!!draft.sessionId} disabled={launchPending} placeholder={draft.sessionId ? "Resolved from the saved session" : "Use this chat’s project directory"} onChange={event=>{if(!draft.sessionId)setGrokLaunchDraft(chatId,{...draft,cwd:event.target.value});}}/></label>
        {draft.sessionId ? <p>Grok resumes this session in its saved directory.</p>:null}
        <div className="grok-peer-dialog__actions"><button type="submit" className="grok-launch-primary" disabled={!peers.connected || !valid || result.installed!==true || result.available!==true || profileMismatch || launchPending || setupPending}>{launch?.phase==="pending" ? "Opening terminal…" : draft.sessionId ? "Open saved session in terminal" : "Launch Grok in terminal"}</button>
          <button type="button" disabled={!peers.connected || sessions?.phase==="pending" || readPending} onClick={refresh}>{readPending || sessions?.phase==="pending" ? "Refreshing…" : "Refresh"}</button>
        </div>
        {saved?.cursor ? <button type="button" disabled={!peers.connected || sessions?.phase==="pending" || nextPage?.phase==="pending"} onClick={()=>requestPeer(chatId,{operation:"grok:sessions",cursor:saved.cursor!})}>{nextPage?.phase==="pending" ? "Loading sessions…" : "More saved sessions"}</button>:null}
        {sessions?.phase==="accepted" && !saved?.items.length ? <p>No saved sessions found.</p>:null}
      </form>
      <div className="grok-bridge-setup"><span>{profileMismatch ? "Adapter belongs to another profile" : valid && result.installed===true ? "Shared bridge installed" : "Shared bridge setup required"}</span>
        <button type="button" disabled={!peers.connected || !valid || setupPending || launchPending || (profileMismatch && !result?.profile_id)} onClick={()=>requestPeer(chatId,{operation:"grok:setup",...(profileMismatch ? {replace_profile_id:installationProfile} : {})})}>{setup?.phase==="pending" ? "Setting up…" : profileMismatch ? "Switch adapter to this profile" : result?.installed===true ? "Repair bridge setup" : "Set up bridge"}</button>
      </div>
      {profileMismatch ? <p>Future or reloaded Grok MCP sessions will use this profile. Currently running connections keep their existing profile.</p>:null}
      {!peers.connected ? <p role="status">Backend disconnected. Your session and directory selection are retained.</p>:null}
      {[status,sessions,nextPage,setup,action].map((entry,index)=>entry?.error ? <p key={index} role="alert">{entry.error.message}</p>:null)}
      {launch?.phase==="unconfirmed" ? <p role="status">Launch confirmation is unavailable. Refresh connections before opening another terminal.</p>:null}
      {setup?.phase==="unconfirmed" ? <p role="status">Setup confirmation is unavailable. Refresh bridge status before trying again.</p>:null}
      {openTerminal ? <p role="status">Finding the built-in terminal…</p>:null}
      {terminalNotice ? <p role="status">{terminalNotice}</p>:null}
      {status?.phase==="accepted" && result?.available===false ? <p role="status">Grok is unavailable on this device.</p>:null}
      <h2 className="grok-connections-title">Connected sessions</h2>
      {valid && !bindings.length ? <p>No Grok connections are registered.</p>:null}
      {bindings.map(binding=>{
        const reconnect=peers.requests[peerRequestKey(chatId,{operation:"grok:connect",binding_id:binding.binding_id})];
        const delivery=grokDeliveryLabel(binding.delivery_mode,binding.automatic_wake_available);
        const localTerminal=!!binding.terminal_id && (!binding.terminal_chat_id || binding.terminal_chat_id===chatId) && terminals.terminals.some(row=>row.id===binding.terminal_id);
        return <article key={binding.binding_id} className="grok-peer-binding" data-binding-id={binding.binding_id}>
          <header><strong>Grok Build</strong><span>{binding.status}</span></header>
          <p className="grok-delivery-mode"><strong>{delivery.label}</strong><br/>{delivery.detail}</p>
          <GrokDeliveryControl chatId={chatId} binding={binding}/>
          <dl><dt>Native session</dt><dd>{binding.session_id || "Not yet confirmed"}</dd><dt>Peer</dt><dd>{binding.peer_id || "Not yet registered"}</dd>
            {binding.session_activity!=="unknown" ? <><dt>Activity</dt><dd>{binding.session_activity}</dd></>:null}
            {binding.terminal_id ? <><dt>Terminal</dt><dd>{binding.terminal_id}{!localTerminal ? " · not in this chat" : ""}</dd></>:null}
          </dl>
          {grokBindingError(binding.error) ? <p role="alert">{grokBindingError(binding.error)}</p>:null}
          {binding.pending_permissions.map(record).filter(permission=>permission.binding_id===binding.binding_id && permission.session_id===binding.session_id && typeof permission.permission_id==="string").map(permission=><GrokPermission key={String(permission.permission_id)} chatId={chatId} bindingId={binding.binding_id} permission={permission}/>)}
          <div className="grok-peer-dialog__actions">
            <button disabled={!peers.connected || reconnect?.phase==="pending" || reconnect?.phase==="unconfirmed"}
              onClick={()=>setActionId(requestPeer(chatId,{operation:"grok:connect",binding_id:binding.binding_id}))}>{reconnect?.phase==="pending" ? "Reconnecting…" : "Reconnect"}</button>
            <button disabled={!localTerminal || !canPresentTerminal} title={!canPresentTerminal ? "View terminal panes in the main window" : localTerminal ? "Open this chat’s terminal" : "No matching terminal in this chat"} onClick={()=>{if(localTerminal && canPresentTerminal){selectTerminal(binding.terminal_id,chatId);revealPane(chatPaneId("terminal",chatId),"bottom");onClose();}}}>Show terminal</button>
          </div>
          {reconnect?.error ? <p role="alert">{reconnect.error.message}</p>:null}
        </article>;
      })}
    </div>
  </Overlay>;
}

function GrokPermission({chatId,bindingId,permission}:{chatId:string;bindingId:string;permission:Record<string,unknown>}) {
  const state=usePeers(),permissionId=String(permission.permission_id);
  const request=state.requests[peerRequestKey(chatId,{operation:"grok:permission",binding_id:bindingId,permission_id:permissionId,option_id:""})];
  const options=Array.isArray(permission.options) ? permission.options.map(record).filter(option=>typeof option.optionId==="string" && typeof option.name==="string") : [];
  const finished=request?.phase==="accepted",blocked=finished || request?.phase==="pending" || request?.phase==="unconfirmed";
  useEffect(()=>{if(finished)requestPeer(chatId,{operation:"grok:status"});},[finished,chatId]);
  return <section className="grok-peer-permission"><strong>{finished ? "Answer accepted" : "Grok requests permission"}</strong>
    <details><summary>Requested action</summary><pre>{JSON.stringify(permission.tool_call,null,2)}</pre></details>
    <div className="grok-peer-dialog__actions">{options.map(option=><button key={String(option.optionId)} type="button" disabled={!state.connected || blocked} onClick={()=>requestPeer(chatId,{operation:"grok:permission",binding_id:bindingId,permission_id:permissionId,option_id:String(option.optionId)})}>{String(option.name)}</button>)}</div>
    {request?.error ? <p role="status">{request.error.message}</p>:null}
  </section>;
}

export function GrokControl({chatId,terminalId,label}:{chatId:string;terminalId?:string;label?:string}) {
  const owner=useSurfaceDocument();const [open,setOpen]=useState(false);
  const peers=usePeers(),status=peers.requests[peerRequestKey(chatId,{operation:"grok:status"})];
  const items=status?.result?.items;
  const binding=terminalId && Array.isArray(items) ? items.map(item=>parseGrokBinding(item,chatId)).find(item=>item?.terminal_id===terminalId) : undefined;
  return <><button title={binding ? `Grok peer session ${binding.session_id}; the terminal may display another session` : "Connected Grok sessions"} disabled={!chatId} onClick={()=>setOpen(true)}>{label || `Grok${binding ? ` · ${binding.status}` : ""}`}</button>
    {open ? createPortal(<GrokDialog key={chatId} chatId={chatId} onClose={()=>setOpen(false)}/>,owner.body):null}</>;
}
