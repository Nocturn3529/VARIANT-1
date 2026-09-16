export const MUTATION_INK = "#39ff14";
export type KernelPhase = "idle" | "running" | "waiting" | "complete" | "offline";
export function kernelSeed(value: string): number {
  let hash = 2166136261;
  for (let i = 0; i < value.length; i++) hash = Math.imul(hash ^ value.charCodeAt(i), 16777619);
  return hash >>> 0;
}

/** A seeded, closed harmonic filament. Geometry never represents guessed progress. */
export function kernelSamples(seed: number, time = 0, count = 168) {
  const a = (seed % 997) / 997 * Math.PI * 2;
  const b = ((seed >>> 8) % 991) / 991 * Math.PI * 2;
  const tilt = .72 + Math.sin(b) * .14;
  return Array.from({length: count}, (_, index) => {
    const t = index / count * Math.PI * 2;
    const radius = .66 + .13 * Math.cos(3 * t + a) + .025 * Math.sin(7 * t + b);
    const x = radius * Math.cos(2 * t);
    const y = radius * Math.sin(2 * t);
    const z = .33 * Math.sin(3 * t + a);
    const depth = y * Math.sin(tilt) + z * Math.cos(tilt);
    const pulse = Math.pow(Math.max(0, Math.cos(t - time * 1.4)), 14);
    return {x, y: y * Math.cos(tilt) - z * Math.sin(tilt), depth,
      shade: Math.min(1, .28 + (depth + .8) * .35 + pulse * .18),
      accent: index >= Math.floor(count * .22) && index < Math.floor(count * .24)};
  });
}
