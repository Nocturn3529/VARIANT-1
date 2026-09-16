import type { WsCommand, WsMessage } from "../protocol";
import type {RuntimeApi, RuntimeContext} from "../types";
import {relativeTimeLabel} from "../state/storePrimitives";
import {
  BackendClient,
  type BackendBridgeApi,
  type BackendClientDiagnostics,
  type BackendClientOptions,
  type BackendConnectionState,
} from "./BackendClient";
import {requestInitialHydration} from "./initialHydration";

type StateListener = (state: BackendConnectionState) => void;
type VoidListener = () => void;

export type DeckRuntimeOptions = {
  api: BackendBridgeApi | null;
  clientOptions?: Omit<BackendClientOptions, "api">;
  notify?: (message: string, surface?: string) => void;
  navigate?: (view: string) => void;
  initialView?: string;
  initialSettingsCategory?: string;
};

export type DeckRuntimeModule = {
  id: string;
  messageTypes: readonly string[];
  start?: (context: RuntimeContext) => void;
  enter?: (context: RuntimeContext, view: string) => void;
  settingsCategory?: (context: RuntimeContext, category: string) => void;
  /**
   * Optional per-module shaper (e.g. chat family discriminated union).
   * Runs once at the runtime edge before `handle`. Return value is passed
   * through as the handle message (cast to WsMessage for non-chat modules).
   */
  parse?: (message: WsMessage) => WsMessage;
  handle?: (context: RuntimeContext, message: WsMessage) => void;
  connection?: (context: RuntimeContext, status: BackendConnectionState) => void;
  subtitle?: (
    context: RuntimeContext,
    text: string,
    state: "ready" | "working" | "idle" | "offline",
  ) => void;
};

/**
 * Long-lived Main Deck runtime facade.
 *
 * It owns initial hydration and exposes the narrow interface used by React
 * stores.
 */
export class DeckRuntime {
  private readonly client: BackendClient;
  private readonly api: RuntimeApi | null;
  private readonly notify: (message: string, surface?: string) => void;
  private readonly navigate: (view: string) => void;
  private hydratedGeneration = -1;
  private currentState: BackendConnectionState = "offline";
  private currentView: string;
  private currentSettingsCategory: string;
  private started = false;
  private readonly modules: DeckRuntimeModule[] = [];
  private readonly stateListeners = new Set<StateListener>();
  private readonly closeListeners = new Set<VoidListener>();

  constructor(options: DeckRuntimeOptions) {
    this.api = options.api as RuntimeApi | null;
    this.notify = options.notify ?? (() => undefined);
    this.navigate = options.navigate ?? (() => undefined);
    this.currentView = options.initialView ?? "chat";
    this.currentSettingsCategory = options.initialSettingsCategory ?? "general";
    this.client = new BackendClient({
      api: options.api,
      ...options.clientOptions,
    });
    this.client.subscribeMessage(message => {
      this.dispatchMessage(message);
    });
    this.client.subscribeState(state => {
      this.currentState = state;
      if (typeof document !== "undefined") {
        document.body.dataset.backendState = state;
        const diagnostics = this.client.getDiagnostics();
        document.body.dataset.deckLiveSockets = String(
          diagnostics.liveSocketCount,
        );
        document.body.dataset.deckMaxConcurrentSockets = String(
          diagnostics.maxConcurrentSocketCount,
        );
      }
      this.modules.forEach(module => this.callModule(module, "connection", state));
      this.emit(this.stateListeners, state);
    });
    this.client.subscribeOpen(() => {
      const generation = this.client.getDiagnostics().generation;
      if (this.hydratedGeneration !== generation) {
        this.hydratedGeneration = generation;
        requestInitialHydration(command => this.client.send(command));
      }
      if (this.currentView === "settings") this.refreshActiveSettings();
    });
    this.client.subscribeClose(() => {
      this.emit(this.closeListeners, undefined);
    });
  }

  start(): void {
    if (this.started) return;
    this.started = true;
    this.modules.forEach(module => {
      this.callModule(module, "start");
      this.callModule(module, "connection", this.currentState);
      this.callModule(module, "enter", this.currentView);
    });
    if (this.currentView === "settings") this.refreshActiveSettings();
    this.client.start();
  }

  stop(): void {
    this.started = false;
    this.client.stop();
  }

  send(command: WsCommand): boolean {
    return this.client.send(command);
  }

  isOpen(): boolean {
    return this.client.isOpen();
  }

  getConnectionState(): BackendConnectionState {
    return this.currentState;
  }

  getDiagnostics(): BackendClientDiagnostics {
    return this.client.getDiagnostics();
  }

  /**
   * Development-only fixture ingress for ?fixture=1. Production messages
   * always arrive through BackendClient.
   */
  ingestDevelopmentMessage(message: WsMessage): void {
    this.dispatchMessage(message);
  }

  setDevelopmentConnectionState(state: BackendConnectionState): void {
    this.currentState = state;
    if (typeof document !== "undefined") {
      document.body.dataset.backendState = state;
    }
    this.modules.forEach(module => this.callModule(module, "connection", state));
    if (state === "connected" && this.currentView === "settings") {
      this.refreshActiveSettings();
    }
    this.emit(this.stateListeners, state);
  }

  register(module: DeckRuntimeModule): void {
    if (!module?.id || this.modules.some(item => item.id === module.id)) return;
    this.modules.push(module);
    if (!this.started) return;
    this.callModule(module, "start");
    this.callModule(module, "connection", this.currentState);
    this.callModule(module, "enter", this.currentView);
    if (this.currentView === "settings") this.refreshActiveSettings(module);
  }

  setView(view: string): void {
    const next = String(view || "chat");
    if (next === this.currentView) return;
    this.currentView = next;
    this.modules.forEach(module => this.callModule(module, "enter", next));
    if (next === "settings") this.refreshActiveSettings();
  }

  setSettingsCategory(category: string): void {
    const next = String(category || "general");
    if (next === this.currentSettingsCategory) return;
    this.currentSettingsCategory = next;
    if (this.currentView === "settings") this.refreshActiveSettings();
  }

  private refreshActiveSettings(module?: DeckRuntimeModule): void {
    const modules = module ? [module] : this.modules;
    modules.forEach(item => this.callModule(
      item,
      "settingsCategory",
      this.currentSettingsCategory,
    ));
  }

  subscribeState(listener: StateListener): () => void {
    this.stateListeners.add(listener);
    return () => this.stateListeners.delete(listener);
  }

  subscribeClose(listener: VoidListener): () => void {
    this.closeListeners.add(listener);
    return () => this.closeListeners.delete(listener);
  }

  private moduleContext(): RuntimeContext {
    const runtime = this;
    return {
      api: this.api,
      send: payload => this.send(payload),
      notify: this.notify,
      isOpen: () => this.isOpen(),
      get view() {
        return runtime.currentView;
      },
      get settingsCategory() {
        return runtime.currentSettingsCategory;
      },
      relativeTime: value => this.relativeTime(value),
      formatTime: value => this.formatTime(value),
      navigate: this.navigate,
    };
  }

  private dispatchMessage(message: WsMessage): void {
    const type = String(message.type || "");
    this.modules.forEach(module => {
      if (!module.handle) return;
      if (!module.messageTypes.includes(type)) return;
      // Shape high-churn families at the runtime edge (chat parse, etc.).
      let shaped = message;
      try {
        if (module.parse) shaped = module.parse(message);
      } catch (error) {
        console.error(`[Main Deck] ${module.id} parse failed`, error);
        return;
      }
      this.callModule(module, "handle", shaped);
    });
  }

  private callModule<K extends keyof DeckRuntimeModule>(
    module: DeckRuntimeModule,
    method: K,
    ...args: unknown[]
  ): void {
    const callback = module[method];
    if (typeof callback !== "function") return;
    try {
      (callback as (...items: unknown[]) => void)(this.moduleContext(), ...args);
    } catch (error) {
      console.error(
        `[Main Deck] ${module.id} ${String(method)} failed`,
        error,
      );
    }
  }

  private relativeTime(value: number | string | undefined | null): string {
    return relativeTimeLabel(value);
  }

  private formatTime(value: number | string | undefined | null): string {
    const numeric = Number(value);
    const milliseconds = Number.isFinite(numeric)
      ? (numeric > 1e12 ? numeric : numeric * 1000)
      : Date.now();
    return new Date(milliseconds).toLocaleTimeString(
      [],
      {hour: "2-digit", minute: "2-digit"},
    );
  }

  private emit<T>(
    listeners: Set<((value: T) => void)> | Set<VoidListener>,
    value: T,
  ): void {
    listeners.forEach(listener => {
      try {
        (listener as (item: T) => void)(value);
      } catch (error) {
        console.error("[Main Deck] runtime subscriber failed", error);
      }
    });
  }
}
