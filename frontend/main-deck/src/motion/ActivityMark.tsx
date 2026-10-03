import {BrailleSpinner} from "./BrailleSpinner";

export type MarkState = "live" | "queued" | "done" | "failed" | "stopped" | "attention" | "idle";

/** Leading status mark: a braille spinner while live, otherwise a quiet dot. */
export function ActivityMark({state, label, className = ""}: {state: MarkState; label?: string; className?: string}) {
  if (state === "live") return <BrailleSpinner className={`activity-mark activity-mark--live ${className}`.trim()} label={label}/>;
  return <span className={`activity-mark activity-mark--${state} ${className}`.trim()} role={label ? "img" : undefined}
    aria-label={label} aria-hidden={label ? undefined : true}><i/></span>;
}
