import {createExternalStore} from "../state/createModuleStore";

/** The Overview's selected tab, shared so other surfaces can open a tab. */
export type OverviewTab = "summary" | "inference" | "system" | "cloud" | "models";

const store = createExternalStore<{tab: OverviewTab}>({tab: "summary"});

export function selectOverviewTab(tab: OverviewTab): void {
  if (store.getState().tab !== tab) store.setState({tab});
}

export function useOverviewTab(): OverviewTab {
  return store.useStore().tab;
}
