import {Icon} from "../ui/Icon";
import {Overlay,OverlayHeader} from "../ui/Overlay";
import {StatusGlyph,type GlyphState} from "../motion/StatusGlyph";
import {useElapsed} from "./elapsedClock";
import {childStepLabel,useChildActivity} from "./childActivity";

import type {AgentSummary,AgentDetail} from "../protocol/children";
const active=(agent:AgentSummary)=>["queued","running"].includes(agent.status);
function time(value:number):string {
  if(!value)return "Not reported";
  const date=new Date(value<1e12?value*1000:value);
  return Number.isNaN(date.getTime())?"Not reported":date.toLocaleString(undefined,{month:"short",day:"numeric",hour:"2-digit",minute:"2-digit",second:"2-digit"});
}
const label=(value:string)=>value?value.replaceAll("_"," "):"unknown";
const ms=(value:number)=>value<1e12?value*1000:value;
/** Compact duration: 12s, 4m, 1h 3m. */
export function shortDuration(value:number):string {
  const seconds=Math.max(0,Math.floor(value/1000));
  if(seconds<60)return `${seconds}s`;
  const minutes=Math.floor(seconds/60);
  if(minutes<60)return `${minutes}m`;
  return `${Math.floor(minutes/60)}h ${minutes%60}m`;
}
export function agentGlyph(agent:Pick<AgentSummary,"status"|"outcome">):GlyphState {
  if(agent.outcome==="blocked")return "blocked";
  if(agent.status==="running")return "running";
  if(agent.status==="queued")return "queued";
  if(/fail|error/.test(agent.status))return "failed";
  if(/cancel|stop|interrupt/.test(agent.status))return "stopped";
  if(/complete|succeed|done|finish/.test(agent.status))return "done";
  return "idle";
}
function activityGlyph(status:string):GlyphState {
  if(/running|start|leas/.test(status))return "running";
  if(/queue|plan|pending/.test(status))return "queued";
  if(/fail|error/.test(status))return "failed";
  if(/cancel|stop|interrupt/.test(status))return "stopped";
  if(/ok|succeed|complete|done/.test(status))return "done";
  return "idle";
}

function AgentRow({agent,parent,onSelect}:{agent:AgentSummary;parent:string;onSelect:(id:string)=>void}) {
  const running=agent.status==="running";
  const elapsed=useElapsed(running && !!agent.startedAt,ms(agent.startedAt));
  const activity=useChildActivity(agent);
  const finished=agent.completedAt && agent.startedAt?shortDuration(ms(agent.completedAt)-ms(agent.startedAt)):"";
  const when=running && agent.startedAt?shortDuration(elapsed):finished;
  return <button type="button" className={`agent-team__card${active(agent)?" is-working":""}`} data-agent-state={agentGlyph(agent)} onClick={()=>onSelect(agent.id)} aria-label={`Inspect ${agent.name}`}
    title={`Spawned ${time(agent.createdAt)}`}>
    <span className="agent-team__glyph"><StatusGlyph state={agentGlyph(agent)}/></span>
    <span className="agent-team__main">
      <span className="agent-team__name">{agent.name}<small>{label(agent.status)}</small></span>
      <span className="agent-team__task">{agent.task || "Assignment not reported"}</span>
      {activity?<span className="agent-team__current">{childStepLabel(activity)}</span>:null}
      {parent || agent.outcome==="blocked"?<span className="agent-team__meta">{parent?<small>From {parent}</small>:null}{agent.outcome==="blocked"?<small>Objective blocked</small>:null}</span>:null}
    </span>
    {when?<span className="agent-team__time">{when}</span>:null}
  </button>;
}

export function AgentTeamView({agents,total,activeCount,blockedCount,truncated,connected,synced,error,selectedId,detail,detailError,loadingDetail,onRefresh,onSelect,onClose}: {
  agents:readonly AgentSummary[];total:number;activeCount:number;blockedCount:number;truncated:boolean;connected:boolean;synced:boolean;error:string;
  selectedId:string|null;detail:AgentDetail|null;detailError:string;loadingDetail:boolean;
  onRefresh:()=>void;onSelect:(id:string)=>void;onClose:()=>void;
}) {
  if(!agents.length && !error)return null;
  const selected=agents.find(agent=>agent.id===selectedId);
  const parent=(agent:AgentSummary)=>agent.parentId?(agents.find(row=>row.id===agent.parentId)?.name || agent.parentId):"";
  const working=agents.filter(active),finished=agents.filter(agent=>!active(agent));
  const row=(agent:AgentSummary)=><AgentRow key={agent.id} agent={agent} parent={parent(agent)} onSelect={onSelect}/>;
  return <section className="agent-team" aria-label="Agent team">
    <header><strong><Icon name="tree"/>Agent team <span>{total}{truncated && total<=agents.length?"+":""}</span></strong><span>{activeCount} working{blockedCount?` · ${blockedCount} blocked`:""}</span>
      {!synced?<small>Checking status</small>:null}<button type="button" aria-label="Refresh agent team" disabled={!connected} onClick={onRefresh}><Icon name="refresh"/></button></header>
    {error?<p role="alert">{error}</p>:null}
    {working.length?<div className="agent-team__roster">{working.map(row)}</div>:null}
    {finished.length?<details className="agent-team__finished" open={!working.length || undefined}>
      <summary><Icon name="chevron" className="agent-team__chevron"/>{finished.length} finished</summary>
      <div className="agent-team__roster">{finished.map(row)}</div>
    </details>:null}
    {truncated?<small className="agent-team__note">Showing a bounded agent history.</small>:null}
    {selected?<Overlay labelledBy="agent-team-title" onClose={onClose} className="agent-team-overlay">
      <OverlayHeader id="agent-team-title" title="Agent activity" onClose={onClose}/>
      <div className="agent-team-overlay__layout">
        <nav aria-label="Subagents">{agents.map(agent=><button key={agent.id} type="button" aria-current={agent.id===selected.id?"true":undefined} onClick={()=>onSelect(agent.id)}>
          <StatusGlyph state={agentGlyph(agent)}/><span><strong>{agent.name}</strong><span>{label(agent.status)}</span>{parent(agent)?<small>From {parent(agent)}</small>:null}</span></button>)}</nav>
        <div className="agent-team-overlay__detail">
          <SelectedAgentHeader agent={selected} parent={parent(selected)}/>
          <div className="agent-team__trace-heading"><h3>{selected.name} · execution trace</h3><button type="button" disabled={!connected} onClick={()=>onSelect(selected.id)}><Icon name="refresh"/>Refresh trace</button></div>
          <p className="agent-team__provenance">Recorded capability operations · Includes prior runs of this child</p>
          {loadingDetail?<div className="agent-team__skeleton" role="status" aria-label="Loading this agent’s activity"><i/><i/><i/></div>:null}{detailError?<p role="alert">{detailError}</p>:null}
          {detail?.id===selected.id && detail.generation===selected.generation?<>
            {detail.activities.length?<ol className="agent-team__trace">{detail.activities.map(activity=><li key={activity.id} data-status={activityGlyph(activity.status)}>
              <StatusGlyph state={activityGlyph(activity.status)} className="agent-team__trace-glyph"/>
              <div><strong>{activity.kind}</strong><span>{label(activity.status)}</span></div><time>{time(activity.createdAt)}</time>
              <code>{activity.id}</code>{activity.runId?<small>Run {activity.runId}</small>:null}
              {activity.completedAt?<small>Finished {time(activity.completedAt)}</small>:null}
            </li>)}</ol>:<p className="agent-team__empty">No recorded trace entries for this agent yet.</p>}
            {detail.report?<section className="agent-team__report"><h3>Agent report</h3><p>{detail.report}</p></section>:null}
            {detail.truncated?<p className="agent-team__note">Activity preview is truncated.</p>:null}
          </>:null}
        </div>
      </div>
    </Overlay>:null}
  </section>;
}

function SelectedAgentHeader({agent,parent}:{agent:AgentSummary;parent:string}) {
  const running=agent.status==="running";
  const elapsed=useElapsed(running && !!agent.startedAt,ms(agent.startedAt));
  const activity=useChildActivity(agent);
  return <header className="agent-team-overlay__summary">
    <span className="agent-team__eyebrow">Subagent{parent?` · from ${parent}`:""}</span>
    <h2><StatusGlyph state={agentGlyph(agent)} size={18}/>{agent.name}<small>{label(agent.status)}{running && agent.startedAt?` · ${shortDuration(elapsed)}`:""}</small></h2>
    {activity?<p className="agent-team-overlay__now"><span>Now</span>{childStepLabel(activity)}</p>:null}
    <p className="agent-team-overlay__task">{agent.task || "Assignment not reported"}</p>
    <details className="agent-team-overlay__facts">
      <summary>Run details</summary>
      <dl className="agent-team__facts"><div><dt>Execution</dt><dd>{label(agent.status)}</dd></div><div><dt>Objective</dt><dd>{label(agent.outcome)}</dd></div><div><dt>Cleanup</dt><dd>{label(agent.cleanupStatus)}</dd></div><div><dt>Generation</dt><dd>{agent.generation || "Not reported"}</dd></div><div><dt>Spawned</dt><dd>{time(agent.createdAt)}</dd></div><div><dt>Started</dt><dd>{time(agent.startedAt)}</dd></div>{agent.completedAt?<div><dt>Finished</dt><dd>{time(agent.completedAt)}</dd></div>:null}<div><dt>Agent id</dt><dd><code>{agent.id}</code></dd></div></dl>
    </details>
  </header>;
}
