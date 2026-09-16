import {getChatState, stopSpeech} from "../chatStore";
import {submitUserInput} from "../chat/composer";
import {retainChatDraft} from "../chat/stateCore";
import {MicController, type MicPhase} from "../runtime/MicController";
import type {RuntimeContext} from "../types";
import {createModuleStore} from "./createModuleStore";
import {OFFLINE_HOLD_MS} from "../connectionUi";
import type {WsCommand} from "../protocol";

export type MicState = Readonly<{
  phase: MicPhase;
  error: string;
  sessionId: string | null;
  sessionTitle: string;
  deliveryMode: "steer" | "follow_up";
}>;

const store = createModuleStore<MicState>({
  initialState: {phase: "idle", error: "", sessionId: null, sessionTitle: "", deliveryMode:"steer"},
});
let controller: MicController | null = null;
let offlineTimer: ReturnType<typeof setTimeout> | null = null;
let unsentTranscript: WsCommand | null = null;
function clearOfflineTimer() { if (offlineTimer) clearTimeout(offlineTimer); offlineTimer = null; }
function holdOffline() {
  if (offlineTimer) return;
  offlineTimer = setTimeout(() => {
    offlineTimer = null;
    unsentTranscript = null;
    const active = controller && !["idle", "error"].includes(controller.getPhase());
    controller?.dispose();
    if (active) store.getContext()?.notify("Voice capture stopped because the backend disconnected.");
  }, OFFLINE_HOLD_MS);
}

function ensureController(): MicController {
  if (controller) return controller;
  controller = new MicController({
    isOpen: () => !!store.getContext()?.isOpen?.(),
    send: command => {
      if (store.send(command)) return true;
      if (command.type !== "voice:transcribe") return false;
      // Only retry a clip the transport explicitly did not accept.
      unsentTranscript = command; holdOffline(); return true;
    },
    notify: message => store.getContext()?.notify(message),
    getSessionId: () => String(getChatState().sessionId || ""),
    stopSpeech,
    onPhase: (phase, error = "") => {
      const owner = phase === "requesting" ? getChatState() : store.getState();
      store.replaceState({phase, error,
        deliveryMode:owner.deliveryMode,
        sessionId: phase === "idle" ? null : owner.sessionId,
        sessionTitle: phase === "idle" ? "" : "title" in owner ? owner.title : owner.sessionTitle});
    },
  });
  return controller;
}

export function setMicContext(next: RuntimeContext): void {
  store.setContext(next);
  ensureController();
}

export function setMicConnection(status: string): void {
  if (status === "connected") {
    clearOfflineTimer();
    if (unsentTranscript) {
      if (controller?.getPhase() !== "transcribing") unsentTranscript = null;
      else if (store.send(unsentTranscript)) unsentTranscript = null;
      else holdOffline();
    }
    return;
  }
  holdOffline();
}

export function toggleMic(): void {
  ensureController().toggle();
}

export function cancelMic(): void {
  unsentTranscript = null;
  controller?.stop(false);
}

export function ingestMic(message: Record<string, unknown>): void {
  if (String(message.type || "") !== "transcript") return;
  const requestId = String(message.request_id || "");
  const sessionId = String(message.session_id || "");
  const owner=store.getState();
  if (!controller?.completeTranscription(requestId, sessionId)) return;
  if (message.cancelled) return;
  const text = String(message.text || "").trim();
  if (!text) {
    if (message.error) {
      const detail = String(message.error).replace(/\s+/g, " ").trim();
      const short = detail.length > 120 ? `${detail.slice(0, 117)}…` : detail;
      store.getContext()?.notify(
        short ? `Couldn't transcribe: ${short}` : "I couldn't transcribe that",
      );
    } else {
      store.getContext()?.notify("I didn't catch that");
    }
    return;
  }
  if (sessionId && sessionId !== getChatState().sessionId) {
    retainChatDraft(sessionId,text,owner.sessionTitle);
    store.getContext()?.notify(`Voice text was saved to the draft in ${owner.sessionTitle || "its original chat"}.`);
    return;
  }
  if(!submitUserInput({source: "voice", text, sessionId},owner.deliveryMode)) {
    retainChatDraft(sessionId,text,owner.sessionTitle);
    store.getContext()?.notify("Voice text was added to your draft. Review it before sending.");
  }
}

export function useMicState(): MicState {
  return store.useStore();
}
export const getMicState = store.getState;

export function disposeMic(): void {
  clearOfflineTimer(); unsentTranscript = null;
  controller?.dispose();
}
