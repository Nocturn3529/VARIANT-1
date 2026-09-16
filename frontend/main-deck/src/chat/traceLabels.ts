import {activityPresentation} from "./activityPresentation";
import {currentStepLabel} from "./activityModel";
import type {ChatTurnStep} from "./types";

type Action = {present: string; past: string; failed: string};
const python: Action = {present: "Running Python", past: "Ran Python", failed: "Python cell failed"};
const actions: Array<[RegExp, Action]> = [
  [/^browser\.(?:navigate|session\.new_page)$/, {present: "Opening browser page", past: "Opened browser page", failed: "Browser navigation failed"}],
  [/^browser\.(?:read|observe|session\.pages)$/, {present: "Reading browser page", past: "Read browser page", failed: "Browser read failed"}],
  [/^browser\.(?:click|type|fill|scroll|press)$/, {present: "Interacting with browser", past: "Interacted with browser", failed: "Browser action failed"}],
  [/^(?:(?:tools|files|fs)\.)?(?:read_file|read_text)$/, {present: "Reading file", past: "Read file", failed: "File read failed"}],
  [/^(?:(?:tools|files|fs)\.)?(?:write_file|write_text|edit_file|apply_patch|exact_replace)$/, {present: "Editing file", past: "Edited file", failed: "File edit failed"}],
  [/^(?:(?:tools|terminal|shell)\.)?(?:run_command|execute|exec|run)$/, {present: "Running command", past: "Ran command", failed: "Command failed"}],
  [/^(?:(?:tools|children)\.)?(?:spawn|spawn_agent|delegate_task)$/, {present: "Starting subagent", past: "Started subagent", failed: "Subagent start failed"}],
];

/** Conservative labels for explicit calls; arbitrary Python remains simply “Ran Python”. */
export function traceActionLabel(step: ChatTurnStep, live: boolean): string {
  const presentation = activityPresentation(step);
  let name = step.tool || "";
  if (presentation.python) {
    // Only a call at the start of a statement (optionally assigned), not keywords in strings/comments.
    name = presentation.input?.match(/^\s*(?:[A-Za-z_]\w*\s*=\s*)?(?:await\s+)?([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*\(/)?.[1] || "";
  }
  const action = actions.find(([pattern]) => pattern.test(name))?.[1] || (presentation.python ? python : null);
  if (!action) return currentStepLabel(step, live);
  if (step.status === "error") return action.failed;
  if (step.status === "running") return live ? action.present : `${action.present} · interrupted`;
  return action.past;
}
