import {useEffect, useMemo, useRef, useState} from "react";
import {RichText, thoughtPreview} from "./RichText";
import {canRevealPaneForStep, revealPaneForStep} from "../workbench/activityRouting";
import {formatActivityDuration, stepFailed, activityStatusLabel} from "./activityModel";
import {currentTraceStep, traceRows, traceSummary} from "./traceModel";
import {traceActionLabel} from "./traceLabels";
import {Icon} from "../ui/Icon";
import type {ChatTurnStep} from "./types";
import {useChatSelection, shallowChatSelection} from "../chatStore";
import {useElapsed} from "./elapsedClock";
import {PEER_DELIVERY} from "../peers/peerModels";
import {peerRequestKey, requestPeer, usePeers} from "../peers/peerStore";
import {disclosureChoice, disclosureKey, useTraceDisclosure} from "./disclosures";
import {navigateTo} from "../state/appStore";
import {chooseContextChat,captureContext,getExternalContextSettings} from "../externalContextSettingsStore";

// Virtualized turns retain disclosures across scroll and overlay visits.
function timeMs(value?: number) {
  const number = Number(value) || 0;
  return number > 0 && number < 1e12 ? number * 1000 : number;
}

type TraceRow = ReturnType<typeof traceRows>[number];
type RowScope = readonly [string, string];

/**
 * A turn reads in the order it happened, as Hermes lays it out: what the
 * model thought, what it said while working, the tools it ran (consecutive
 * calls fold into one run), and the messages it sent to other agents.
 */
export type TimelineBlock =
  | {kind: "thought"; row: TraceRow}
  | {kind: "text"; step: ChatTurnStep}
  | {kind: "peer"; step: ChatTurnStep}
  | {kind: "inbound"; step: ChatTurnStep}
  | {kind: "tools"; rows: TraceRow[]};

export function timelineBlocks(steps: readonly ChatTurnStep[], shownPeerMessages?: ReadonlySet<string>): TimelineBlock[] {
  const blocks: TimelineBlock[] = [];
  for (const row of traceRows(steps)) {
    const {step} = row;
    if (step.peerInbound) {
      // Once the transcript has the message as a card in this turn, the card speaks for it.
      if (!shownPeerMessages?.has(step.peerInbound.message_id)) blocks.push({kind: "inbound", step});
      continue;
    }
    if (step.kind === "thinking") blocks.push({kind: "thought", row});
    else if (step.kind === "text") { if (step.detail?.trim()) blocks.push({kind: "text", step}); }
    else if (step.peerMessage) blocks.push({kind: "peer", step});
    else {
      const last = blocks.at(-1);
      if (last?.kind === "tools") last.rows.push(row);
      else blocks.push({kind: "tools", rows: [row]});
    }
  }
  return blocks;
}

function TraceProgress({mutation = false}: {mutation?: boolean}) {
  return <span className={`trace-progress${mutation ? " trace-progress--mutation" : ""}`} aria-hidden="true"><i/><i/><i/></span>;
}

function Elapsed({start}: {start: number}) {
  const fallback = useRef(Date.now());
  const elapsed = useElapsed(true, start || fallback.current);
  return elapsed >= 1000 ? <small className="trace-time" aria-hidden="true">{formatActivityDuration(elapsed)}</small> : null;
}

function ThoughtText({text, streaming, preview}: {text: string; streaming: boolean; preview: boolean}) {
  const viewport = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  useEffect(() => {
    if (!streaming || !preview || !viewport.current || !content.current) return;
    let height = -1;
    const observer = new ResizeObserver(entries => {
      const next = entries[0]?.contentRect.height ?? 0;
      if (next <= height) { height = next; return; }
      height = next;
      if (follow.current && viewport.current) viewport.current.scrollTop = viewport.current.scrollHeight;
    });
    observer.observe(content.current);
    return () => observer.disconnect();
  }, [streaming, preview]);
  return <div ref={viewport} className={`trace-thought-content message__content${preview ? " is-preview" : ""}`}
    onScroll={event => {const node = event.currentTarget; follow.current = node.scrollHeight - node.scrollTop - node.clientHeight < 24;}}>
    <div ref={content}><RichText value={text} streaming={streaming}/></div>
  </div>;
}

function TraceEntry({row, live, current, scope, suspended, mutation, waiting}: {
  row: TraceRow; live: boolean; current: boolean; scope: RowScope; suspended: string; mutation: boolean; waiting: boolean;
}) {
  const {step, presentation, headline} = row;
  const thought = step.kind === "thinking";
  const running = live && !suspended && step.status === "running";
  const failed = stepFailed(step);
  const [choice, setChoice] = useTraceDisclosure(scope, step.callId || step.id, step.id);
  const [sawLive, setSawLive] = useState(thought && running);
  useEffect(() => { if (thought && running) setSawLive(true); }, [thought, running]);
  const open = choice ?? (failed || (thought && (running || sawLive)));
  const preview = choice === undefined && thought && (running || sawLive);
  const previewText = useMemo(() => thought ? thoughtPreview(step.detail || "") : "", [thought, step.detail]);
  const startedAt = useRef(timeMs(step.startedAt || step.ts) || Date.now());
  const previousFailure = useRef(failed);
  useEffect(() => {
    if (failed && !previousFailure.current) setChoice(true);
    previousFailure.current = failed;
  }, [failed, setChoice]);
  const duration = formatActivityDuration(step.durationMs);
  const hasDetails = !!(step.argsPreview || step.resultPreview || step.detail);
  const label = thought ? (running ? "Thinking" : step.summaryState === "discarded" ? "Discarded thought" : step.summaryState === "cancelled" ? "Interrupted thought" : "Thought")
    : waiting && running ? "Waiting for peer reply" : traceActionLabel(step, live, presentation);
  const status = running ? "Running" : step.status === "running" ? suspended || "Interrupted" : activityStatusLabel(step.status);
  const exception = step.status && !["running", "ok", "done"].includes(step.status);
  const cellLine = presentation.python && presentation.input?.trimStart().startsWith("#") ? headline : "";
  const Heading = hasDetails ? "button" : "div";
  return <section className={`trace-entry${thought ? " trace-entry--thought" : ""}${open ? " is-open" : ""}${failed ? " is-error" : ""}${running ? " is-running" : ""}${suspended && step.status === "running" ? " is-suspended" : ""}`} data-conversation-scaffold="" data-trace-id={step.id}>
    <div className="trace-entry__heading">
      <Heading {...(hasDetails ? {type:"button" as const,onClick:()=>setChoice(!open)} : {tabIndex:0,role:"group"})} className="trace-entry__disclosure trace-disclosure" aria-label={`${label}, ${status}${duration ? `, ${duration}` : ""}${presentation.python ? `, Python cell${presentation.executionCount !== null ? ` ${presentation.executionCount}` : ""}` : ""}`}
        aria-expanded={hasDetails ? open : undefined}>
        {current ? <TraceProgress mutation={mutation}/> : exception ? <Icon name={failed ? "error" : step.status === "cancelled" || step.status === "interrupted" ? "stop" : "pause"}/> : null}
        <span className="trace-entry__name" title={label} role={current ? "status" : undefined}>{label}</span>
        {hasDetails ? <Icon className="trace-caret" name="chevron"/> : null}
        {!open && thought && previewText ? <span className="trace-entry__headline">{previewText}</span> : cellLine ? <code className="trace-entry__headline" title={cellLine}>{cellLine}</code> : null}
        {exception && !failed && !label.toLowerCase().includes(status.toLowerCase()) ? <small>{status}</small> : null}
        {current ? <Elapsed start={startedAt.current}/> : duration ? <small className="trace-time">{duration}</small> : null}
      </Heading>
      {!thought && canRevealPaneForStep(step) ? <button type="button" className="trace-entry__related" aria-label={`Open related surface for ${headline}`} title="Open related surface" onClick={() => revealPaneForStep(step)}><Icon name="popout"/></button> : null}
    </div>
    {open && hasDetails ? <div className="trace-entry__body">
      {thought ? <ThoughtText text={step.detail || ""} streaming={running} preview={preview}/> : <>
        <dl className="trace-entry__metadata">
          {presentation.executionCount !== null ? <div><dt>Python cell</dt><dd>{presentation.executionCount}</dd></div> : null}
          {presentation.generation !== null ? <div><dt>Kernel</dt><dd>generation {presentation.generation}</dd></div> : null}
          <div><dt>Status</dt><dd>{status}</dd></div>
        </dl>
        {presentation.input ? <section><strong>{presentation.python ? "Code" : "Input"}</strong><pre><code>{presentation.input}</code></pre></section> : null}
        {step.resultPreview ? <section><strong>{failed ? "Diagnostic" : "Result"}</strong><pre><code>{step.resultPreview}</code></pre></section> : null}
        {!step.argsPreview && !step.resultPreview && step.detail ? <pre>{step.detail}</pre> : null}
        {step.evidence?.some(item => item.kind === "file" || item.kind === "url") ? <div className="trace-evidence">
          {step.evidence.filter(item => item.kind === "file" || item.kind === "url").map(item => <button type="button" key={item.id} title={item.value}
            onClick={() => revealPaneForStep({...step, evidence: [item]})}><Icon name={item.kind === "file" ? "file" : "browser"}/><span>{item.label || item.value}</span><Icon name="popout"/></button>)}
        </div> : null}
      </>}
    </div> : null}
  </section>;
}

/** What the model said while it worked; quieter than its reply. */
function Narration({step}: {step: ChatTurnStep}) {
  return <div className="turn-narration message__content" data-conversation-scaffold="" data-trace-id={step.id}>
    <RichText value={step.detail || ""}/>
    {step.status === "cancelled" ? <small className="turn-narration__stopped">Stopped before finishing</small> : null}
  </div>;
}

/** A message this agent sent to another agent, shown where it was sent. */
function PeerSendNotice({step, scope, live}: {step: ChatTurnStep; scope: RowScope; live: boolean}) {
  const peer = step.peerMessage!;
  const [open = false, setOpen] = useTraceDisclosure(scope, `peer:${peer.message_id}`, `peer:${peer.message_id}`);
  const peers = usePeers(), chatId = scope[0];
  const cached = peers.messages[peer.message_id];
  const receipt = cached?.sender_peer_id === peer.sender_peer_id && cached?.target_peer_id === peer.target_peer_id ? cached : undefined;
  const state = peer.state === "replied" ? peer.state : receipt?.state || peer.state;
  const kind = peer.message_kind || receipt?.message_kind;
  const name = peer.target_display_name || receipt?.target_display_name || "another agent";
  const verb = kind === "request" ? "Sent request to" : kind === "result" ? "Sent result to" : "Sent a message to";
  const delivery = PEER_DELIVERY[state];
  // A reply can invalidate the chat under its own message ID. Refresh the original
  // request to learn its authoritative state, coalescing changes during a read.
  const revision = peers.scopes[chatId]?.changed || 0;
  const initialRevision = useRef(revision);
  const request = {operation:"inspect" as const, message_id:peer.message_id};
  const pending = peers.requests[peerRequestKey(chatId, request)]?.phase === "pending";
  const lastRead = useRef("");
  useEffect(() => {
    if (!peers.connected) {lastRead.current = ""; return;}
    const followRequest = kind === "request" && revision > initialRevision.current;
    if (!chatId || peer.sender_peer_id !== `chat:${chatId}` || (!live && !open && !followRequest) || state === "replied" || pending) return;
    const key = JSON.stringify([chatId, peer.message_id, revision]);
    if (key === lastRead.current) return;
    const timer = setTimeout(() => {
      lastRead.current = key;
      requestPeer(chatId, {operation:"inspect", message_id:peer.message_id});
    }, 100);
    return () => clearTimeout(timer);
  }, [chatId, peer.message_id, peer.sender_peer_id, peers.connected, revision, live, open, kind, state, pending]);
  return <details open={open} className={`peer-send-trace${state === "failed" ? " is-error" : ""}`} data-conversation-scaffold="" data-trace-id={step.id}>
    <summary className="trace-disclosure" onClick={event => {event.preventDefault(); setOpen(!open);}} aria-label={`${verb} ${name}, ${delivery?.label || state}`}>
      <span>{verb} {name}</span><Icon className="trace-caret" name="chevron"/><small title={delivery?.meaning}>{delivery?.label || state}</small>
    </summary>
    <p>{peer.content}</p>
  </details>;
}

/** A message from another agent that steered into this run, where it landed. */
function PeerInboundNotice({step, scope}: {step: ChatTurnStep; scope: RowScope}) {
  const inbound = step.peerInbound!;
  const [open = true, setOpen] = useTraceDisclosure(scope, `in:${inbound.message_id}`, `in:${inbound.message_id}`);
  const peers = usePeers();
  const sessionId = useChatSelection(state => state.sessionId);
  const receipt = peers.messages[inbound.message_id];
  const content = inbound.content ?? (receipt?.sender_peer_id === inbound.peer_id ? receipt.content : undefined);
  useEffect(() => {
    if (content !== undefined || !peers.connected || !sessionId) return;
    requestPeer(sessionId, {operation: "inspect", message_id: inbound.message_id});
  }, [content, peers.connected, sessionId, inbound.message_id]);
  const name = inbound.display_name || "another agent";
  return <details className="peer-send-trace peer-receive-trace" open={open} data-conversation-scaffold="" data-trace-id={step.id}>
    <summary className="trace-disclosure" onClick={event => {event.preventDefault(); setOpen(!open);}} aria-label={`Message from ${name}, steered into this task`}>
      <span>Message from {name}</span><Icon className="trace-caret" name="chevron"/><small>Steered in</small>
    </summary>
    <p>{content ?? (peers.connected ? "Loading message…" : "Message unavailable while offline.")}</p>
  </details>;
}

/** The folded group itself presents the current action. Opening it moves the cue to that row. */
function ToolRun({rows, live, activeId, scope, suspended, mutation, waiting}: {
  rows: TraceRow[]; live: boolean; activeId: string | null; scope: RowScope;
  suspended: string; mutation: boolean; waiting: boolean;
}) {
  const steps = useMemo(() => rows.map(row => row.step), [rows]);
  const summary = useMemo(() => traceSummary(steps), [steps]);
  const first = steps[0];
  const [choice, setChoice] = useTraceDisclosure(scope, `group:${first.callId || first.id}`, `group:${first.id}`);
  const previousErrors = useRef(summary.errors);
  useEffect(() => {
    if (summary.errors > previousErrors.current) setChoice(true);
    previousErrors.current = summary.errors;
  }, [summary.errors, setChoice]);
  const active = steps.find(step => step.id === activeId);
  if (rows.length === 1) return <div className="tool-run is-single">
    <TraceEntry row={rows[0]} live={live} current={!!active} scope={scope} suspended={suspended} mutation={mutation} waiting={waiting && !!active}/>
  </div>;
  const open = choice ?? (summary.errors > 0 || steps.some(step =>
    disclosureChoice(disclosureKey(scope[0], scope[1], scope[1] ? step.callId || step.id : step.id)) === true));
  const total = steps.reduce((sum, step) => sum + (step.durationMs || 0), 0);
  const current = !open && active;
  const label = current ? waiting ? "Waiting for peer reply" : traceActionLabel(current, true) : summary.label;
  const notices = steps.filter(step => stepFailed(step) || ["cancelled","interrupted","degraded","unknown"].includes(step.status || "")).slice(-2);
  return <div className={`tool-run execution-trace${active ? " is-live" : ""}`}>
    <button type="button" className="execution-trace__summary trace-disclosure" aria-label={`Tool activity, ${label}${current ? `, ${rows.length} steps` : ""}`} aria-expanded={open}
      onClick={() => setChoice(!open)}>
      {current ? <TraceProgress mutation={mutation}/> : summary.errors ? <Icon name="error"/> : null}
      <span className="execution-trace__counts" role={current ? "status" : undefined}>{label}</span><Icon name="chevron" className="trace-caret"/>
      {current ? <Elapsed start={timeMs(current.startedAt || current.ts)}/> : total > 0 && !active ? <small className="trace-time">{formatActivityDuration(total)}</small> : null}
    </button>
    {!open && notices.length ? <div className="execution-trace__alerts">{notices.map(step => <p key={step.id} className={stepFailed(step) ? "is-error" : ""}>{traceActionLabel(step, false)}</p>)}</div> : null}
    {open ? <div className="execution-trace__rows">{rows.map(row => <div key={disclosureKey(scope[0], scope[1], row.step.callId || row.step.id)}>
      {row.boundary ? <div className="execution-trace__generation">{row.boundary}</div> : null}
      <TraceEntry row={row} live={live} current={row.step === active} scope={scope} suspended={suspended} mutation={mutation} waiting={waiting && row.step === active}/>
    </div>)}</div> : null}
  </div>;
}

export function TurnActivity({steps, live, streamText, turnStartedAt,ownerLabel="",tracePersistence,scope,currentStep,shownPeerMessages,suppressStatus=false}: {
  steps: readonly ChatTurnStep[]; live: boolean; streamText: string; turnStartedAt: number;ownerLabel?:string;
  tracePersistence?: "pending" | "saved" | "failed";
  scope?: string;
  currentStep?: ChatTurnStep | null;
  /** Peer messages this turn already shows as transcript cards. */
  shownPeerMessages?: ReadonlySet<string>;
  suppressStatus?: boolean;
}) {
  const blocks = useMemo(() => timelineBlocks(steps, shownPeerMessages), [steps, shownPeerMessages]);
  const chat = useChatSelection(state=>({sessionId:state.sessionId,mutation:live && state.runtime?.mutationEffectiveEnabled,connected:state.connected,pause:live ? state.pause : null,stopPending:live && state.stopPending,waits:live ? state.peerWaits : undefined}),shallowChatSelection);
  const mutation = !!chat.mutation;
  const suspended = live && (!chat.connected ? "Reconnecting" : chat.pause?.state === "paused" ? "Paused"
    : chat.stopPending ? "Stopping" : chat.pause?.state === "pausing" ? "Pausing" : "") || "";
  const active = live && !suspended ? currentStep === undefined ? currentTraceStep(steps) : currentStep : null;
  const waiting = !!active?.callId && !!chat.waits?.some(wait => wait.runId === scope && wait.outerCallId === active.callId);
  const fallbackScope = useRef(String(turnStartedAt));
  const rowScope = [chat.sessionId || "", scope ?? fallbackScope.current] as const;
  const showStatus = live && !suppressStatus && (suspended || (!active && !streamText));
  if (!blocks.length && !showStatus) return null;
  return <div className={`turn-timeline turn-activity-stack${live && !suspended ? " is-live" : ""}`}>
    {steps.some(step=>step.omittedBefore) ? <button type="button" className="message-action" disabled={!chat.connected || !chat.sessionId}
      onClick={()=>{if(!chat.sessionId)return;chooseContextChat(chat.sessionId);navigateTo("memory");if(getExternalContextSettings().chatId===chat.sessionId)captureContext();}}
      aria-label="View earlier session evidence">View earlier evidence</button> : null}
    {ownerLabel ? <strong className="execution-trace__owner">{ownerLabel}</strong> : null}
    {blocks.map(block => {
      if (block.kind === "thought") return <TraceEntry key={disclosureKey(rowScope[0], rowScope[1], block.row.step.id)} row={block.row} live={live} current={block.row.step === active} scope={rowScope} suspended={suspended} mutation={mutation} waiting={false}/>;
      if (block.kind === "text") return <Narration key={block.step.id} step={block.step}/>;
      if (block.kind === "peer") return <PeerSendNotice key={block.step.id} step={block.step} scope={rowScope} live={live}/>;
      if (block.kind === "inbound") return <PeerInboundNotice key={block.step.id} step={block.step} scope={rowScope}/>;
      const first = block.rows[0].step;
      return <ToolRun key={disclosureKey(rowScope[0], rowScope[1], `run:${first.callId || first.id}`)} rows={block.rows} live={live} activeId={active?.id || null}
        scope={rowScope} suspended={suspended} mutation={mutation} waiting={waiting}/>;
    })}
    {showStatus ? <div className="execution-trace__waiting turn-timeline__status" role="status">
      {suspended ? <Icon name="pause"/> : <TraceProgress mutation={mutation}/>}<span>{suspended || "Working"}</span>
      {!suspended ? <Elapsed start={turnStartedAt}/> : null}
    </div> : null}
    {tracePersistence === "pending" || tracePersistence === "failed" ? <small className="trace-persistence" role="status">{tracePersistence === "pending" ? "Activity details awaiting confirmation" : "Activity details not confirmed saved"}</small> : null}
  </div>;
}
