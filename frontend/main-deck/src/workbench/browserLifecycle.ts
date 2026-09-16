/** A browser request has one deadline and never replays an uncertain effect. */
export class BrowserCommandError extends Error {
  constructor(public code: string, message: string, public phase = "prepare") { super(message); }
}

export function abortError(signal?: AbortSignal): Error {
  return signal?.reason instanceof Error ? signal.reason : new BrowserCommandError("HOST_DISCONNECTED", "Browser host disconnected; request was cancelled");
}

export function checkBrowserRequest(signal?: AbortSignal): void {
  if (signal?.aborted) throw abortError(signal);
}

export function browserDeadline<T>(work: Promise<T>, ms: number, phase: string, signal?: AbortSignal): Promise<T> {
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (value: T | unknown, failed: boolean) => {
      if (settled) return;
      settled = true; clearTimeout(timer); signal?.removeEventListener("abort", aborted);
      if (failed) reject(value); else resolve(value as T);
    };
    const aborted = () => finish(abortError(signal), true);
    const timer = setTimeout(() => finish(new BrowserCommandError("BROWSER_TIMEOUT", `Browser ${phase} timed out; inspect the page before another action`, phase), true), ms);
    signal?.addEventListener("abort", aborted, {once: true});
    work.then(value => finish(value, false), error => finish(error, true));
    if (signal?.aborted) aborted();
  });
}
