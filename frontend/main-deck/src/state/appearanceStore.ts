import {useEffect, useState} from "react";
import {createExternalStore} from "./createModuleStore";
import {useNativeWindows} from "../workbench/nativeWindowStore";

export type Appearance = {density: "comfortable" | "compact"; motion: "system" | "reduced"};
const key = "variant1.appearance.v1";
function read(): Appearance {
  try {
    const value = JSON.parse(window.localStorage?.getItem(key) || "{}");
    return {density: value?.density === "compact" ? "compact" : "comfortable", motion: value?.motion === "reduced" ? "reduced" : "system"};
  } catch { return {density: "comfortable", motion: "system"}; }
}
const store = createExternalStore<Appearance>(read());
export const useAppearance = store.useStore;
export const getAppearance = store.getState;
export function setAppearance(value: Partial<Appearance>): void {
  store.setState(value);
  try { window.localStorage?.setItem(key, JSON.stringify(store.getState())); } catch { /* applies for this app lifetime */ }
}
export function useReducedMotion(owner: Document = document): boolean {
  const preference = useAppearance();
  const [system, setSystem] = useState(false);
  useEffect(() => {
    const media = owner.defaultView?.matchMedia?.("(prefers-reduced-motion: reduce)");
    setSystem(!!media?.matches);
    const update = () => setSystem(!!media?.matches);
    media?.addEventListener?.("change", update);
    return () => media?.removeEventListener?.("change", update);
  }, [owner]);
  return preference.motion === "reduced" || system;
}
export function AppearanceBindings() {
  const preference = useAppearance();
  const windows = useNativeWindows();
  const reduced = useReducedMotion();
  useEffect(() => {
    const documents = [document, ...Object.values(windows).flatMap(record => record.document ? [record.document] : [])];
    for (const owner of documents) {
      owner.documentElement.dataset.density = preference.density;
      owner.documentElement.dataset.motion = reduced ? "reduced" : "full";
    }
  }, [preference, windows, reduced]);
  return null;
}
