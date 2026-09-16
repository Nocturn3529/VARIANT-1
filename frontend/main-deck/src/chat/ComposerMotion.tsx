import {useEffect, useRef, useState, type CSSProperties} from "react";
import {useReducedMotion} from "../state/appearanceStore";
import {Switch, type SwitchProps} from "../ui/Switch";
import type {MicPhase} from "../runtime/MicController";

function useControlMotion() {
  const reduced = useReducedMotion();
  const [visible, setVisible] = useState(() => document.visibilityState !== "hidden" && document.hasFocus());
  useEffect(() => {
    const update = () => setVisible(document.visibilityState !== "hidden" && document.hasFocus());
    const focus = () => setVisible(document.visibilityState !== "hidden");
    const blur = () => setVisible(false);
    // The Deck keeps background throttling off for browser/backend work, so
    // native focus events also gate these purely decorative animations.
    document.addEventListener("visibilitychange", update);
    window.addEventListener("focus",focus);window.addEventListener("blur",blur);
    return () => {document.removeEventListener("visibilitychange", update);window.removeEventListener("focus",focus);window.removeEventListener("blur",blur);};
  }, []);
  return !reduced && visible;
}

/** Decorative recording state, not a fabricated audio-level measurement. */
export function MicGlyph({phase}: {phase: MicPhase}) {
  const motion = useControlMotion();
  return <span className="composer-mic-glyph" data-phase={phase} data-motion={motion ? "on" : "off"} aria-hidden="true">
    <svg viewBox="0 0 28 28" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
      <g className="mic-glyph__symbol"><rect x="11" y="5" width="6" height="12" rx="3"/><path d="M7.5 13a6.5 6.5 0 0 0 13 0M14 19.5V23m-3 0h6"/></g>
      <g className="mic-glyph__wave">
        {[7,10.5,14,17.5,21].map((x,index) => <path key={x} d={`M${x} ${index===2 ? 5 : index%2 ? 8 : 10}V${index===2 ? 23 : index%2 ? 20 : 18}`} style={{"--wave-delay":`${index*-170}ms`} as CSSProperties}/>)}
      </g>
      <g className="mic-glyph__echo"><path d="M5 9a12 12 0 0 0 0 10M23 9a12 12 0 0 1 0 10"/></g>
      <circle className="mic-glyph__orbit" cx="14" cy="14" r="11" strokeDasharray="12 58"/>
    </svg>
  </span>;
}

const sparks = [
  [8,0,-8,-10],[35,0,-2,-12],[80,0,5,-9],[100,30,11,-4],
  [100,75,10,7],[70,100,4,10],[25,100,-4,11],[0,60,-10,4],
] as const;

/** A single activation flourish follows the acknowledged state, never a click. */
export function MutationSwitch({sessionId, ...props}: SwitchProps & {sessionId:string|null}) {
  const motion = useControlMotion();
  const previous = useRef({sessionId,checked:props.checked});
  const [activating,setActivating] = useState(false);
  useEffect(() => {
    const prior=previous.current;
    previous.current={sessionId,checked:props.checked};
    const activate=motion && !!sessionId && sessionId===prior.sessionId && !prior.checked && props.checked;
    setActivating(activate);
    if(!activate)return;
    const timer=setTimeout(()=>setActivating(false),1100);
    return ()=>clearTimeout(timer);
  },[sessionId,props.checked,motion]);

  return <Switch {...props} className={[props.className,activating ? "is-energizing" : "",motion ? "" : "control-motion-off"].filter(Boolean).join(" ")}
    decoration={<span className="mutation-decoration" aria-hidden="true">
      <span className="mutation-sweep"><i/></span>
      <svg className="mutation-lattice" viewBox="0 0 128 32" preserveAspectRatio="none" fill="none" stroke="currentColor">
        <path d="M0 24 12 8 24 24 36 8 48 24 60 8 72 24 84 8 96 24 108 8 120 24 128 12M0 8 12 24 24 8 36 24 48 8 60 24 72 8 84 24 96 8 108 24 120 8 128 20"/>
        {Array.from({length:10},(_,i)=><path key={i} d={`M${i*12+6} 12v8`} className="mutation-lattice__rung"/>)}
      </svg>
      <span className="mutation-sparks">{sparks.map(([left,top,x,y],i)=><i key={i} style={{left:`${left}%`,top:`${top}%`,"--spark-x":`${x}px`,"--spark-y":`${y}px`,"--spark-delay":`${i*28}ms`} as CSSProperties}/>)}</span>
    </span>}/>;
}
