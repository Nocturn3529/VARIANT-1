import {BrowserCommandError} from "./browserLifecycle";

type RunScript = (script: string, gesture: boolean, timeoutMs: number, signal: AbortSignal) => Promise<unknown>;

export async function waitForBrowserElement(
  command: Record<string, unknown>, guest: EventTarget, validate: () => void,
  execute: RunScript, signal?: AbortSignal,
) {
  const {selector, condition, state = "visible", timeout_ms = 5000} = command;
  if (typeof selector !== "string" || !selector.trim()) {
    throw new BrowserCommandError("INVALID_WAIT_SELECTOR", "Enter a CSS selector", "wait");
  }
  if (condition !== "element" || !["visible", "hidden", "attached", "detached"].includes(String(state))
      || typeof timeout_ms !== "number" || !Number.isInteger(timeout_ms) || timeout_ms < 0 || timeout_ms > 20000) {
    throw new BrowserCommandError("INVALID_WAIT_ARGUMENT", "Invalid element wait state or timeout", "wait");
  }
  // A navigation can destroy the old execution context before Electron resolves
  // its promise. Release the request on the existing document/guest fence.
  const lifetime = new AbortController();
  const changed = () => { try { validate(); } catch (error) { lifetime.abort(error); } };
  const events = ["did-start-navigation", "destroyed", "render-process-gone"];
  for (const event of events) guest.addEventListener(event, changed);
  const request = signal ? AbortSignal.any([lifetime.signal, signal]) : lifetime.signal;
  try {
    const result = await execute(`new Promise(resolve => {
      const selector = ${JSON.stringify(selector)}, state = ${JSON.stringify(state)};
      let observer, tick, deadline;
      const finish = value => {
        observer?.disconnect();
        clearInterval(tick); clearTimeout(deadline);
        window.removeEventListener('pagehide', leave);
        resolve(value);
      };
      const leave = () => finish('DOCUMENT_CHANGED');
      const check = () => {
        let element;
        try { element = document.querySelector(selector); }
        catch { finish('INVALID_WAIT_SELECTOR'); return true; }
        let matches;
        if (state === 'attached' || state === 'detached') matches = !!element === (state === 'attached');
        else {
          const rect = element?.getBoundingClientRect();
          const visible = !!(rect?.width && rect.height && getComputedStyle(element).visibility === 'visible');
          matches = visible === (state === 'visible');
        }
        if (matches) finish('matched');
        return matches;
      };
      if (check()) return;
      observer = new MutationObserver(check);
      observer.observe(document, {childList:true,subtree:true,attributes:true});
      // Layout and CSS transitions can change visibility without a DOM mutation.
      if (state === 'visible' || state === 'hidden') tick = setInterval(check, 100);
      deadline = setTimeout(() => { if (!check()) finish('WAIT_TIMEOUT'); }, ${timeout_ms});
      window.addEventListener('pagehide', leave, {once:true});
    })`, false, timeout_ms + 1000, request);
    if (result === "matched") return {ok: true, value: {condition, state}};
    if (result === "INVALID_WAIT_SELECTOR") throw new BrowserCommandError(result, "Invalid CSS selector", "wait");
    if (result === "DOCUMENT_CHANGED") throw new BrowserCommandError(result, "The document changed during the wait", "document");
    if (result === "WAIT_TIMEOUT") throw new BrowserCommandError(result, "Element wait timed out", "wait");
    throw new BrowserCommandError("BROWSER_COMMAND_FAILED", "The page returned an invalid wait result", "wait");
  } finally {
    for (const event of events) guest.removeEventListener(event, changed);
  }
}
