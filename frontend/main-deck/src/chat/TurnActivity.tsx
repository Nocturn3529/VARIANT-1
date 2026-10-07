import {useEffect, useMemo, useRef, useState} from "react";
import {RichText, thoughtPreview} from "./RichText";
import {canRevealPaneForStep, revealPaneForStep} from "../workbench/activityRouting";
import {formatActivityDuration, stepFailed, activityStatusLabel} from "./activityModel";
import {traceRows, traceSummary} from "./traceModel";
import {traceActionLabel} from "./traceLabels";
import {Icon} from "../ui/Icon";
import type {ChatTurnStep} from "./types";
import {useChatSelection, shallowChatSelection} from "../chatStore";
import {useElapsed} from "./elapsedClock";
import {PEER_DELIVERY} from "../peers/peerModels";
import {requestPeer, usePeers} from "../peers/peerStore";
import {disclosureKey, disclosureChoice, rememberDisclosure as remember} from "./disclosures";

// Virtualized turns retain disclosures across scroll and overlay visits.
function timeMs(value?: number) {
  const number = Number(value) || 0;
  return number > 0 && number < 1e12 ? number * 1000 : number;
}

type TraceRow = ReturnType<typeof traceRows>[number];

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

function TraceEntry({row, live, scope, suspended = "", mutation = false}: {row: TraceRow; live: boolean; scope: readonly [string,string]; suspended?: string; mutation?: boolean}) {
  const {step, presentation, headline} = row;
  const thought = step.kind === "thinking";
  const running = live && !suspended && step.status === "running";
  const failed = stepFailed(step);
  const key = disclosureKey(scope[0], scope[1], step.callId || step.id);
  const [choice, setChoice] = useState<boolean | null>(() => disclosureChoice(key) ?? null);
  const [sawLive, setSawLive] = useState(thought && running);
  useEffect(() => { if (thought && running) setSawLive(true); }, [thought, running]);
  const open = choice ?? (failed || (thought && (running || sawLive)));
  const preview = choice === null && thought && (running || sawLive);
  const previewText = useMemo(() => thought ? thoughtPreview(step.detail || "") : "", [thought, step.detail]);
  const startedAt = useRef(timeMs(step.startedAt || step.ts) || Date.now());
  const previousFailure = useRef(failed);
  useEffect(() => {
    if (failed && !previousFailure.current) { remember(key, true); setChoice(true); }
    previousFailure.current = failed;
  }, [failed, key]);
  const elapsed = useElapsed(running, startedAt.current);
  const duration = formatActivityDuration(running ? elapsed : step.durationMs);
  const hasDetails = !!(step.argsPreview || step.resultPreview || step.detail);
  const label = thought ? (running ? "Thinking" : step.summaryState === "discarded" ? "Discarded thought" : step.summaryState === "cancelled" ? "Interrupted thought" : "Thought") : traceActionLabel(step, live);
  // A Python cell says which cell it was: its first meaningful line.
  const cellLine = presentation.python && headline !== "Execute Python" ? headline : "";
  const Heading = hasDetails ? "button" : "div";
  return <section className={`trace-entry${thought ? " trace-entry--thought" : ""}${open ? " is-open" : ""}${failed ? " is-error" : ""}${running ? " is-running" : ""}${suspended && step.status === "running" ? " is-suspended" : ""}`} data-conversation-scaffold="" data-trace-id={step.id}>
    <div className="trace-entry__heading">
      <Heading {...(hasDetails ? {type:"button" as const,onClick:()=>{remember(key, !open);setChoice(!open);}} : {tabIndex:0,role:"group"})} className="trace-entry__disclosure" aria-label={`${label}, ${running ? "Running" : step.status === "running" ? suspended || "Interrupted" : activityStatusLabel(step.status)}${duration ? `, ${duration}` : ""}${presentation.python ? `, Python cell${presentation.executionCount !== null ? ` ${presentation.executionCount}` : ""}` : ""}`}
        aria-expanded={hasDetails ? open : undefined}>
        <span className="trace-entry__status" aria-hidden="true">{running ? <TraceProgress mutation={mutation}/> : <Icon name={failed ? "error" : step.status === "cancelled" || step.status === "interrupted" ? "stop" : step.status === "skipped" || step.status === "degraded" || step.status === "unknown" ? "pause" : step.status === "running" && suspended ? "pause" : thought ? "thought" : step.status === "running" ? "stop" : "check"}/>}</span>
        <span className="trace-entry__name" title={label}>{label}</span>
        {thought ? <span className="trace-entry__headline">{previewText}</span> : cellLine ? <code className="trace-entry__headline" title={cellLine}>{cellLine}</code> : null}
        <small>{[running ? "Running" : step.status === "running" ? suspended || "Interrupted" : step.status && !["ok","done"].includes(step.status) ? activityStatusLabel(step.status) : "", duration].filter(Boolean).join(" · ")}</small>
        {hasDetails ? <Icon className={open ? "is-expanded" : ""} name="chevron"/> : null}
      </Heading>
      {!thought && canRevealPaneForStep(step) ? <button type="button" className="trace-entry__related" aria-label={`Open related surface for ${headline}`} title="Open related surface" onClick={() => revealPaneForStep(step)}><Icon name="popout"/></button> : null}
    </div>
    {open && hasDetails ? <div className="trace-entry__body">
      {thought ? <ThoughtText text={step.detail || ""} streaming={running} preview={preview}/> : <>
        <dl className="trace-entry__metadata">
          {presentation.executionCount !== null ? <div><dt>Python cell</dt><dd>{presentation.executionCount}</dd></div> : null}
          {presentation.generation !== null ? <div><dt>Kernel</dt><dd>generation {presentation.generation}</dd></div> : null}
          <div><dt>Status</dt><dd>{running ? "Running" : step.status === "running" ? suspended || "Interrupted" : activityStatusLabel(step.status)}</dd></div>
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
function PeerSendNotice({step}: {step: ChatTurnStep}) {
  const peer = step.peerMessage!;
  const name = peer.target_display_name || "another agent";
  const state = PEER_DELIVERY[peer.state]?.label || peer.state;
  return <details className={`peer-send-trace${peer.state === "failed" ? " is-error" : ""}`} data-conversation-scaffold="" data-trace-id={step.id}>
    <summary aria-label={`Sent a message to ${name}, ${state}`}><Icon name="send"/><span>Sent a message to <strong>{name}</strong></span><small>{state}</small><Icon name="chevron"/></summary>
    <p>{peer.content}</p>
  </details>;
}

/** A message from another agent that steered into this run, where it landed. */
function PeerInboundNotice({step}: {step: ChatTurnStep}) {
  const inbound = step.peerInbound!;
  const peers = usePeers();
  const sessionId = useChatSelection(state => state.sessionId);
  const receipt = peers.messages[inbound.message_id];
  const content = inbound.content ?? (receipt?.sender_peer_id === inbound.peer_id ? receipt.content : undefined);
  useEffect(() => {
    if (content !== undefined || !peers.connected || !sessionId) return;
    requestPeer(sessionId, {operation: "inspect", message_id: inbound.message_id});
  }, [content, peers.connected, sessionId, inbound.message_id]);
  const name = inbound.display_name || "another agent";
  return <details className="peer-send-trace peer-receive-trace" open data-conversation-scaffold="" data-trace-id={step.id}>
    <summary aria-label={`Message from ${name}, steered into this task`}><Icon name="peers"/><span>Message from <strong>{name}</strong></span><small>Steered in</small><Icon name="chevron"/></summary>
    <p>{content ?? (peers.connected ? "Loading message…" : "Message unavailable while offline.")}</p>
  </details>;
}

/**
 * Consecutive tool calls read as one run: a summary line that opens to the
 * rows. While the run is live a single ticker row shows the current call, so
 * a mission that runs forty cells stays one line instead of a growing list.
 */
function ToolRun({rows, live, latest, scope, suspended, mutation, liveLabel}: {
  rows: TraceRow[]; live: boolean; latest: boolean; scope: readonly [string, string];
  suspended: string; mutation: boolean; liveLabel: string;
}) {
  const steps = rows.map(row => row.step);
  const summary = traceSummary(steps);
  const first = steps[0];
  const key = disclosureKey(scope[0], scope[1], `run:${first.callId || first.id}`);
  const [choice, setChoice] = useState<boolean | null>(() => disclosureChoice(key) ?? null);
  useEffect(() => {setChoice(disclosureChoice(key) ?? null);}, [key]);
  const previousErrors = useRef(summary.errors);
  useEffect(() => {
    if (summary.errors > previousErrors.current) { remember(key, true); setChoice(true); }
    previousErrors.current = summary.errors;
  }, [summary.errors, key]);
  const running = live && !suspended && steps.some(step => step.status === "running");
  const active = running || (live && latest);
  if (rows.length === 1) return <div className="tool-run is-single">
    {rows[0].boundary ? <div className="execution-trace__generation">{rows[0].boundary}</div> : null}
    <TraceEntry row={rows[0]} live={live} scope={scope} suspended={suspended} mutation={mutation}/>
  </div>;
  const open = choice ?? summary.errors > 0;
  const total = steps.reduce((sum, step) => sum + (step.durationMs || 0), 0);
  const current = [...rows].reverse().find(row => row.step.status === "running") || rows.at(-1)!;
  const notices = steps.filter(step => stepFailed(step) || ["cancelled","interrupted","degraded","unknown"].includes(step.status || "")).slice(-2);
  return <div className={`tool-run execution-trace${active ? " is-live" : ""}`}>
    <button type="button" className="execution-trace__summary" aria-label={`Tool activity, ${summary.label}${active ? `, ${liveLabel}` : ""}`} aria-expanded={open}
      onClick={() => { remember(key, !open); setChoice(!open); }}>
      {running ? <TraceProgress mutation={mutation}/> : <Icon name={summary.errors ? "error" : "kernel"}/>}
      <span className="execution-trace__counts">{summary.label}</span>
      <small>{active ? liveLabel : formatActivityDuration(total)}</small>
      <Icon name="chevron" className={open ? "is-expanded" : ""}/>
    </button>
    {!open && active ? <div className="tool-run__ticker" role="status"><TraceEntry key={current.step.callId || current.step.id} row={current} live={live} scope={scope} suspended={suspended} mutation={mutation}/></div> : null}
    {!open && !active && notices.length ? <div className="execution-trace__alerts" role="status">{notices.map(step => <p key={step.id} className={stepFailed(step) ? "is-error" : ""}>{traceActionLabel(step, false)}</p>)}</div> : null}
    {open ? <div className="execution-trace__rows">{rows.map(row => <div key={disclosureKey(scope[0], scope[1], row.step.callId || row.step.id)}>
      {row.boundary ? <div className="execution-trace__generation">{row.boundary}</div> : null}
      <TraceEntry row={row} live={live} scope={scope} suspended={suspended} mutation={mutation}/>
    </div>)}</div> : null}
  </div>;
}

export function TurnActivity({steps, live, streamText, turnStartedAt,ownerLabel="",tracePersistence,scope="",shownPeerMessages}: {
  steps: readonly ChatTurnStep[]; live: boolean; streamText: string; turnStartedAt: number;ownerLabel?:string;
  tracePersistence?: "pending" | "saved" | "failed";
  scope?: string;
  /** Peer messages this turn already shows as transcript cards. */
  shownPeerMessages?: ReadonlySet<string>;
}) {
  const blocks = useMemo(() => timelineBlocks(steps, shownPeerMessages), [steps, shownPeerMessages]);
  const chat = useChatSelection(state=>({sessionId:state.sessionId,runtime:state.runtime,connected:state.connected,pause:state.pause,stopPending:state.stopPending}),shallowChatSelection);
  const mutation = !!chat.runtime?.mutationEffectiveEnabled;
  const suspended = live ? !chat.connected ? "Reconnecting" : chat.pause?.state === "paused" ? "Paused" : "" : "";
  const liveLabel = suspended || (chat.stopPending ? "Stopping" : chat.pause?.state === "pausing" ? "Pausing" : "Working");
  const fallbackScope = useRef(String(turnStartedAt));
  const rowScope = [chat.sessionId || "", scope || fallbackScope.current] as const;
  const elapsed = useElapsed(live && !suspended, turnStartedAt);
  if (!blocks.length) return live && !streamText ? <div className="turn-status" role="status">{suspended ? <Icon name="pause"/> : <TraceProgress mutation={mutation}/>}<span>{ownerLabel ? `${ownerLabel} · ${liveLabel}` : liveLabel}</span><em>{formatActivityDuration(elapsed)}</em></div> : null;
  const running = steps.some(step => step.status === "running");
  return <div className={`turn-timeline turn-activity-stack${live && !suspended ? " is-live" : ""}`}>
    {ownerLabel ? <strong className="execution-trace__owner">{ownerLabel}</strong> : null}
    {blocks.map((block, index) => {
      if (block.kind === "thought") return <TraceEntry key={disclosureKey(rowScope[0], rowScope[1], block.row.step.id)} row={block.row} live={live} scope={rowScope} suspended={suspended} mutation={mutation}/>;
      if (block.kind === "text") return <Narration key={block.step.id} step={block.step}/>;
      if (block.kind === "peer") return <PeerSendNotice key={block.step.id} step={block.step}/>;
      if (block.kind === "inbound") return <PeerInboundNotice key={block.step.id} step={block.step}/>;
      const first = block.rows[0].step;
      return <ToolRun key={`run:${first.callId || first.id}`} rows={block.rows} live={live} latest={index === blocks.length - 1}
        scope={rowScope} suspended={suspended} mutation={mutation} liveLabel={liveLabel}/>;
    })}
    {live && (suspended || (!running && !streamText)) ? <div className="execution-trace__waiting turn-timeline__status" role="status">{suspended ? <Icon name="pause"/> : <TraceProgress mutation={mutation}/>}<span>{liveLabel}</span>{elapsed > 0 ? <em>{formatActivityDuration(elapsed)}</em> : null}</div> : null}
    {tracePersistence === "pending" || tracePersistence === "failed" ? <small className="trace-persistence" role="status">{tracePersistence === "pending" ? "Activity details awaiting confirmation" : "Activity details not confirmed saved"}</small> : null}
  </div>;
}
