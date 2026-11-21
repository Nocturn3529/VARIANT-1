import {useEffect, useId, useRef, useState} from "react";
import {createPortal} from "react-dom";
import {useChatSelection, shallowChatSelection} from "../chatStore";
import {useSessionState} from "../state/sessionStore";
import {Icon} from "../ui/Icon";
import {Button} from "../ui/Button";
import {trapDialogFocus} from "../ui/Overlay";
import {useSurfaceDocument} from "../ui/SurfaceDocument";
import {ActivityMark, type MarkState} from "../motion/ActivityMark";
import type {ChatGoal} from "../protocol/goals";
import {formatGoalDuration, formatGoalTokens, goalCanEdit, goalCanResume, goalLength, goalStatusLabel, MAX_GOAL_OBJECTIVE} from "../protocol/goals";
import {answerGoalReplace, refreshComposerGoal, requestGoalControl} from "./goals";

function goalMark(goal: ChatGoal): MarkState {
  if (goal.status === "active") return goal.submission_pending ? "queued" : goal.continuing ? "live" : "idle";
  if (goal.status === "complete") return "done";
  if (goal.status === "paused") return "stopped";
  return "attention";
}

/** A plain modal for goal decisions; the composer stays where it was. */
function GoalDialog({title, children, onClose}: {title: string; children: (titleId: string) => React.ReactNode; onClose: () => void}) {
  const ownerDocument = useSurfaceDocument();
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const dialog = ref.current!, previous = ownerDocument.activeElement as HTMLElement | null;
    dialog.showModal();
    return () => {
      dialog.close();
      requestAnimationFrame(() => {
        if (previous?.isConnected && !ownerDocument.querySelector("dialog[open]")) previous.focus();
      });
    };
  }, [ownerDocument]);
  return createPortal(<dialog ref={ref} className="deck-input-dialog composer-goal-dialog" aria-labelledby={titleId}
    onCancel={event => { event.preventDefault(); event.stopPropagation(); onClose(); }}
    onKeyDown={event => { trapDialogFocus(event); event.stopPropagation(); }}>
    <header><h2 id={titleId}>{title}</h2><Button tone="icon" aria-label="Close dialog" onClick={onClose}>×</Button></header>
    {children(titleId)}
  </dialog>, ownerDocument.body);
}

function GoalFeedback() {
  const {goal, connected} = useChatSelection(state => ({goal: state.goal, connected: state.connected}), shallowChatSelection);
  const uncertain = goal.pending?.uncertain;
  const checking = !!goal.readRequestId;
  const error = goal.readError || goal.error;
  const showCheck = uncertain || (!goal.synced && (goal.goal || goal.confirmReplace || goal.readError));
  if (!error && !showCheck && !uncertain) return null;
  return <div className="composer-goal__reason">
    {error && <span role="alert">{error} </span>}
    {uncertain && <span role="status">The goal change is unconfirmed. </span>}
    {showCheck && (checking ? <span role="status">Checking the saved goal…</span>
      : connected ? <Button tone="quiet" onClick={() => refreshComposerGoal()}>Check saved goal</Button>
      : <span>Waiting for connection.</span>)}
  </div>;
}

function EditGoalDialog({goal, onClose}: {goal: ChatGoal; onClose: () => void}) {
  const {pending, confirmed, ready} = useChatSelection(state => ({pending: state.goal.pending,
    confirmed: state.goal.confirmedRequestId, ready: state.connected && state.goal.synced}), shallowChatSelection);
  const navigating = !!useSessionState().pendingAction;
  const [baseline, setBaseline] = useState(goal.objective);
  const [value, setValue] = useState(goal.objective);
  const [sent, setSent] = useState("");
  const text = value.trim();
  const busy = !!pending;
  const changed = goal.objective !== baseline;
  const tooLong = goalLength(text) > MAX_GOAL_OBJECTIVE;
  const canSave = ready && !busy && !navigating && !changed && !tooLong && !!text && text !== goal.objective && goalCanEdit(goal);
  useEffect(() => { if (sent && sent === confirmed && goal.objective === text) onClose(); }, [sent, confirmed, goal.objective, text, onClose]);
  return <GoalDialog title="Edit goal" onClose={onClose}>{() => <form onSubmit={event => {
    event.preventDefault();
    if (!canSave) return;
    const requestId = requestGoalControl("edit", goal, text);
    if (requestId) setSent(requestId);
  }}>
    <label><span>Objective</span><textarea className="composer-goal-dialog__objective" value={value} rows={6}
      readOnly={busy} autoFocus aria-invalid={tooLong} onChange={event => setValue(event.target.value)}/></label>
    {tooLong && <p role="alert">A goal is limited to 32,000 characters.</p>}
    {changed && !busy && <><p role="status">The saved objective changed while you were editing.</p>
      <blockquote className="composer-goal-dialog__quote">{goal.objective}</blockquote>
      <Button onClick={() => setBaseline(goal.objective)}>Keep editing my draft</Button></>}
    <GoalFeedback/>
    <footer><Button onClick={onClose}>{busy ? "Close" : "Cancel"}</Button>
      <Button tone="primary" type="submit" disabled={!canSave}>{busy ? "Saving…" : "Save objective"}</Button></footer>
  </form>}</GoalDialog>;
}

/** "Replace current goal?" — asked when /goal is sent while another goal is unfinished. */
export function GoalReplaceDialog() {
  const {held, goal, ready} = useChatSelection(state => ({held: state.goal.confirmReplace, goal: state.goal.goal,
    ready: state.connected && state.goal.synced && !state.goal.pending}), shallowChatSelection);
  const navigating = !!useSessionState().pendingAction;
  if (!held) return null;
  const target = held.expectedGoalId && held.expectedGoalId === goal?.goal_id ? goal : null;
  return <GoalDialog title="Replace current goal?" onClose={() => answerGoalReplace(held, false)}>{() => <>
    <p className="composer-goal-dialog__text">This chat can pursue one goal at a time. The current goal stops and the new one starts.</p>
    {target && <blockquote className="composer-goal-dialog__quote">{target.objective}</blockquote>}
    <GoalFeedback/>
    <footer><Button onClick={() => answerGoalReplace(held, false)}>Keep current goal</Button>
      <Button tone="primary" disabled={!target || !ready || navigating} onClick={() => answerGoalReplace(held, true)}>Replace goal</Button></footer>
  </>}</GoalDialog>;
}

/** The chat goal's one-line status above the composer, with its controls. */
export function ComposerGoalStatus() {
  const goal = useChatSelection(state => state.goal.goal);
  return goal ? <GoalStatus key={JSON.stringify([goal.session_id, goal.goal_id])} goal={goal}/> : <GoalFeedback/>;
}

function GoalStatus({goal}: {goal: ChatGoal}) {
  const {pending, connected, synced, replacing} = useChatSelection(state => ({pending: state.goal.pending,
    connected: state.connected, synced: state.goal.synced, replacing: !!state.goal.confirmReplace}), shallowChatSelection);
  const navigating = !!useSessionState().pendingAction;
  const [editing, setEditing] = useState(false);
  const disabled = !connected || !synced || !!pending || navigating;
  const label = goal.status === "complete" ? `Goal achieved in ${formatGoalDuration(goal.time_used_s)}`
    : goal.status === "active" && goal.submission_pending ? "Goal set · starts after the current turn" : goalStatusLabel(goal);
  const stopped = ["blocked", "budget_limited", "usage_limited"].includes(goal.status);
  return <section className="composer-goal" aria-label="Chat goal" data-goal-status={goal.status} data-goal-id={goal.goal_id}>
    <div className="composer-goal__line">
      <ActivityMark state={goalMark(goal)}/>
      <strong role="status" aria-live="polite">{label}</strong>
      <span className="composer-goal__objective" title={goal.objective}>{goal.objective}</span>
      <span className="composer-goal__tokens" title={`${goal.tokens_used.toLocaleString()}${goal.token_budget ? " of " + goal.token_budget.toLocaleString() : ""} tokens`}>
        {formatGoalTokens(goal.tokens_used)}{goal.token_budget ? " / " + formatGoalTokens(goal.token_budget) : ""} tokens</span>
      <div className="composer-goal__controls" role="group" aria-label="Goal controls">
        {goal.status === "active" ? <button type="button" disabled={disabled} title="Pause the goal; the current turn finishes"
          onClick={() => requestGoalControl("pause", goal)}><Icon name="pause"/><span>Pause</span></button> : null}
        {goalCanResume(goal) ? <button type="button" disabled={disabled} title="Resume working toward the goal"
          onClick={() => requestGoalControl("resume", goal)}><Icon name="play"/><span>Resume</span></button> : null}
        {goalCanEdit(goal) ? <button type="button" disabled={disabled} aria-label="Edit goal objective" title="Edit objective"
          onClick={() => setEditing(true)}><Icon name="edit"/></button> : null}
        <button type="button" disabled={disabled} aria-label="Clear goal" title={goal.status === "complete" ? "Dismiss" : "Clear goal; the transcript is kept"}
          onClick={() => requestGoalControl("clear", goal)}><Icon name="close"/></button>
      </div>
    </div>
    {stopped && goal.status_reason ? <p className="composer-goal__reason">{goal.status_reason}</p> : null}
    {!editing && !replacing && <GoalFeedback/>}
    {editing ? <EditGoalDialog goal={goal} onClose={() => setEditing(false)}/> : null}
  </section>;
}
