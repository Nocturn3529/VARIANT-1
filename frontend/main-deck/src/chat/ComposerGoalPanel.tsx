import {useChatState} from "../chatStore";
import {useSessionState} from "../state/sessionStore";
import {Icon} from "../ui/Icon";
import {goalIsTerminal} from "../protocol/goals";
import {refreshComposerGoal,requestGoalControl,setGoalGuidance,MAX_GOAL_GUIDANCE} from "./goals";

const statusLabels:Record<string,string>={draft:"Draft",queued:"Queued",running:"Running",waiting_user:"Waiting for your input",
  waiting_external:"Waiting for an external result",blocked:"Blocked",paused:"Scheduling paused",succeeded:"Execution finished",failed:"Execution failed",cancelled:"Cancelled",archived:"Archived"};

export function ComposerGoalPanel() {
  const state=useChatState(),goal=state.goal,snapshot=goal.snapshot;
  const navigating=!!useSessionState().pendingAction;
  if(!snapshot && !goal.pending && !goal.error)return null;
  const pending=goal.pending;
  const blocked=!state.connected || !goal.synced || !!pending || navigating;
  const record=snapshot?.goal;
  const pendingLabel=pending?.uncertain?"Checking saved goal state; no request has been resent."
    :pending?.operation==="submit"?"Submitting goal…"
    :pending?.operation==="pause"?"Requesting scheduling pause…"
    :pending?.operation==="resume"?"Resuming scheduling…"
    :pending?.operation==="cancel"?"Stopping goal and checking cleanup…"
    :pending?.operation==="continue"?"Continuing goal…":pending?.operation==="finish"?"Ending goal…":pending?.operation==="archive"?"Removing finished goal…":"";
  const lifecycle=record ? snapshot?.cleanup.status==="pending"?"Cleanup pending":snapshot?.cleanup.status==="failed"?"Cleanup failed":snapshot?.terminationKind==="user_finished"?"Ended by you":record.status==="cancelled" && snapshot?.cleanup.complete?"Stopped":statusLabels[record.status]:"Awaiting confirmation";
  return <section className="composer-goal" aria-label="Durable goal" data-goal-id={record?.goal_id}>
    <header><strong><Icon name="queue"/>Goal</strong><span role="status">{lifecycle}</span>
      <button type="button" className="composer-icon-button" aria-label="Refresh goal" aria-busy={!!goal.refreshRequestId} disabled={!state.connected || navigating} onClick={()=>refreshComposerGoal(true)}><Icon name="refresh"/></button></header>
    {record ? <>
      <code>{record.goal_id}</code>
      <details><summary>{record.title || record.objective}</summary><p className="composer-goal__objective">{record.objective}</p>
        {snapshot.steps.length ? <ol>{snapshot.steps.map(step=><li key={step.id}><span>{step.title}</span><small>{step.status.replaceAll("_"," ")}</small></li>)}</ol> : null}
      </details>
      <div className="composer-goal__facts" aria-label="Goal outcome and cleanup">
        <span>Objective <strong>{snapshot.objectiveOutcome.status}</strong></span>
        <span>Cleanup <strong>{snapshot.cleanup.complete?"complete":snapshot.cleanup.status==="complete"?"unconfirmed":snapshot.cleanup.status.replaceAll("_"," ")}</strong></span>
      </div>
      {snapshot.objectiveOutcome.summary?<p>{snapshot.objectiveOutcome.summary}</p>:null}
      {snapshot.reports.filter(report=>report.text).map(report=><details className="composer-goal__report" key={`${report.childId}:${report.stepId}`}>
        <summary>Agent report <small>{report.status.replaceAll("_"," ")}</small></summary>
        <p>{report.text}</p>{report.truncated?<small>Report preview truncated.</small>:null}
      </details>)}
      {snapshot.objectiveOutcome.status==="completed" ? <p className="composer-goal__basis">Agent-reported completion · Not independently verified</p> : null}
      {record.pause_reason ? <p>{record.pause_reason}</p> : null}
      {snapshot.capabilities.continue ? <label className="composer-goal__guidance" htmlFor="goal-continuation-guidance">
        <span>Continuation guidance <small>Optional</small></span>
        <textarea id="goal-continuation-guidance" aria-label="Goal continuation guidance" rows={2} maxLength={MAX_GOAL_GUIDANCE}
          placeholder="Add changed requirements or context for this goal…" disabled={!!pending || navigating}
          value={goal.guidance?.goalId===record.goal_id?goal.guidance.text:""}
          onChange={event=>setGoalGuidance(event.target.value,state.sessionId,record.goal_id,record.version)}/>
      </label>:null}
      <div className="composer-goal__controls">
        {!goalIsTerminal(record) && record.status!=="paused" && snapshot.capabilities.pause_scheduling ? <button type="button" disabled={blocked} onClick={()=>requestGoalControl("pause",state.sessionId,record.goal_id)}><Icon name="pause"/>Pause scheduling</button> : null}
        {record.status==="paused" && snapshot.capabilities.resume ? <button type="button" disabled={blocked} onClick={()=>requestGoalControl("resume",state.sessionId,record.goal_id)}><Icon name="play"/>Resume goal</button> : null}
        {snapshot.capabilities.continue ? <button type="button" data-goal-action="continue" disabled={blocked} onClick={()=>requestGoalControl("continue",state.sessionId,record.goal_id,record.version)}><Icon name="play"/>Continue goal</button>:null}
        {snapshot.capabilities.cancel || snapshot.capabilities.retry_cleanup ? <button type="button" disabled={blocked} onClick={()=>requestGoalControl("cancel",state.sessionId,record.goal_id)}><Icon name="stop"/>{snapshot.capabilities.retry_cleanup?"Retry cleanup":"Cancel goal"}</button> : null}
        {snapshot.capabilities.finish ? <button type="button" disabled={blocked} title="End this goal without claiming verified completion" onClick={()=>requestGoalControl("finish",state.sessionId,record.goal_id)}>End goal</button>:null}
        {snapshot.capabilities.archive ? <button type="button" disabled={blocked} onClick={()=>requestGoalControl("archive",state.sessionId,record.goal_id)}>Dismiss goal</button>:null}
        {!goalIsTerminal(record) && snapshot.capabilities.pause_scheduling && !snapshot.capabilities.pause_active_work ? <small>A running step may finish while scheduling is paused.</small> : null}
      </div>
    </> : null}
    {pendingLabel ? <p role="status">{pendingLabel}</p> : null}
    {!goal.synced && snapshot ? <p role="status">Saved status needs refreshing.</p> : null}
    {goal.error ? <p role="alert">{goal.error}</p> : null}
  </section>;
}
