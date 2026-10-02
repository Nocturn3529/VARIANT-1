import {useControlMotion} from "./useControlMotion";

export type GlyphState = "running" | "queued" | "thinking" | "done" | "failed" | "stopped" | "blocked" | "attention" | "idle";

/*
 * Code-drawn state marks on a 16px grid. Live states animate only while work
 * is genuinely live and the window is attentive; settled states draw once.
 * Color comes from the parent (currentColor), so the same glyph serves
 * monochrome rows and the deliberate live accent.
 */
export function StatusGlyph({state, size = 14, label, className = ""}: {
  state: GlyphState;
  size?: number;
  /** Accessible name; omit when adjacent text already states the status. */
  label?: string;
  className?: string;
}) {
  const motion = useControlMotion();
  return <svg
    className={`status-glyph ${className}`.trim()}
    data-state={state}
    data-motion={motion ? "on" : "off"}
    width={size}
    height={size}
    viewBox="0 0 16 16"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.5"
    strokeLinecap="round"
    strokeLinejoin="round"
    role={label ? "img" : undefined}
    aria-label={label}
    aria-hidden={label ? undefined : true}
    focusable="false"
  >
    {state === "running" ? <>
      <circle className="status-glyph__track" cx="8" cy="8" r="5.5"/>
      <path className="status-glyph__arc" d="M8 2.5a5.5 5.5 0 0 1 5.5 5.5"/>
    </> : null}
    {state === "thinking" ? <circle className="status-glyph__orbit" cx="8" cy="8" r="5.5" strokeDasharray="2 3.2"/> : null}
    {state === "queued" ? <g className="status-glyph__dots" stroke="none" fill="currentColor">
      <circle cx="3.5" cy="8" r="1.25"/><circle cx="8" cy="8" r="1.25"/><circle cx="12.5" cy="8" r="1.25"/>
    </g> : null}
    {state === "done" ? <path className="status-glyph__draw" d="m3.5 8.5 3 3 6-7" pathLength={1}/> : null}
    {state === "failed" ? <path className="status-glyph__draw" d="m4.5 4.5 7 7m0-7-7 7" pathLength={1}/> : null}
    {state === "stopped" ? <rect x="4.5" y="4.5" width="7" height="7" rx="1.25"/> : null}
    {state === "blocked" ? <>
      <circle cx="8" cy="8" r="5.5"/>
      <path d="M4.2 11.8 11.8 4.2"/>
    </> : null}
    {state === "attention" ? <>
      <circle className="status-glyph__ping" cx="8" cy="8" r="5.5"/>
      <circle cx="8" cy="8" r="2" stroke="none" fill="currentColor"/>
    </> : null}
    {state === "idle" ? <circle cx="8" cy="8" r="2" stroke="none" fill="currentColor"/> : null}
  </svg>;
}
