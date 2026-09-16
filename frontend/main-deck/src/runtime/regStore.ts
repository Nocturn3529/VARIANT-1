/**
 * Compact Main Deck module registration for stores that only need:
 * setContext + connection + ingest (+ optional enter/settings refresh).
 */
import type { DeckRuntimeModule } from "./DeckRuntime";
import type { RuntimeContext } from "../types";
import type { WsMessage } from "../protocol";
import type {BackendConnectionState} from "./BackendClient";

export type StoreRegHooks = {
  setContext: (ctx: RuntimeContext) => void;
  setConnection: (status: BackendConnectionState) => void;
  ingest: (message: Record<string, unknown>) => void;
  /** Destination view id that should trigger refresh on enter (e.g. "memory"). */
  enterView?: string;
  /** Settings categories that should trigger refresh (e.g. "skills"). */
  settingsCategory?: string | readonly string[];
  onEnter?: () => void;
  onSettings?: () => void;
  onStart?: () => void;
  parse?: (message: WsMessage) => WsMessage;
  subtitle?: (
    ctx: RuntimeContext,
    text: string,
    state: "ready" | "working" | "idle" | "offline",
  ) => void;
  /** Extra connection / handle behavior beyond setContext + setConnection/ingest. */
  onConnection?: (ctx: RuntimeContext, status: BackendConnectionState) => void;
  onHandle?: (ctx: RuntimeContext, message: WsMessage) => void;
};

function online(ctx: RuntimeContext) {
  return !!ctx.isOpen?.();
}

/**
 * Build a DeckRuntimeModule that wires a typical store.
 * Callers still pass `id` and `messageTypes` when registering.
 */
export function storeModuleHooks(hooks: StoreRegHooks): Omit<DeckRuntimeModule, "id" | "messageTypes"> {
  const {
    setContext,
    setConnection,
    ingest,
    enterView,
    settingsCategory: settingsCat,
    onEnter,
    onSettings,
    onStart,
    parse,
    subtitle,
    onConnection,
    onHandle,
  } = hooks;
  const settingsCategories = typeof settingsCat === "string"
    ? [settingsCat]
    : settingsCat || [];

  return {
    start(ctx) {
      setContext(ctx);
      onStart?.();
    },
    enter(ctx, view) {
      setContext(ctx);
      if (enterView && view === enterView && online(ctx)) onEnter?.();
    },
    settingsCategory(ctx, category) {
      setContext(ctx);
      if (settingsCategories.includes(category) && online(ctx)) onSettings?.();
    },
    connection(ctx, status) {
      setContext(ctx);
      setConnection(status);
      onConnection?.(ctx, status);
    },
    handle(ctx, message) {
      setContext(ctx);
      if (onHandle) {
        onHandle(ctx, message);
        return;
      }
      ingest(message as Record<string, unknown>);
    },
    parse,
    subtitle,
  };
}
