import type {CSSProperties} from "react";
import {useControlMotion} from "./useControlMotion";

/* Ten-cell braille rotation, 80ms per cell — the classic terminal "dots" cadence. */
const FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"] as const;
const INTERVAL_MS = 80;

/**
 * One-character live spinner animated entirely on the compositor: every frame
 * is in the DOM and a stepped translate scrolls the strip, so there is no
 * timer and no per-frame text mutation. Paused (first frame) when motion is
 * reduced or the window is not attentive.
 */
export function BrailleSpinner({className = "", label}: {className?: string; label?: string}) {
  const motion = useControlMotion();
  const vars = {"--braille-frames": FRAMES.length, "--braille-duration": `${FRAMES.length * INTERVAL_MS}ms`} as CSSProperties;
  return <span className={`braille-spinner ${className}`.trim()} role={label ? "img" : undefined} aria-label={label} aria-hidden={label ? undefined : true}>
    <span className="braille-spinner__window" data-paused={motion ? undefined : "true"}>
      <span className="braille-spinner__strip" style={vars}>{FRAMES.map(frame => <span key={frame}>{frame}</span>)}</span>
    </span>
  </span>;
}
