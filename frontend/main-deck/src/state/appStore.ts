import DECK_ROUTES from "../../../../deck-routes.json";
import {closeCompactPane, hidePane, PANE, revealPane} from "../workbench/workbenchStore";
import {getDeckRuntime} from "../runtime/runtimeBridge";
import {createExternalStore} from "./createModuleStore";
import {focusNativeWindow, nativeUtilityKey} from "../workbench/nativeWindowStore";

export type PrimaryView =
  | "chat"
  | "memory"
  | "automations"
  | "overview"
  | "runtime"
  | "settings";

export type WorkspaceView = Exclude<PrimaryView, "settings">;

export type SettingsCategory =
  | "general"
  | "providers"
  | "provider-keys"
  | "custom-endpoints"
  | "local-models"
  | "tools-keys"
  | "search"
  | "browser"
  | "voice"
  | "messaging"
  | "plugins"
  | "memory"
  | "about";

export type AppState = Readonly<{
  view: PrimaryView;
  settingsReturnView: WorkspaceView;
  settingsCategory: SettingsCategory;
}>;

const initialView = new URLSearchParams(window.location.search).get("view");
export const PRIMARY_VIEWS = DECK_ROUTES.primary as PrimaryView[];
const validViews = new Set<PrimaryView>(PRIMARY_VIEWS);
export function isPrimaryView(value: string): value is PrimaryView {
  return validViews.has(value as PrimaryView);
}
const requestedInitialView = validViews.has(initialView as PrimaryView)
  ? initialView as PrimaryView
  : "chat";
const store = createExternalStore<AppState>({
  view: requestedInitialView === "memory" ? "settings" : requestedInitialView,
  settingsReturnView: "chat",
  settingsCategory: requestedInitialView === "memory" ? "memory" : "general",
});

export function navigateTo(view: PrimaryView): void {
  if (["runtime", "overview", "automations"].includes(view) && focusNativeWindow(nativeUtilityKey(view))) return;
  // Route the public Memory deep link to its current Settings page.
  if (view === "memory") {
    selectSettingsCategory("memory");
    navigateTo("settings");
    return;
  }
  const state = store.getState();
  if (state.view === view) return;
  closeCompactPane();
  if (view === "settings") {
    store.setState({
      view,
      settingsReturnView: "chat",
    });
    getDeckRuntime()?.setView(view);
    return;
  }
  store.setState({
    view,
    settingsReturnView: "chat",
  });
  getDeckRuntime()?.setView(view);
}

export function closeSettings(): void {
  const state = store.getState();
  if (state.view !== "settings") return;
  navigateTo(state.settingsReturnView);
}

export function selectSettingsCategory(category: SettingsCategory): void {
  const state = store.getState();
  if (state.settingsCategory === category) return;
  store.setState({settingsCategory: category});
  getDeckRuntime()?.setSettingsCategory(category);
}

export function collapseHistory(): void {
  hidePane(PANE.history);
}

export function revealHistory(): void {
  revealPane(PANE.history, "left");
}

export function closeMobileHistory(): void {
  closeCompactPane(PANE.history);
}

export function getAppState(): AppState {
  return store.getState();
}

export function useAppState(): AppState {
  return store.useStore();
}
