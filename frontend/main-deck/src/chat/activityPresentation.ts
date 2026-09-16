import type {ChatTurnStep} from "./types";

function record(value?: string): Record<string, unknown> | null {
  if (!value) return null;
  try {
    const parsed: unknown = JSON.parse(value);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed as Record<string, unknown> : null;
  } catch { return null; }
}

/** Recover only a supplied code-string prefix when a bounded JSON preview is cut short. */
function codePrefix(value?: string): string | undefined {
  const match = value?.match(/^\s*\{\s*"(?:code|source)"\s*:\s*"((?:[^"\\]|\\(?:["\\/bfnrt]|u[\da-fA-F]{4}))*)/);
  if (!match) return undefined;
  try { return JSON.parse(`"${match[1]}"`) as string; } catch { return undefined; }
}

/** Only display cell identities actually reported by this call, never the current kernel's. */
export function activityPresentation(step: ChatTurnStep) {
  const python = /(?:^|\.)ipython$/i.test(step.tool || "");
  const args = record(step.argsPreview);
  const result = record(step.resultPreview);
  const code = python ? args?.code ?? args?.source : null;
  const number = (value: unknown) => typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : null;
  return {
    python,
    input: typeof code === "string" ? code : python ? codePrefix(step.argsPreview) ?? step.argsPreview : step.argsPreview,
    executionCount: python ? number(result?.execution_count) : null,
    generation: python ? number(result?.kernel_generation) : null,
  };
}
