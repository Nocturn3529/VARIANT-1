/** A bounded display projection that never hides its own omission. */
import type {ChatTurnStep} from "./types";

export const MAX_TURN_STEPS = 48;

export function retainTurnSteps(steps: ChatTurnStep[]): ChatTurnStep[] {
  const omitted = Math.min(1_000_000, steps.reduce((count, step) => count + (step.omittedBefore || 0), 0)
    + Math.max(0, steps.length - MAX_TURN_STEPS));
  const retained = steps.slice(-MAX_TURN_STEPS).map(step => {
    if (!step.omittedBefore) return step;
    const {omittedBefore: _omitted, ...rest} = step;
    return rest as ChatTurnStep;
  });
  if (omitted && retained.length) retained[0] = {...retained[0], omittedBefore: omitted};
  return retained;
}

export function boundedPreview(value: string, limit: number): string {
  if (value.length <= limit) return value;
  const marker = `\n[Preview truncated: ${value.length} characters total]`;
  const room = Math.max(0, limit - marker.length);
  const head = value.slice(0, room);
  const boundary = head.lastIndexOf("\n");
  return `${boundary > room / 2 ? head.slice(0, boundary) : head}${marker}`;
}
