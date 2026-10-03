/**
 * Focus rings belong to keyboard navigation. Chromium's :focus-visible also
 * turns on when any key is pressed while a clicked button still has focus
 * (Shift, Win, the screenshot shortcut), which paints a ring around the last
 * clicked control. Track the last real input instead: navigation keys switch
 * to "keyboard", pointer presses switch back, and CSS shows rings only for
 * keyboard input.
 */
const NAVIGATION_KEYS = new Set(["Tab", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown"]);

export function installInputModality(doc: Document = document): () => void {
  const root = doc.documentElement;
  root.dataset.input = "pointer";
  const key = (event: KeyboardEvent) => {
    if (NAVIGATION_KEYS.has(event.key) && !event.ctrlKey && !event.metaKey && !event.altKey) root.dataset.input = "keyboard";
  };
  const pointer = () => { root.dataset.input = "pointer"; };
  doc.addEventListener("keydown", key, true);
  doc.addEventListener("pointerdown", pointer, true);
  return () => {
    doc.removeEventListener("keydown", key, true);
    doc.removeEventListener("pointerdown", pointer, true);
  };
}
