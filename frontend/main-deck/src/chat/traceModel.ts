import {activityPresentation} from "./activityPresentation";
import {currentStepLabel, stepFailed} from "./activityModel";
import type {ChatTurnStep} from "./types";

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
  const actions = steps.filter(step => step.kind !== "thinking");
  const cells = actions.filter(step => activityPresentation(step).python).length;
  const peers=actions.filter(step=>step.peerMessage).length;
  const tools = actions.length - cells - peers;
  const errors = actions.filter(stepFailed).length;
  const thoughts = steps.filter(step => step.kind === "thinking" && step.detail?.trim()).length;
  const parts = [thoughts ? `${thoughts} ${thoughts === 1 ? "thought" : "thoughts"}` : "", cells ? `${cells} Python ${cells === 1 ? "cell" : "cells"}` : "",
    tools ? `${tools} ${tools === 1 ? "action" : "actions"}` : "", peers ? `${peers} peer ${peers===1 ? "message" : "messages"}` : "", errors ? `${errors} ${errors === 1 ? "issue" : "issues"}` : ""].filter(Boolean);
  return {cells, errors, label: parts.join(" · ") || "Execution details"};
}
