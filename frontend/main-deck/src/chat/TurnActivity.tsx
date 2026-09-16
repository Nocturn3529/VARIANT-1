import {useEffect, useMemo, useRef, useState} from "react";
import {RichText, thoughtPreview} from "./RichText";
import {canRevealPaneForStep, revealPaneForStep} from "../workbench/activityRouting";
import {formatActivityDuration, stepFailed} from "./activityModel";
import {traceRows, traceSummary} from "./traceModel";
import {traceActionLabel} from "./traceLabels";
import {Icon} from "../ui/Icon";
import type {ChatTurnStep} from "./types";
import {useChatState} from "../chatStore";
import {PEER_DELIVERY} from "../peers/peerModels";

// Virtualized turns retain disclosures across scroll and overlay visits.
const disclosures = new Map<string, boolean>();
function remember(key: string, open: boolean) {
  if (disclosures.size >= 600 && !disclosures.has(key)) disclosures.delete(disclosures.keys().next().value!);
  disclosures.set(key, open);
}
function useElapsed(active: boolean, startedAt: number): number {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  return Math.max(0, now - startedAt);
}
function timeMs(value?: number) {
  const number = Number(value) || 0;
  return number > 0 && number < 1e12 ? number * 1000 : number;
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

function TraceEntry({row, live, suspended = "", mutation = false}: {row: ReturnType<typeof traceRows>[number]; live: boolean; suspended?: string; mutation?: boolean}) {
  const {step, presentation, headline} = row;
  const thought = step.kind === "thinking";
  const running = live && !suspended && step.status === "running";
  const failed = stepFailed(step);
  const key = step.callId || step.id;
  const [choice, setChoice] = useState<boolean | null>(() => disclosures.get(key) ?? null);
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
  return <section className={`trace-entry${thought ? " trace-entry--thought" : ""}${open ? " is-open" : ""}${failed ? " is-error" : ""}${running ? " is-running" : ""}${suspended && step.status === "running" ? " is-suspended" : ""}`} data-conversation-scaffold="" data-trace-id={step.id}>
    <div className="trace-entry__heading">
      <button type="button" className="trace-entry__disclosure" aria-label={`${label}${presentation.python ? `, Python cell${presentation.executionCount !== null ? ` ${presentation.executionCount}` : ""}` : ""}${suspended && step.status === "running" ? `, ${suspended}` : ""}`}
        aria-expanded={hasDetails ? open : undefined} disabled={!hasDetails} onClick={() => { remember(key, !open); setChoice(!open); }}>
        <span className="trace-entry__status" aria-hidden="true">{running ? <TraceProgress mutation={mutation}/> : <Icon name={failed ? "error" : step.status === "running" && suspended ? "pause" : thought ? "thought" : step.status === "running" ? "stop" : "check"}/>}</span>
        <span className="trace-entry__name">{label}</span>
        {thought ? <span className="trace-entry__headline">{previewText}</span> : null}
        <small>{[failed ? "Error" : running ? "Running" : step.status === "running" ? suspended || "Interrupted" : "", duration].filter(Boolean).join(" · ")}</small>
        {hasDetails ? <Icon className={open ? "is-expanded" : ""} name="chevron"/> : null}
      </button>
      {!thought && canRevealPaneForStep(step) ? <button type="button" className="trace-entry__related" aria-label={`Open related surface for ${headline}`} title="Open related surface" onClick={() => revealPaneForStep(step)}><Icon name="popout"/></button> : null}
    </div>
    {open && hasDetails ? <div className="trace-entry__body">
      {thought ? <ThoughtText text={step.detail || ""} streaming={running} preview={preview}/> : <>
        <dl className="trace-entry__metadata">
          {presentation.executionCount !== null ? <div><dt>Python cell</dt><dd>{presentation.executionCount}</dd></div> : null}
          {presentation.generation !== null ? <div><dt>Kernel</dt><dd>generation {presentation.generation}</dd></div> : null}
          <div><dt>Status</dt><dd>{running ? "Running" : failed ? "Error" : step.status === "running" ? suspended || "Interrupted" : "Complete"}</dd></div>
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

export function TurnActivity({steps, live, streamText, turnStartedAt,ownerLabel=""}: {
  steps: readonly ChatTurnStep[]; live: boolean; streamText: string; turnStartedAt: number;ownerLabel?:string;
}) {
  const rows = traceRows(steps);
  const summary = traceSummary(steps);
  const chat = useChatState();
  const mutation = !!chat.runtime?.mutationEffectiveEnabled;
  const suspended = live ? !chat.connected ? "Reconnecting" : chat.pause?.state === "paused" ? "Paused" : "" : "";
  const liveLabel = suspended || (chat.stopPending ? "Stopping" : chat.pause?.state === "pausing" ? "Pausing" : "Working");
  const key = `run:${steps[0]?.callId || steps[0]?.id || turnStartedAt}`;
  const [choice, setChoice] = useState<boolean | null>(() => disclosures.get(key) ?? null);
  const previousErrors = useRef(summary.errors);
  useEffect(() => {
    if (summary.errors > previousErrors.current) { remember(key, true); setChoice(true); }
    previousErrors.current = summary.errors;
  }, [summary.errors, key]);
  const open = choice ?? (live || summary.errors > 0);
  const elapsed = useElapsed(live && !suspended, turnStartedAt);
  if (!rows.length) return live && !streamText ? <div className="turn-status" role="status">{suspended ? <Icon name="pause"/> : <TraceProgress mutation={mutation}/>}<span>{ownerLabel ? `${ownerLabel} · ${liveLabel}` : liveLabel}</span><em>{formatActivityDuration(elapsed)}</em></div> : null;
  return <div className={`turn-activity-stack execution-trace${live && !suspended ? " is-live" : ""}`}>
    <button type="button" className="execution-trace__summary" aria-label="Execution trace" aria-expanded={open}
      onClick={() => { remember(key, !open); setChoice(!open); }}>
      {live && !suspended ? <TraceProgress mutation={mutation}/> : <Icon name={suspended ? "pause" : "kernel"}/>}
      {ownerLabel ? <strong className="execution-trace__owner">{ownerLabel}</strong> : null}<span className="execution-trace__counts">{summary.label}</span>{live ? <small>{liveLabel}{elapsed > 0 ? ` · ${formatActivityDuration(elapsed)}` : ""}</small> : null}
      <Icon name="chevron" className={open ? "is-expanded" : ""}/>
    </button>
    {open ? <div className="execution-trace__rows">{rows.map(row => <div key={row.step.callId || row.step.id}>
      {row.boundary ? <div className="execution-trace__generation">{row.boundary}</div> : null}
      {row.step.peerMessage ? <details className="peer-send-trace"><summary><Icon name="send"/><span>Message to {row.step.peerMessage.target_display_name || "Peer agent"}</span><small>{PEER_DELIVERY[row.step.peerMessage.state]?.label || row.step.peerMessage.state}</small><Icon name="chevron"/></summary><p>{row.step.peerMessage.content}</p></details> : <TraceEntry row={row} live={live} suspended={suspended} mutation={mutation}/>}
    </div>)}{live && !rows.some(row => row.step.status === "running") ? <div className="execution-trace__waiting" role="status">{suspended ? <Icon name="pause"/> : <TraceProgress mutation={mutation}/>}<span>{liveLabel}</span></div> : null}</div> : null}
  </div>;
}
