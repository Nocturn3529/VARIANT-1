/** Pure presentation policy for the per-chat mutation-authority switch. */
import type {
  ChatRuntimeState,
  MutationTogglePending,
} from "./types";
import {isActionSurface} from "./runtimeProfile";

export type MutationToggleControlState = {
  visible: boolean;
  checked: boolean;
  disabled: boolean;
  title: string;
  valueText: string;
};

export function mutationToggleControlState(input: {
  runtime: ChatRuntimeState | null;
  pending: MutationTogglePending | null;
  connected: boolean;
  turnActive: boolean;
  sessionId: string | null;
}): MutationToggleControlState {
  const {runtime, pending, connected, turnActive, sessionId} = input;
  const checked = Boolean(runtime?.mutationEnabled);
  const visible = isActionSurface(runtime?.actionSurface);
  const statusReason = runtime?.mutationToggleReason || (
    checked
      ? "Mutation authoring is on for this chat"
      : "Mutation authoring is off; activated session tools remain available"
  );

  let title = statusReason;
  if (pending) {
    title = "Changing mutation authority…";
  } else if (!connected) {
    title = "Reconnect to VARIANT-1 before changing mutation authority";
  } else if (!sessionId) {
    title = "Open a chat session before changing mutation authority";
  } else if (turnActive) {
    title = "Finish or stop the active turn before changing mutation authority";
  } else if (!runtime?.mutationToggleAvailable) {
    title = statusReason || "Mutation authority is unavailable for this chat";
  } else if (checked && !runtime.mutationEffectiveEnabled) {
    title = "Mutation authoring is on but temporarily frozen by the operator";
  }

  return {
    visible,
    checked,
    disabled: Boolean(
      pending
      || !connected
      || !sessionId
      || turnActive
      || !runtime?.mutationToggleAvailable
    ),
    title,
    valueText: pending
      ? (pending.enabled ? "Turning on…" : "Turning off…")
      : (checked ? "On" : "Off"),
  };
}
