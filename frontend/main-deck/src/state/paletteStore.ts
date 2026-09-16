import {createExternalStore} from "./createModuleStore";
const store = createExternalStore<{open: boolean; mode: "all" | "windows"}>({open: false, mode: "all"});
export const usePalette = store.useStore;
export function openPalette(mode: "all" | "windows" = "all"): void {
  void window.variant1Deck?.controlNativeWindow?.("deck", "focus");
  store.replaceState({open: true, mode});
}
export function closePalette(): void { store.setState({open: false}); }
