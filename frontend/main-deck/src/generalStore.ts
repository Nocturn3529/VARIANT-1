import type { WsCommand } from "./protocol";
import {createModuleStore} from "./state/createModuleStore";
import {playSpeechPlayback} from "./runtime/SpeechPlayback";
import type {
  CapabilitiesInfo,
  GeneralState,
  LocalModelInfo,
  RuntimeContext,
  SpeechProviderInfo,
  VoiceOption,
} from "./types";

const store = createModuleStore<GeneralState>({
  initialState: {
    connected: false,
    launchAtLogin: false,
    startHidden: false,
    mode: "local",
    model: "",
    inferenceRuntime: "llamacpp",
    capabilities: null,
    installedModels: [],
    modelsFolder: "models\\user",
    modelsScanning: false,
    modelsSwitching: false,
    sttProvider: "local",
    ttsProvider: "kokoro",
    sttProviders: [],
    ttsProviders: [],
    sttLocalAvailable: true,
    sttDropPath: "",
    ttsLocalAvailable: true,
    ttsDropPath: "",
    voiceEnabled: true,
    voiceAvailable: true,
    voiceSpeed: 1,
    voices: [],
    currentVoice: "",
    voicesLoaded: false,
    voicesError: "",
    speechReceipt: null,
  },
});

let modelScanGeneration = 0;

export function setGeneralContext(next: RuntimeContext) {
  store.setContext(next);
}

export function setGeneralConnection(status: string) {
  const open = status === "connected";
  if (store.getState().connected === open) return;
  store.setConnected(open);
}

export function sendGeneral(payload: WsCommand) {
  return store.send(payload);
}

export function notifyGeneral(message: string) {
  store.getContext()?.notify(message);
}

function applyConfig(
  message: Record<string, unknown>,
  {acceptModelFallback = true}: {acceptModelFallback?: boolean} = {},
) {
  const state = store.getState();
  const hasVoice = !!message.voice && typeof message.voice === "object";
  const voiceRoot = (hasVoice ? message.voice : {}) as Record<string, unknown>;
  const stt = (voiceRoot.stt || {}) as Record<string, unknown>;
  const tts = (voiceRoot.tts || {}) as Record<string, unknown>;
  const previousTts = state.ttsProvider;
  const nextTts = String(tts.provider || state.ttsProvider || "kokoro");
  const parseProviders = (value: unknown): SpeechProviderInfo[] => (
    Array.isArray(value) ? value.flatMap(item => {
      if (!item || typeof item !== "object") return [];
      const row = item as Record<string, unknown>;
      const id = String(row.id || "");
      if (!id) return [];
      return [{
        id,
        name: String(row.name || id),
        kind: row.kind === "local" ? "local" : "cloud",
        auth: (["none", "api_key", "shared"].includes(String(row.auth))
          ? String(row.auth) : "none") as SpeechProviderInfo["auth"],
        description: String(row.description || ""),
        signup_url: String(row.signup_url || ""),
        env_vars: Array.isArray(row.env_vars) ? row.env_vars.map(String) : [],
        default_model: String(row.default_model || ""),
        default_voice: String(row.default_voice || ""),
        mime_type: String(row.mime_type || ""),
        configured: !!row.configured,
        available: !!row.available,
        config: row.config && typeof row.config === "object"
          ? row.config as Record<string, unknown> : {},
        voices: Array.isArray(row.voices) ? row.voices.map(String) : [],
      } satisfies SpeechProviderInfo];
    }) : []
  );
  store.setState({
    connected: true,
    mode: String(message.mode || state.mode || "local"),
    // The authoritative local selection comes from the `models` snapshot,
    // where the server supplies an unambiguous full path. Engine/config model
    // names are display basenames and may only seed an otherwise empty state.
    model: acceptModelFallback && !state.model
      ? String(message.model || "")
      : state.model,
    inferenceRuntime: String(message.inference_runtime || state.inferenceRuntime || "llamacpp"),
    sttProvider: hasVoice ? String(stt.provider || state.sttProvider || "local") : state.sttProvider,
    ttsProvider: hasVoice ? nextTts : state.ttsProvider,
    sttProviders: hasVoice ? parseProviders(stt.providers) : state.sttProviders,
    ttsProviders: hasVoice ? parseProviders(tts.providers) : state.ttsProviders,
    sttLocalAvailable: hasVoice ? stt.local_available !== false : state.sttLocalAvailable,
    sttDropPath: hasVoice ? String(stt.drop_path || state.sttDropPath || "") : state.sttDropPath,
    ttsLocalAvailable: hasVoice ? tts.local_available !== false : state.ttsLocalAvailable,
    ttsDropPath: hasVoice ? String(tts.drop_path || state.ttsDropPath || "") : state.ttsDropPath,
    voiceEnabled: hasVoice ? tts.enabled !== false : state.voiceEnabled,
    voiceAvailable: hasVoice ? tts.available !== false : state.voiceAvailable,
    voiceSpeed: hasVoice && tts.speed != null ? Number(tts.speed) : state.voiceSpeed,
    currentVoice: hasVoice && tts.voice != null ? String(tts.voice) : state.currentVoice,
    voicesLoaded: hasVoice && previousTts && previousTts !== nextTts ? false : state.voicesLoaded,
  });
}

export function ingestGeneral(message: Record<string, unknown>) {
  const type = String(message.type || "");
  if (type === "config" || type === "engine" || type === "hello") {
    applyConfig(message, {acceptModelFallback: type !== "engine"});
    return;
  }
  if (type === "models") {
    modelScanGeneration += 1;
    const items = Array.isArray(message.items) ? message.items : [];
    store.setState({
      connected: true,
      installedModels: items.map(item => {
        const row = (item || {}) as Record<string, unknown>;
        return {
          name: String(row.name || ""),
          path: String(row.path || ""),
          vision: !!row.vision,
          mmproj: String(row.mmproj || ""),
          sizeBytes: Number(row.size_bytes || 0),
        } satisfies LocalModelInfo;
      }).filter(item => item.path || item.name),
      modelsFolder: String(
        message.folder || store.getState().modelsFolder || "models\\user"
      ),
      modelsScanning: false,
      model: String(message.current || ""),
      modelsSwitching: !!message.switching,
    });
    return;
  }
  if (type === "models:error") {
    if (message.operation === "switch") {
      store.setState({
        modelsSwitching: false,
        model: String(message.current || ""),
      });
      notifyGeneral(`Could not switch local model: ${String(message.error || "unknown error")}`);
      return;
    }
    modelScanGeneration += 1;
    store.setState({modelsScanning: false});
    notifyGeneral(`Could not scan models\\user: ${String(message.error || "unknown error")}`);
    return;
  }
  if (type === "capabilities") {
    store.setState({capabilities: message as CapabilitiesInfo});
    return;
  }
  if (type === "tts:voices") {
    const state = store.getState();
    const provider = String(message.provider || "");
    if (provider && provider !== state.ttsProvider) return;
    const items = Array.isArray(message.items) ? message.items : [];
    const voices: VoiceOption[] = items.map(item => {
      if (typeof item === "string") return {id: item, name: item, language: ""};
      const row = (item || {}) as Record<string, unknown>;
      return {
        id: String(row.id || row.voice_id || ""),
        name: String(row.name || row.id || row.voice_id || ""),
        language: String(row.language || ""),
      };
    }).filter(item => item.id);
    store.setState({
      voices,
      currentVoice: String(message.current || state.currentVoice || ""),
      voicesLoaded: true,
      voicesError: String(message.error || ""),
      voiceAvailable: state.voiceAvailable,
    });
    if (message.error) notifyGeneral(`Could not load ${message.provider || "speech"} voices: ${message.error}`);
    return;
  }
  if (type === "tts:preview") {
    // Chat reply Play uses purpose=chat (handled by chatStore). Settings
    // samples use purpose=preview or omit purpose.
    const purpose = String(message.purpose || "preview");
    if (purpose === "chat") return;
    if (message.error) notifyGeneral(`Voice preview failed: ${message.error}`);
    else if (message.audio) {
      const mime = String(message.mime_type || "audio/wav");
      playSpeechPlayback({
        owner: "settings",
        src: `data:${mime};base64,${message.audio}`,
        onError: () => notifyGeneral("Voice preview playback failed"),
      });
    }
    return;
  }
  if (type === "speech:accepted" || type === "speech:rejected") {
    store.setState({speechReceipt: {
      requestId: String(message.request_id || ""),
      accepted: type === "speech:accepted",
      error: String(message.error || ""),
    }});
  }
}

export async function loadLocalShellSettings() {
  try {
    const context = store.getContext();
    const [launchAtLogin, settings] = await Promise.all([
      context?.api?.getLaunchAtLogin?.(),
      context?.api?.getSettings?.(),
    ]);
    store.setState({
      launchAtLogin: !!launchAtLogin,
      startHidden: !!(settings && settings.general && settings.general.startHidden),
    });
  } catch {
    /* ignore */
  }
}

export function refreshGeneral() {
  sendGeneral({type: "config:get"});
  sendGeneral({type: "model:list"});
  sendGeneral({type: "tts:voices"});
  void loadLocalShellSettings();
}

export function isActiveModel(
  model: {name?: string; path?: string},
  currentModel: string,
): boolean {
  const normalize = (value: unknown) => String(value || "")
    .trim()
    .replace(/\\/g, "/")
    .replace(/\/{2,}/g, "/")
    .replace(/\/$/, "")
    .toLowerCase();
  const current = normalize(currentModel);
  if (!current || !current.includes("/")) return false;
  return normalize(model.path) === current;
}

export function scanUserModels() {
  const generation = ++modelScanGeneration;
  store.setState({modelsScanning: true});
  if (!sendGeneral({type: "model:list"})) {
    store.setState({modelsScanning: false});
    notifyGeneral("Backend offline — model folder was not scanned");
    return false;
  }
  window.setTimeout(() => {
    if (generation !== modelScanGeneration || !store.getState().modelsScanning) return;
    modelScanGeneration += 1;
    store.setState({modelsScanning: false});
    notifyGeneral("Model scan timed out");
  }, 20_000);
  notifyGeneral("Scanning models\\user…");
  return true;
}

export async function openUserModelFolder() {
  try {
    const result = await store.getContext()?.api?.openAppPath?.("modelsUser");
    if (result && result.ok === false) {
      notifyGeneral(result.reason || "Could not open models\\user");
      return false;
    }
    return true;
  } catch {
    notifyGeneral("Could not open models\\user");
    return false;
  }
}

export async function setLaunchAtLogin(on: boolean) {
  try {
    const setter = store.getContext()?.api?.setLaunchAtLogin;
    if (!setter) throw new Error("Startup integration is unavailable");
    const result = await setter(on);
    const ok = typeof result === "object" && result !== null
      ? result.ok && result.value === on
      : result === on;
    if (!ok) {
      const reason = typeof result === "object" && result !== null
        ? result.reason
        : "setting was not applied";
      throw new Error(reason || "setting was not applied");
    }
    store.setState({launchAtLogin: on});
    return true;
  } catch {
    notifyGeneral("Could not update launch setting");
    return false;
  }
}

export async function setStartHidden(on: boolean) {
  try {
    const setter = store.getContext()?.api?.setStartHidden;
    if (!setter) throw new Error("Startup integration is unavailable");
    const result = await setter(on);
    const ok = typeof result === "object" && result !== null
      ? result.ok && result.value === on
      : result === on;
    if (!ok) throw new Error("setting was not applied");
    store.setState({startHidden: on});
    return true;
  } catch {
    notifyGeneral("Could not update startup setting");
    return false;
  }
}

export function useGeneralState() {
  return store.useStore();
}

export function getGeneralState() {
  return store.getState();
}
