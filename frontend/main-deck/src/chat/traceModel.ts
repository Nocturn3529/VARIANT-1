import {activityPresentation} from "./activityPresentation";
import {currentStepLabel, stepFailed} from "./activityModel";
import {completedAction} from "./traceLabels";
import type {ChatTurnStep} from "./types";

/** A steer can divide one live turn into several timeline segments. Select its cue once. */
export function currentTraceStep(steps: readonly ChatTurnStep[]): ChatTurnStep | null {
  for (let i = steps.length - 1; i >= 0; i--) {
    const step = steps[i];
    if (step.status !== "running" || step.peerInbound || step.peerMessage) continue;
    if (step.kind === "tool" || (step.kind === "thinking" && step.detail?.trim()) ||
        (step.kind === "step" && i === steps.length - 1)) return step;
  }
  return null;
}

export function traceRows(steps: readonly ChatTurnStep[]) {
  let previous: number | null = null;
  return steps.filter(step => step.kind !== "thinking" || step.detail?.trim()).map(step => {
    const presentation = activityPresentation(step);
    const generation = presentation.generation;
    const boundary = generation !== null && generation !== previous
      ? previous === null ? `Kernel ${String(generation).padStart(2, "0")}`
        : `Kernel ${String(previous).padStart(2, "0")} → ${String(generation).padStart(2, "0")}` : "";
    if (generation !== null) previous = generation;
    const firstLine = presentation.input?.split(/\r?\n/).find(line => line.trim())?.trim() || "";
    const headline = presentation.python
      ? firstLine.replace(/^#\s*/, "") || "Execute Python"
      : currentStepLabel(step, step.status === "running");
    return {step, presentation, boundary, headline: headline.length > 96 ? `${headline.slice(0, 95)}…` : headline};
  });
}
export function traceSummary(steps: readonly ChatTurnStep[]) {
  const actions = steps.filter(step => step.kind === "tool" || step.kind === "step");
  let cells = 0;
  const errors = actions.filter(stepFailed).length || (steps.some(stepFailed) ? 1 : 0);
  const stopped = actions.filter(step => step.status === "cancelled" || step.status === "interrupted").length
    || (steps.some(step => step.status === "cancelled" || step.status === "interrupted") ? 1 : 0);
  const omitted = steps.reduce((count, step) => count + (step.omittedBefore || 0), 0);
  const thoughts = steps.filter(step => step.kind === "thinking" && step.detail?.trim()).length;
  const groups = new Map<string, {first: string; count: number}>();
  for (const step of actions) {
    const presentation = activityPresentation(step);
    if (presentation.python) cells++;
    if (stepFailed(step) || step.status === "cancelled" || step.status === "interrupted") continue;
    // A grouping label must not describe an unsuccessful or pending action as completed.
    const action = (step.status === "ok" || step.status === "done") && completedAction(step, presentation);
    const plural = action ? action.plural : "# actions";
    const group = groups.get(plural);
    if (group) group.count++;
    else groups.set(plural, {first: action ? action.label : "1 action", count: 1});
  }
  const phrases = Array.from(groups, ([plural, {first, count}]) => count === 1 ? first : plural.replace("#", String(count)));
  const parts = [phrases.map((phrase, index) => index ? phrase.charAt(0).toLowerCase() + phrase.slice(1) : phrase).join(", "),
    thoughts ? `${thoughts} ${thoughts === 1 ? "thought" : "thoughts"}` : "", errors ? `${errors} ${errors === 1 ? "issue" : "issues"}` : "", stopped ? `${stopped} stopped` : "", omitted ? `${omitted} earlier events omitted` : ""].filter(Boolean);
  return {cells, errors, label: parts.join(" · ") || "Execution details"};
}
