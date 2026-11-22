import {activityPresentation} from "./activityPresentation";
import {currentStepLabel, activityStatusLabel, stepFailed} from "./activityModel";
import type {ChatTurnStep} from "./types";

type Action = {present: string; past: string; failed: string; plural: string; file?: boolean};
const python: Action = {present: "Running Python", past: "Ran Python", failed: "Python cell failed", plural: "Ran Python # times"};
const actions: Array<[RegExp, Action]> = [
  [/^browser\.(?:navigate|session\.new_page)$/, {present: "Opening browser page", past: "Opened browser page", failed: "Browser navigation failed", plural: "Opened # browser pages"}],
  [/^browser\.(?:read|observe|session\.pages)$/, {present: "Reading browser page", past: "Read browser page", failed: "Browser read failed", plural: "Read browser pages # times"}],
  [/^browser\.(?:click|type|fill|scroll|press)$/, {present: "Interacting with browser", past: "Interacted with browser", failed: "Browser action failed", plural: "Used the browser # times"}],
  [/^(?:(?:tools|files|fs)\.)?(?:read_file|read_text)$/, {present: "Reading file", past: "Read file", failed: "File read failed", plural: "Read files # times", file: true}],
  [/^(?:(?:tools|files|fs)\.)?(?:write_file|write_text|edit_file|apply_patch|exact_replace)$/, {present: "Editing file", past: "Edited file", failed: "File edit failed", plural: "Made # file edits", file: true}],
  [/^(?:(?:tools|terminal|shell)\.)?(?:run_command|execute|exec|run)$/, {present: "Running command", past: "Ran command", failed: "Command failed", plural: "Ran # commands"}],
  [/^(?:(?:tools|children)\.)?(?:spawn|spawn_agent|delegate_task)$/, {present: "Starting subagent", past: "Started subagent", failed: "Subagent start failed", plural: "Started # subagents"}],
  [/^peers\.(?:list|inbox|inspect_message|inspect_request|get)$/, {present: "Checking peers", past: "Checked peers", failed: "Peer lookup failed", plural: "Checked peers # times"}],
];

function actionFor(step: ChatTurnStep, presentation: ReturnType<typeof activityPresentation>) {
  let name = step.tool || "";
  if (presentation.python) {
    if (step.cellAction && (step.status === "ok" || step.status === "done")) {
      // A cell can read several paths or run several commands. Only its host summary is established.
      return step.cellAction === python.past ? python : {...python, past: step.cellAction, plural: `${step.cellAction} (# cells)`};
    }
    // Only a call at the start of a statement (optionally assigned), not keywords in strings/comments.
    name = presentation.input?.match(/^\s*(?:[A-Za-z_]\w*\s*=\s*)?(?:await\s+)?([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*\(/)?.[1] || "";
  }
  return actions.find(([pattern]) => pattern.test(name))?.[1] || (presentation.python ? python : null);
}

/** Only name a file when this operation has a file target. */
function withFile(label: string, step: ChatTurnStep, action: Action, presentation: ReturnType<typeof activityPresentation>): string {
  if (!action.file) return label;
  // A Python variable can hold any path. Only show a filename supplied as an
  // argument or identified as file evidence by the activity projection.
  const explicit = presentation.python
    ? presentation.input?.match(/^[^\n(]*\b(?:read_file|read_text|write_file|write_text|edit_file|exact_replace)\(\s*(?:(?:path|file|file_path)\s*=\s*)?r?["']([^"'\n]+)["']/)?.[1]
    : presentation.filePath;
  const file = (explicit || step.evidence?.find(item => item.kind === "file")?.value)?.split(/[\\/]/).pop()?.slice(0, 90);
  return file ? label.replace(/file$/, () => file) : label;
}

export function completedAction(step: ChatTurnStep, presentation: ReturnType<typeof activityPresentation>) {
  const action = actionFor(step, presentation);
  return action ? {label: withFile(action.past, step, action, presentation), plural: action.plural} : null;
}

/** Conservative labels for explicit calls; arbitrary Python remains simply “Ran Python”. */
export function traceActionLabel(step: ChatTurnStep, live: boolean, presentation = activityPresentation(step)): string {
  const action = actionFor(step, presentation);
  if (!action) return `${currentStepLabel(step, live, presentation)}${step.status && !["running","ok","done"].includes(step.status) ? ` · ${activityStatusLabel(step.status)}` : ""}`;
  if (stepFailed(step)) return step.status === "timed_out" ? `${action.present} · timed out` : action.failed;
  if (step.status && !["running","ok","done"].includes(step.status)) return `${action.present} · ${activityStatusLabel(step.status).toLowerCase()}`;
  if (step.status === "running") return live ? withFile(action.present, step, action, presentation) : `${action.present} · interrupted`;
  return withFile(action.past, step, action, presentation);
}
