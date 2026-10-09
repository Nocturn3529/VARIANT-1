/**
 * The llama.cpp engine is downloaded in-app, never shipped with the installer.
 * Chat surfaces use this to prompt for it when a local model is chosen.
 */
import {navigateTo, selectSettingsCategory} from "./state/appStore";
import {usePlatformState} from "./store";
import type {InferencePlatform, InstallJob} from "./types";

export type LocalEngine = Readonly<{
  /** The platform supports local models but no engine is installed yet. */
  missing: boolean;
  /** Install progress while an engine download runs. */
  installing: number | null;
}>;

export function activeEngineJob(jobs: readonly InstallJob[] | undefined): InstallJob | undefined {
  return (jobs || []).find(item => item.runtime_id === "llamacpp" && !["done", "error", "cancelled"].includes(item.status));
}

export function useLocalEngine(): LocalEngine {
  const {config} = usePlatformState();
  const platform = (config.inference_platform || {}) as Partial<InferencePlatform>;
  const status = platform.local_runtime;
  const job = activeEngineJob(platform.install_jobs);
  return {
    missing: !!status && status.supported !== false && status.installed === false,
    installing: job ? job.progress || 0 : null,
  };
}

export function openLocalEngine(): void {
  selectSettingsCategory("local-models");
  navigateTo("settings");
}
