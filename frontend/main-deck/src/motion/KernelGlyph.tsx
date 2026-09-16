import {useEffect, useMemo, useRef, useState} from "react";
import type p5 from "p5";
import {useReducedMotion} from "../state/appearanceStore";
import {useSurfaceDocument} from "../ui/SurfaceDocument";
import {kernelSamples, kernelSeed, MUTATION_INK, type KernelPhase} from "./kernelField";

export function KernelGlyph({seed = "variant-1", phase = "idle", mutation = false, size = 28, ascii = false}: {
  seed?: string; phase?: KernelPhase; mutation?: boolean; size?: number; ascii?: boolean;
}) {
  const host = useRef<HTMLSpanElement>(null);
  const owner = useSurfaceDocument();
  const reduced = useReducedMotion(owner);
  const [ready, setReady] = useState(false);
  const renderCanvas = phase === "running" || size >= 64;
  const still = useMemo(() => kernelSamples(kernelSeed(seed), 0, 72).sort((a, b) => a.depth - b.depth), [seed]);
  const current = useRef({phase, mutation, reduced, ascii});
  current.current = {phase, mutation, reduced, ascii};
  const refresh = useRef<() => void>(() => {});
  useEffect(() => refresh.current(), [phase, mutation, reduced, ascii]);
  useEffect(() => {
    const element = host.current;
    if (!element || !renderCanvas || typeof CanvasRenderingContext2D === "undefined") return;
    let disposed = false;
    let visible = true;
    let sketch: p5 | undefined;
    let time = 0;
    const sync = () => {
      if (!sketch || disposed) return;
      const showing = visible && owner.visibilityState !== "hidden";
      if (showing && !current.current.reduced && current.current.phase === "running") sketch.loop();
      else { sketch.noLoop(); if (showing) sketch.redraw(); }
    };
    refresh.current = sync;
    const observer = typeof IntersectionObserver !== "undefined" ? new IntersectionObserver(entries => { visible = !!entries[0]?.isIntersecting; sync(); }) : null;
    observer?.observe(element);
    owner.addEventListener("visibilitychange", sync);
    const seedValue = kernelSeed(seed);
    void import("./p5Runtime").then(({default: P5}) => {
      if (disposed) return;
      // Bundled production code needs neither source fetching nor the FES
      // evaluator. Keep the app's existing script/connect policy intact.
      P5.disableFriendlyErrors = true;
      sketch = new P5(p => {
        p.setup = () => {
          if (disposed) { p.remove(); return; }
          p.createCanvas(size, size); p.pixelDensity(Math.min(2, owner.defaultView?.devicePixelRatio || 1));
          p.randomSeed(seedValue); p.noiseSeed(seedValue); p.frameRate(18); p.noStroke();
          p.textAlign(p.CENTER, p.CENTER); p.textFont("monospace"); p.textSize(Math.max(7, size / 21));
          p.noLoop(); setReady(true); queueMicrotask(sync);
        };
        p.draw = () => {
          const state = current.current;
          if (state.phase === "running" && !state.reduced) time += Math.min(80, p.deltaTime || 0) / 1000;
          p.clear();
          const count = size < 40 ? 72 : 168;
          const points = kernelSamples(seedValue, time, count).sort((a, b) => a.depth - b.depth);
          for (const point of points) {
            if (state.mutation && point.accent) p.fill(MUTATION_INK);
            else p.fill(Math.round((state.phase === "offline" ? 110 : 238) * point.shade));
            const x = size / 2 + point.x * size * .52;
            const y = size / 2 + point.y * size * .52;
            if (state.ascii && size >= 64) p.text(point.shade > .72 ? "+" : point.shade > .48 ? ":" : ".", x, y);
            else p.circle(x, y, size < 40 ? 1.25 : 1.5 + point.shade);
          }
        };
      }, element);
      sync();
    }).catch(() => { /* The SVG remains available when canvas cannot initialize. */ });
    return () => { disposed = true; refresh.current = () => {}; observer?.disconnect(); owner.removeEventListener("visibilitychange", sync); sketch?.remove(); setReady(false); };
  }, [owner, seed, size, renderCanvas]);
  return <span className={`kernel-glyph kernel-glyph--${phase}${ready ? " is-ready" : ""}`} style={{width: size, height: size}} aria-hidden="true">
    <svg className="kernel-glyph__still" viewBox="0 0 100 100" aria-hidden="true">{still.map((point, index) => <circle key={index}
      cx={50 + point.x * 52} cy={50 + point.y * 52} r={1.1}
      fill={mutation && point.accent ? MUTATION_INK : `rgb(${Math.round(238 * point.shade)} ${Math.round(238 * point.shade)} ${Math.round(238 * point.shade)})`}/>)}</svg>
    <span ref={host} className="kernel-glyph__canvas"/>
  </span>;
}
