import {useMemo, useState} from "react";
import type {ChatTurnStep} from "./types";
import {activityStatusLabel, normalizeActivityStatus, stepFailed} from "./activityModel";
import {traceActionLabel} from "./traceLabels";
import {Icon} from "../ui/Icon";

/** Only results observed for this assistant message belong in its end-of-run summary. */
export function RunResults({steps}: {steps: readonly ChatTurnStep[]}) {
  const [open, setOpen] = useState(false);
  const results = useMemo(() => steps.filter(step => {
    if (step.kind !== "tool" || !step.callId || !step.status || step.status === "running") return false;
    if (step.rawStatus) return normalizeActivityStatus(step.rawStatus, "tool:result") === step.status;
    return step.cellAction || ((step.status === "ok" || step.status === "error") && step.resultPreview);
  }), [steps]);
  if (!results.length) return null;
  const failed = results.filter(stepFailed).length;
  const skipped = results.filter(step => step.status === "skipped").length;
  const partial = steps.some(step => step.omittedBefore);
  return <details className="message-results" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary className="trace-disclosure">
      <span>{results.length} {partial ? "retained " : ""}tool {results.length === 1 ? "result" : "results"}{failed ? ` · ${failed} failed` : ""}{skipped ? ` · ${skipped} skipped` : ""}</span>
      <Icon name="chevron" className="trace-caret"/>
    </summary>
    {open ? <ol>{results.map(step => <li key={step.id}>
      <strong>{traceActionLabel(step, false)}</strong> <small>{activityStatusLabel(step.status)}</small>
      {step.resultPreview ? <pre>{step.resultPreview}</pre> : null}
    </li>)}</ol> : null}
  </details>;
}
