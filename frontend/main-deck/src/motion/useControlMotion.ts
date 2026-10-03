import {useSyncExternalStore} from "react";
import {useReducedMotion} from "../state/appearanceStore";

/*
 * Decorative motion runs only while the window is visible and focused, and
 * never under reduced motion. One shared listener set serves every glyph, so
 * a roster of animated rows does not multiply window listeners.
 */
const listeners = new Set<() => void>();
let attentive = typeof document === "undefined" ? false : document.visibilityState !== "hidden" && document.hasFocus();

function update(next: boolean) {
  if (next === attentive) return;
  attentive = next;
  for (const listener of listeners) listener();
}
const onVisibility = () => update(document.visibilityState !== "hidden" && document.hasFocus());
// The Deck keeps background throttling off for browser/backend work, so
// native focus events also gate these purely decorative animations.
const onFocus = () => update(document.visibilityState !== "hidden");
const onBlur = () => update(false);

function subscribe(listener: () => void) {
  if (!listeners.size) {
    attentive = document.visibilityState !== "hidden" && document.hasFocus();
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("focus", onFocus);
    window.addEventListener("blur", onBlur);
  }
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
    if (listeners.size) return;
    document.removeEventListener("visibilitychange", onVisibility);
    window.removeEventListener("focus", onFocus);
    window.removeEventListener("blur", onBlur);
  };
}

/** True when decorative state motion may run in this window. */
export function useControlMotion(): boolean {
  const reduced = useReducedMotion();
  const visible = useSyncExternalStore(subscribe, () => attentive, () => false);
  return !reduced && visible;
}
