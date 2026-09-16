import {createContext, useContext} from "react";

/** React state belongs to the Deck; DOM interaction belongs to the visible window. */
export const SurfaceDocumentContext = createContext<Document | null>(null);
export function useSurfaceDocument(): Document {
  return useContext(SurfaceDocumentContext) || document;
}

export function focusMainComposer() {
  void window.variant1Deck?.controlNativeWindow?.("deck", "focus");
  window.focus();
  document.getElementById("composer-input")?.focus();
}
