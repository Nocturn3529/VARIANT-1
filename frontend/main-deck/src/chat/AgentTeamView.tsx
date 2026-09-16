import {Icon} from "../ui/Icon";
import {Overlay,OverlayHeader} from "../ui/Overlay";

import type {AgentSummary,AgentDetail} from "../protocol/children";
const active=(agent:AgentSummary)=>["queued","running"].includes(agent.status);
function time(value:number):string {
  if(!value)return "Not reported";
  const date=new Date(value<1e12?value*1000:value);
  return Number.isNaN(date.getTime())?"Not reported":date.toLocaleString(undefined,{month:"short",day:"numeric",hour:"2-digit",minute:"2-digit",second:"2-digit"});
}
const label=(value:string)=>value?value.replaceAll("_"," "):"unknown";

export function AgentTeamView({agents,total,activeCount,blockedCount,truncated,connected,synced,error,selectedId,detail,detailError,loadingDetail,onRefresh,onSelect,onClose}: {
  agents:readonly AgentSummary[];total:number;activeCount:number;blockedCount:number;truncated:boolean;connected:boolean;synced:boolean;error:string;
  selectedId:string|null;detail:AgentDetail|null;detailError:string;loadingDetail:boolean;
  onRefresh:()=>void;onSelect:(id:string)=>void;onClose:()=>void;
}) {
  if(!agents.length && !error)return null;
  const selected=agents.find(agent=>agent.id===selectedId);
  const parent=(agent:AgentSummary)=>agent.parentId?(agents.find(row=>row.id===agent.parentId)?.name || agent.parentId):"";
  return <section className="agent-team" aria-label="Agent team">
    <header><strong><Icon name="tree"/>Agent team <span>{total}{truncated && total<=agents.length?"+":""}</span></strong><span>{activeCount} working{blockedCount?` · ${blockedCount} blocked`:""}</span>
      {!synced?<small>Checking status</small>:null}<button type="button" aria-label="Refresh agent team" disabled={!connected} onClick={onRefresh}><Icon name="refresh"/></button></header>
    {error?<p role="alert">{error}</p>:null}
    <div className="agent-team__roster">{agents.map(agent=><button type="button" key={agent.id} className={`agent-team__card${active(agent)?" is-working":""}`} onClick={()=>onSelect(agent.id)} aria-label={`Inspect ${agent.name}`}>
      <span className="agent-team__name"><i aria-hidden="true"/>{agent.name}<small>{label(agent.status)}</small></span>
      <span className="agent-team__task">{agent.task || "Assignment not reported"}</span>
      <small>Spawned {time(agent.createdAt)}</small>
      {agent.parentId?<small>From {parent(agent)}</small>:null}
      {agent.currentActivity?<span className="agent-team__current">{agent.currentActivity}</span>:null}
      {agent.outcome==="blocked"?<small>Objective blocked</small>:null}
    </button>)}</div>
    {truncated?<small>Showing a bounded agent history.</small>:null}
    {selected?<Overlay labelledBy="agent-team-title" onClose={onClose} className="agent-team-overlay">
      <OverlayHeader id="agent-team-title" title="Agent activity" onClose={onClose}/>
      <div className="agent-team-overlay__layout">
        <nav aria-label="Subagents">{agents.map(agent=><button key={agent.id} type="button" aria-current={agent.id===selected.id?"true":undefined} onClick={()=>onSelect(agent.id)}><strong>{agent.name}</strong><span>{label(agent.status)}</span>{parent(agent)?<small>From {parent(agent)}</small>:null}</button>)}</nav>
        <div className="agent-team-overlay__detail">
          <header><span className="agent-team__eyebrow">Subagent{parent(selected)?` · ${parent(selected)}`:""}</span><h2>{selected.name}</h2><code>{selected.id}</code><p>{selected.task || "Assignment not reported"}</p></header>
          <dl className="agent-team__facts"><div><dt>Execution</dt><dd>{label(selected.status)}</dd></div><div><dt>Objective</dt><dd>{label(selected.outcome)}</dd></div><div><dt>Cleanup</dt><dd>{label(selected.cleanupStatus)}</dd></div><div><dt>Generation</dt><dd>{selected.generation || "Not reported"}</dd></div><div><dt>Spawned</dt><dd>{time(selected.createdAt)}</dd></div><div><dt>Started</dt><dd>{time(selected.startedAt)}</dd></div>{selected.completedAt?<div><dt>Finished</dt><dd>{time(selected.completedAt)}</dd></div>:null}</dl>
          <div className="agent-team__trace-heading"><h3>{selected.name} · execution trace</h3><button type="button" disabled={!connected} onClick={()=>onSelect(selected.id)}>Refresh trace</button></div>
          <p className="agent-team__provenance">Recorded capability operations · Includes prior runs of this child</p>
          {loadingDetail?<p role="status">Loading this agent’s activity…</p>:null}{detailError?<p role="alert">{detailError}</p>:null}
          {detail?.id===selected.id && detail.generation===selected.generation?<>
            {detail.activities.length?<ol className="agent-team__trace">{detail.activities.map(activity=><li key={activity.id}>
              <div><strong>{activity.kind}</strong><span>{label(activity.status)}</span></div><time>{time(activity.createdAt)}</time>
              <code>{activity.id}</code>{activity.runId?<small>Run {activity.runId}</small>:null}
              {activity.completedAt?<small>Finished {time(activity.completedAt)}</small>:null}
            </li>)}</ol>:<p>No recorded trace entries for this agent yet.</p>}
            {detail.report?<section className="agent-team__report"><h3>Agent report</h3><p>{detail.report}</p></section>:null}
            {detail.truncated?<p>Activity preview is truncated.</p>:null}
          </>:null}
        </div>
      </div>
    </Overlay>:null}
  </section>;
}
