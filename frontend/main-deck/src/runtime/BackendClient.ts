import {detachedChatId} from "./viewIdentity";
import type { WsCommand, WsMessage } from "../protocol";
import { parseWsEnvelope } from "../protocol";

export type BackendConnectionState = "connecting" | "connected" | "offline";

export type BackendInfo = {
  port?: number;
  token?: string;
};

export type BackendStatus = {
  status?: string;
};

export type BackendBridgeApi = {
  getBackendInfo?: () => Promise<BackendInfo | null | undefined>;
  onBackendStatus?: (listener: (status: BackendStatus) => void) => unknown;
  log?: (message: string) => void;
};

export type SocketEventMap = {
  open: Event;
  message: MessageEvent;
  close: CloseEvent;
  error: Event;
};

export interface BackendSocket {
  readonly readyState: number;
  send(data: string): void;
  close(): void;
  addEventListener<K extends keyof SocketEventMap>(
    type: K,
    listener: (event: SocketEventMap[K]) => void,
  ): void;
}

export type BackendClientScheduler = {
  setTimeout(callback: () => void, delay: number): unknown;
  clearTimeout(handle: unknown): void;
};

export type BackendClientOptions = {
  api: BackendBridgeApi | null;
  createSocket?: (url: string) => BackendSocket;
  scheduler?: BackendClientScheduler;
  connectTimeoutMs?: number;
  reconnectDelay?: (attempt: number) => number;
};

export type BackendClientDiagnostics = Readonly<{
  state: BackendConnectionState;
  generation: number;
  createdSocketCount: number;
  liveSocketCount: number;
  maxConcurrentSocketCount: number;
  reconnectAttempt: number;
  started: boolean;
}>;

type Listener<T> = (value: T) => void;
type VoidListener = () => void;

const defaultScheduler: BackendClientScheduler = {
  setTimeout: (callback, delay) => window.setTimeout(callback, delay),
  clearTimeout: handle => window.clearTimeout(handle as number),
};

function defaultSocketFactory(url: string): BackendSocket {
  return new WebSocket(url);
}

function defaultReconnectDelay(attempt: number): number {
  return Math.min(12_000, 600 + attempt * 500);
}

/**
 * Framework-neutral owner of the Main Deck's single backend WebSocket.
 *
 * The class deliberately has no React dependency. React owns one long-lived
 * instance through DeckRuntime; typed stores subscribe through filtered runtime
 * modules.
 */
export class BackendClient {
  private readonly api: BackendBridgeApi | null;
  private readonly createSocket: (url: string) => BackendSocket;
  private readonly scheduler: BackendClientScheduler;
  private readonly connectTimeoutMs: number;
  private readonly reconnectDelay: (attempt: number) => number;

  private socket: BackendSocket | null = null;
  private reconnectTimer: unknown = null;
  private connecting = false;
  private started = false;
  private generation = 0;
  private connectAttempt = 0;
  private state: BackendConnectionState = "offline";
  private createdSocketCount = 0;
  private liveSocketCount = 0;
  private maxConcurrentSocketCount = 0;

  private readonly messageListeners = new Set<Listener<WsMessage>>();
  private readonly stateListeners = new Set<Listener<BackendConnectionState>>();
  private readonly openListeners = new Set<VoidListener>();
  private readonly closeListeners = new Set<VoidListener>();

  constructor(options: BackendClientOptions) {
    this.api = options.api;
    this.createSocket = options.createSocket ?? defaultSocketFactory;
    this.scheduler = options.scheduler ?? defaultScheduler;
    this.connectTimeoutMs = options.connectTimeoutMs ?? 8_000;
    this.reconnectDelay = options.reconnectDelay ?? defaultReconnectDelay;
  }

  start(): void {
    if (this.started) return;
    this.started = true;
    this.connect();
    this.api?.onBackendStatus?.(status => {
      if (
        this.started
        && status?.status === "ready"
        && !this.isOpen()
        && !this.connecting
      ) {
        this.connectAttempt = 0;
        this.connect();
      }
    });
  }

  stop(): void {
    if (!this.started && !this.socket && !this.connecting) return;
    this.started = false;
    this.cancelReconnect();
    this.generation += 1;
    this.connecting = false;
    const socket = this.socket;
    this.socket = null;
    this.liveSocketCount = 0;
    if (socket) {
      try {
        socket.close();
      } catch {
        // Closing is best-effort during renderer teardown.
      }
    }
    this.setState("offline");
  }

  connect(): void {
    void this.connectInternal();
  }

  isOpen(): boolean {
    return !!this.socket && this.socket.readyState === 1;
  }

  send(payload: WsCommand): boolean {
    if (!this.isOpen() || !this.socket) return false;
    try {
      this.socket.send(JSON.stringify(payload));
      return true;
    } catch {
      return false;
    }
  }

  subscribeMessage(listener: Listener<WsMessage>): () => void {
    this.messageListeners.add(listener);
    return () => this.messageListeners.delete(listener);
  }

  subscribeState(listener: Listener<BackendConnectionState>): () => void {
    this.stateListeners.add(listener);
    return () => this.stateListeners.delete(listener);
  }

  subscribeOpen(listener: VoidListener): () => void {
    this.openListeners.add(listener);
    return () => this.openListeners.delete(listener);
  }

  subscribeClose(listener: VoidListener): () => void {
    this.closeListeners.add(listener);
    return () => this.closeListeners.delete(listener);
  }

  getDiagnostics(): BackendClientDiagnostics {
    return {
      state: this.state,
      generation: this.generation,
      createdSocketCount: this.createdSocketCount,
      liveSocketCount: this.liveSocketCount,
      maxConcurrentSocketCount: this.maxConcurrentSocketCount,
      reconnectAttempt: this.connectAttempt,
      started: this.started,
    };
  }

  private async connectInternal(): Promise<void> {
    if (!this.started || !this.api || this.isOpen() || this.connecting) return;
    if (this.socket?.readyState === 0) return;

    this.connecting = true;
    const myGeneration = ++this.generation;
    this.setState("connecting");

    let info: BackendInfo | null | undefined;
    try {
      info = await this.api.getBackendInfo?.();
    } catch {
      info = null;
    }

    if (!this.started || myGeneration !== this.generation) {
      this.connecting = false;
      return;
    }

    if (!info?.port || !info.token) {
      this.connecting = false;
      this.setState("offline");
      this.log(`ws offline no-backend-info attempt=${this.connectAttempt}`);
      this.scheduleReconnect();
      return;
    }

    let next: BackendSocket;
    try {
      this.log(
        `ws connecting port=${info.port} attempt=${this.connectAttempt + 1}`,
      );
      next = this.createSocket(
        `ws://127.0.0.1:${info.port}/ws?token=${encodeURIComponent(info.token)}${detachedChatId() ? `&view_role=detached_chat&view_chat_id=${encodeURIComponent(detachedChatId())}` : ""}`,
      );
    } catch (error) {
      this.connecting = false;
      this.setState("offline");
      this.log(`ws construct failed: ${this.errorText(error)}`);
      this.scheduleReconnect();
      return;
    }

    const previous = this.socket;
    this.socket = next;
    this.createdSocketCount += 1;
    this.liveSocketCount = 1;
    this.maxConcurrentSocketCount = Math.max(
      this.maxConcurrentSocketCount,
      this.liveSocketCount,
    );
    if (previous && previous !== next) {
      try {
        previous.close();
      } catch {
        // The replacement socket is already authoritative.
      }
    }

    let opened = false;

    next.addEventListener("open", () => {
      if (
        !this.started
        || this.socket !== next
        || myGeneration !== this.generation
      ) {
        try {
          next.close();
        } catch {
          // Superseded sockets have no remaining responsibility.
        }
        return;
      }
      opened = true;
      this.connecting = false;
      this.connectAttempt = 0;
      this.setState("connected");
      this.log(`ws connected port=${info?.port}`);
      this.emitSafely(this.openListeners, undefined, "open");
    });

    next.addEventListener("message", event => {
      if (this.socket !== next || myGeneration !== this.generation) return;
      let message: WsMessage;
      try {
        const parsed: unknown = JSON.parse(String(event.data));
        const envelope = parseWsEnvelope(parsed);
        if (!envelope) return;
        message = envelope as WsMessage;
      } catch {
        this.log("ws ignored malformed JSON message");
        return;
      }
      this.emitSafely(this.messageListeners, message, "message");
    });

    next.addEventListener("close", event => {
      if (this.socket !== next || myGeneration !== this.generation) return;
      this.socket = null;
      this.liveSocketCount = 0;
      this.connecting = false;
      if (opened) {
        this.log(
          `ws disconnected code=${event?.code ?? "?"} wasClean=${!!event?.wasClean}`,
        );
        this.emitSafely(this.closeListeners, undefined, "close");
      } else {
        this.log(`ws closed before open code=${event?.code ?? "?"}`);
      }
      this.setState("offline");
      this.scheduleReconnect();
    });

    next.addEventListener("error", () => {
      this.log("ws error (see close for code)");
    });

    this.scheduler.setTimeout(() => {
      if (
        !this.started
        || myGeneration !== this.generation
        || this.socket !== next
      ) {
        return;
      }
      if (next.readyState === 0) {
        try {
          next.close();
        } catch {
          // Its close event or the next backend-ready signal will reconnect.
        }
      }
    }, this.connectTimeoutMs);
  }

  private scheduleReconnect(): void {
    if (!this.started) return;
    this.cancelReconnect();
    this.connectAttempt += 1;
    this.reconnectTimer = this.scheduler.setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, this.reconnectDelay(this.connectAttempt));
  }

  private cancelReconnect(): void {
    if (this.reconnectTimer == null) return;
    this.scheduler.clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
  }

  private setState(next: BackendConnectionState): void {
    if (this.state === next) return;
    this.state = next;
    this.emitSafely(this.stateListeners, next, "state");
  }

  private emitSafely<T>(
    listeners: Set<Listener<T>> | Set<VoidListener>,
    value: T,
    label: string,
  ): void {
    listeners.forEach(listener => {
      try {
        (listener as Listener<T>)(value);
      } catch (error) {
        this.log(`${label} listener failed: ${this.errorText(error)}`);
      }
    });
  }

  private log(message: string): void {
    try {
      this.api?.log?.(`[deck] ${message}`);
    } catch {
      // Logging must never interrupt transport state.
    }
  }

  private errorText(error: unknown): string {
    return error instanceof Error ? error.message : String(error);
  }
}
