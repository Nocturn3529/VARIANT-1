import {useEffect,useState} from "react";
import {useSessionState,refreshSessions} from "./state/sessionStore";
import {Button} from "./ui/Button";
import {SettingsSection} from "./ui/Settings";
import {captureContext,changeContextFilter,chooseContextChat,chooseContextChild,chooseContextView,contextExportError,expandContextSource,exportContextView,getExternalContextSettings,refreshContextChildren,refreshContextViews,turnContextDetail,turnContextPage,useExternalContextSettings} from "./externalContextSettingsStore";
const kindNames:Record<string,string>={message:"Message",cell:"Python cell",snapshot:"Runtime snapshot"};
function viewLabel(view:{view_id:string;created_at?:string|number}){const value=view.created_at;const date=new Date(typeof value==="number"?value*1000:value || "");return Number.isNaN(date.getTime())?view.view_id:date.toLocaleString();}
export function ExternalContextSettings(){
  const state=useExternalContextSettings(),sessions=useSessionState(),[query,setQuery]=useState(""),[childDraft,setChildDraft]=useState(""),[format,setFormat]=useState<"jsonl"|"markdown">("jsonl"),[choosingPath,setChoosingPath]=useState(false);
  useEffect(()=>{if(state.connected){refreshSessions();if(state.chatId){refreshContextViews();refreshContextChildren();}}},[state.connected]);
  useEffect(()=>{if(!state.chatId && sessions.displayedSessionId)chooseContextChat(sessions.displayedSessionId);},[state.chatId,sessions.displayedSessionId]);
  useEffect(()=>setQuery(state.query),[state.chatId,state.viewId]);
  useEffect(()=>setChildDraft(state.childId),[state.chatId,state.childId]);
  const locked=choosingPath || !!state.pending.export,busyView=!!state.pending.view;
  const selected=state.items.find(row=>row.source_id===state.sourceId),counts=(state.status?.counts || {}) as Record<string,unknown>;
  async function exportView(){
    const chosen=getExternalContextSettings(),api=window.variant1Deck;
    if(!api?.chooseContextExportPath){contextExportError("Native save dialog is unavailable in this window.");return;}
    setChoosingPath(true);
    try{const result=await api.chooseContextExportPath(format);if(result.cancelled)return;
      if(!result.path || result.ok===false){contextExportError(result.error || "Export destination could not be selected.");return;}
      const current=getExternalContextSettings();if(current.chatId!==chosen.chatId || current.childId!==chosen.childId || current.viewId!==chosen.viewId){contextExportError("The selected context changed. Choose the export destination again.");return;}
      exportContextView(result.path,format,result.overwrite===true);
    }catch(error){contextExportError(String(error));}finally{setChoosingPath(false);}
  }
  return <div className="external-context-settings">
    <SettingsSection title="Source and view" description="Browse retained session evidence on demand. Views remain frozen until explicitly refreshed; reading does not run historical actions.">
      <div className="context-controls"><label>Session<select aria-label="Context session" value={state.chatId} disabled={locked || !state.connected} onChange={event=>chooseContextChat(event.target.value)}><option value="">Choose session</option>{sessions.items.map(row=><option key={row.id} value={row.id}>{row.title || "Untitled chat"}</option>)}</select></label>
      <Button disabled={!state.connected || !state.chatId || busyView || locked} onClick={()=>captureContext()}>Capture current context</Button>
      <Button disabled={!state.connected || !state.chatId || state.pending.views || locked} onClick={()=>refreshContextViews()}>Refresh saved views</Button></div>
      <div className="context-controls"><label>Evidence source<select aria-label="Context evidence owner" value={state.childId} disabled={locked || busyView || !state.connected || !state.chatId} onChange={event=>chooseContextChild(event.target.value)}><option value="">This session</option>{state.childId && !state.children.some(child=>child.child_id===state.childId)?<option value={state.childId}>{state.childId}</option>:null}{state.children.map(child=><option key={child.child_id} value={child.child_id}>{child.name || child.child_id}{child.status?` · ${child.status}`:""}</option>)}</select></label><Button disabled={locked || !state.connected || !state.chatId || !!state.pending.children} onClick={()=>refreshContextChildren()}>Refresh children</Button></div>
      {state.childrenTruncated?<p className="context-coverage">Showing the first 100 owned children. Use a retained child ID to open an older child's evidence.</p>:null}
      <details><summary>Open an owned child by ID</summary><form className="context-controls" onSubmit={event=>{event.preventDefault();chooseContextChild(childDraft.trim());}}><input aria-label="Owned context child ID" maxLength={512} value={childDraft} onChange={event=>setChildDraft(event.target.value)} disabled={locked || !state.connected}/><Button type="submit" disabled={locked || !state.connected || !state.chatId || !childDraft.trim()}>Use child</Button></form></details>
      <div className="context-controls"><label>Saved view<select aria-label="Saved context view" value={state.viewId} disabled={locked || busyView || !state.connected} onChange={event=>chooseContextView(event.target.value)}><option value="">Choose a frozen view</option>{state.viewId && !state.views.some(row=>row.view_id===state.viewId)?<option value={state.viewId}>{state.viewId}</option>:null}{state.views.map(view=><option key={view.view_id} value={view.view_id}>{viewLabel(view)}{view.child_id?" · child evidence":""}</option>)}</select></label>
        {state.viewsCursor?<Button disabled={!!state.pending.views || locked} onClick={()=>refreshContextViews(true)}>Next saved views</Button>:null}
        <Button disabled={!state.connected || !state.viewId || busyView || locked} onClick={()=>captureContext(true)}>Refresh this view</Button></div>
      {state.pending.views || busyView?<p role="status">Loading context view…</p>:null}
      {!state.views.length && !state.pending.views && state.chatId?<p className="settings-empty">No saved views on this page. Capture current context to inspect retained evidence.</p>:null}
      {state.status?<div className="context-summary"><span>Messages {Number(counts.message || 0)}</span><span>Python cells {Number(counts.cell || 0)}</span><span>Snapshots {Number(counts.snapshot || 0)}</span><details><summary>Coverage and capture boundaries</summary><pre>{JSON.stringify({coverage:state.status.coverage,watermarks:state.status.watermarks,ordering:state.status.ordering},null,2)}</pre></details></div>:null}
    </SettingsSection>
    {state.viewId?<SettingsSection title="Historical evidence" description="Search includes retained message and cell text. Runtime snapshots may have narrower coverage; missing or discarded evidence is not reconstructed.">
      <form className="context-controls" onSubmit={event=>{event.preventDefault();changeContextFilter(query,state.kind);}}><input aria-label="Search session context" maxLength={1000} placeholder="Search retained evidence…" value={query} disabled={locked || !state.connected} onChange={event=>setQuery(event.target.value)}/><select aria-label="Context source kind" value={state.kind} disabled={locked || !state.connected} onChange={event=>changeContextFilter(query,event.target.value)}><option value="">All sources</option><option value="message">Messages</option><option value="cell">Python cells</option><option value="snapshot">Snapshots</option></select><Button type="submit" disabled={!state.connected || locked || !!state.pending.page}>Search</Button><Button disabled={locked || !state.connected} onClick={()=>{setQuery("");changeContextFilter("",state.kind);}}>Clear search</Button></form>
      {state.coverage?<p className="context-coverage">{state.coverage}</p>:null}
      <div className="context-browser"><div className="context-sources" aria-label="Context sources">
        {state.pending.page?<p role="status">Loading evidence…</p>:null}
        {state.items.map(row=><button key={row.source_id} className={row.source_id===state.sourceId?"is-selected":""} disabled={locked || !state.connected} onClick={()=>expandContextSource(row.source_id,row.kind==="cell"?"source":"result")}><span>{kindNames[row.kind] || row.kind} · {row.ordinal}</span><strong>{row.role || row.source_id}</strong><p>{row.snippet || row.preview || "Source details available"}</p></button>)}
        {!state.items.length && !state.pending.page?<p className="settings-empty">{state.query?"No matching retained sources.":"No retained sources in this view."}</p>:null}
        <div className="context-paging"><Button disabled={locked || !state.connected || !!state.pending.page || !state.pageStack.length} onClick={()=>turnContextPage(false)}>Previous page</Button><Button disabled={locked || !state.connected || !!state.pending.page || state.next===null} onClick={()=>turnContextPage(true)}>Next page</Button></div>
      </div><div className="context-detail" aria-label="Context source detail">
        {!state.sourceId?<p>Select a source to read its details.</p>:<>
          <header><strong>{kindNames[selected?.kind || ""] || "Source detail"}</strong>{selected?.kind==="cell"?<select aria-label="Cell evidence part" value={state.part} disabled={locked || !state.connected || !!state.pending.detail} onChange={event=>expandContextSource(state.sourceId,event.target.value)}><option value="source">Python source</option><option value="result">Result</option><option value="output">Output events</option></select>:null}</header>
          {state.pending.detail?<p role="status">Reading source…</p>:null}
          {state.detail?<><pre tabIndex={0}>{state.detail.text}</pre><p>Unicode characters {state.detail.offset}–{state.detail.offset+Array.from(state.detail.text).length} of {state.detail.total_chars}</p><details><summary>Source identity</summary><pre>{JSON.stringify(state.detail.source,null,2)}</pre></details><div className="context-paging"><Button disabled={!state.detailStack.length || locked || !state.connected} onClick={()=>turnContextDetail(false)}>Previous text</Button><Button disabled={!state.detail.has_more || locked || !state.connected} onClick={()=>turnContextDetail(true)}>Continue reading</Button></div></>:null}
        </>}
      </div></div>
    </SettingsSection>:null}
    <SettingsSection title="Export frozen context" description="Save retained evidence from the selected view. Export reports omissions and does not include unretained live objects."><div className="context-controls"><select aria-label="Context export format" value={format} disabled={locked || !state.connected} onChange={event=>setFormat(event.target.value as typeof format)}><option value="jsonl">JSONL · source evidence</option><option value="markdown">Markdown · readable</option></select><Button disabled={!state.connected || !state.viewId || locked || busyView} onClick={()=>void exportView()}>{locked?"Exporting…":"Export view"}</Button></div>{state.exportResult?<p role="status">Saved {String(state.exportResult.path)} · {Number(state.exportResult.bytes)} bytes{typeof state.exportResult.source_count==="number"?` · ${state.exportResult.source_count} sources`:""}{Number(state.exportResult.omission_count || 0)>0?` · ${state.exportResult.omission_count} omissions (see export for coverage)`:""}</p>:null}</SettingsSection>
    {!state.connected?<p role="status">Backend disconnected. Reconnect to read or export context.</p>:null}{state.error?<p className="settings-feedback is-error" role="alert">{state.error}</p>:null}{state.notice?<p className="settings-feedback" role="status">{state.notice}</p>:null}
  </div>;
}
