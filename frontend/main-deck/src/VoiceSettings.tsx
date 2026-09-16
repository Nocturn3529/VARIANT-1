import {useEffect, useMemo, useRef, useState, type FormEvent} from "react";
import {
  notifyGeneral,
  refreshGeneral,
  sendGeneral,
  useGeneralState,
} from "./generalStore";
import type {SpeechProviderInfo} from "./types";
import {Button} from "./ui/Button";
import {createSwitchRow} from "./ui/Switch";

const SwitchRow = createSwitchRow("gen-row deck-data-row");

function status(provider: SpeechProviderInfo): string {
  if (provider.available) return "Ready";
  if (provider.kind === "local") return "Runtime needed";
  return "Credential needed";
}

function SpeechCredential({
  capability,
  provider,
}: {
  capability: "stt" | "tts";
  provider: SpeechProviderInfo;
}) {
  const receipt = useGeneralState().speechReceipt;
  const [draft, setDraft] = useState("");
  const [requestId, setRequestId] = useState("");
  const [requestAction, setRequestAction] = useState<"set" | "clear">("set");
  useEffect(() => {
    if (!requestId || receipt?.requestId !== requestId) return;
    if (receipt.accepted) {
      setDraft("");
      notifyGeneral(`${provider.name} credential ${requestAction === "clear" ? "removed" : "saved"}`);
    } else notifyGeneral(receipt.error || "Could not save speech credential");
    setRequestId("");
  }, [provider.name, receipt, requestAction, requestId]);

  function save(event: FormEvent) {
    event.preventDefault();
    if (!draft.trim()) return;
    const id = `speech-key-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    if (!sendGeneral({type: "speech:credential:set", request_id: id,
      capability, provider: provider.id, key: draft.trim()})) {
      notifyGeneral("Could not save credential — backend offline");
      return;
    }
    setRequestAction("set");
    setRequestId(id);
  }

  function clear() {
    const id = `speech-key-clear-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    if (!sendGeneral({type: "speech:credential:clear", request_id: id,
      capability, provider: provider.id})) {
      notifyGeneral("Could not remove credential — backend offline");
      return;
    }
    setRequestAction("clear");
    setRequestId(id);
  }

  return <form className="voice-provider-credential" onSubmit={save}>
    <label><span>API key</span><input type="password" value={draft}
      onChange={event => setDraft(event.target.value)}
      placeholder={provider.configured ? "Enter a replacement key" : "Paste API key"}
      autoComplete="off" spellCheck={false}/></label>
    <Button type="submit" tone="primary" disabled={!draft.trim() || !!requestId}>Save</Button>
    {provider.configured ? <Button tone="quiet" disabled={!!requestId} onClick={clear}>Remove</Button> : null}
  </form>;
}

function ProviderOptions({
  capability,
  provider,
}: {
  capability: "stt" | "tts";
  provider: SpeechProviderInfo;
}) {
  const stored = provider.config || {};
  const [model, setModel] = useState(String(stored.model || provider.default_model || ""));
  const [baseUrl, setBaseUrl] = useState(String(stored.base_url || ""));
  const [language, setLanguage] = useState(String(stored.language || "auto"));
  const [modelPath, setModelPath] = useState(String(stored.model_path || ""));
  const [refAudio, setRefAudio] = useState(String(stored.ref_audio || ""));
  const [refText, setRefText] = useState(String(stored.ref_text || ""));
  const [device, setDevice] = useState(String(stored.device || "cpu"));
  const receipt = useGeneralState().speechReceipt;
  const [requestId, setRequestId] = useState("");

  useEffect(() => {
    if (!requestId || receipt?.requestId !== requestId) return;
    notifyGeneral(receipt.accepted
      ? `${provider.name} settings saved`
      : receipt.error || `Could not save ${provider.name} settings`);
    setRequestId("");
  }, [provider.name, receipt, requestId]);

  function save(event: FormEvent) {
    event.preventDefault();
    const fields: Record<string, unknown> = {model, base_url: baseUrl, language};
    if (provider.id === "piper") fields.model_path = modelPath;
    if (provider.id === "neutts") Object.assign(fields, {ref_audio: refAudio, ref_text: refText, device});
    const id = `speech-options-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    if (!sendGeneral({type: "tts:set", request_id: id, key: `${capability}_options`,
      value: {provider: provider.id, fields}})) {
      notifyGeneral("Could not save speech settings — backend offline");
      return;
    }
    setRequestId(id);
  }

  return <form className="voice-provider-options" onSubmit={save}>
    <label><span>Model</span><input value={model} onChange={event => setModel(event.target.value)}
      placeholder={provider.default_model || "Provider default"}/></label>
    {provider.kind === "cloud" ? <label><span>Base URL override</span><input value={baseUrl}
      onChange={event => setBaseUrl(event.target.value)} placeholder="Use provider default"/></label> : null}
    {capability === "stt" || provider.id === "xai" ? <label><span>Language</span><input
      value={language} onChange={event => setLanguage(event.target.value)} placeholder="auto"/></label> : null}
    {provider.id === "piper" ? <label><span>Voice model path</span><input value={modelPath}
      onChange={event => setModelPath(event.target.value)} placeholder="C:\\models\\voice.onnx"/></label> : null}
    {provider.id === "neutts" ? <>
      <label><span>Reference audio</span><input value={refAudio}
        onChange={event => setRefAudio(event.target.value)} placeholder="C:\\voices\\reference.wav"/></label>
      <label><span>Reference transcript</span><input value={refText}
        onChange={event => setRefText(event.target.value)} placeholder="C:\\voices\\reference.txt"/></label>
      <label><span>Device</span><select value={device} onChange={event => setDevice(event.target.value)}>
        <option value="cpu">CPU</option><option value="cuda">CUDA</option><option value="mps">MPS</option>
      </select></label>
    </> : null}
    <Button type="submit" tone="quiet" disabled={!!requestId}>{requestId ? "Saving…" : "Save provider settings"}</Button>
  </form>;
}

function ProviderPanel({
  capability,
  providers,
  selectedId,
}: {
  capability: "stt" | "tts";
  providers: SpeechProviderInfo[];
  selectedId: string;
}) {
  const [choice, setChoice] = useState(selectedId);
  useEffect(() => setChoice(selectedId), [selectedId]);
  const provider = providers.find(item => item.id === choice) || providers[0];
  if (!provider) return <section className="voice-provider-card"><p>No providers available.</p></section>;
  const key = capability === "tts" ? "tts_provider" : "stt_provider";
  const needsCredential = provider.auth === "api_key";
  return <section className="voice-provider-card deck-instrument">
    <div className="voice-provider-select deck-data-row">
      <span><strong>{capability === "tts" ? "Speech output" : "Speech input"}</strong>
        <small>{provider.description}</small></span>
      <select value={choice} onChange={event => {
        setChoice(event.target.value);
        sendGeneral({type: "tts:set", key, value: event.target.value});
      }}>
        {providers.map(item => <option value={item.id} key={item.id}>{item.name}</option>)}
      </select>
      <em data-ready={provider.available}>{status(provider)}</em>
    </div>
    {provider.auth === "shared" ? <p className="voice-provider-note">
      VARIANT-1 reuses this provider's connected account or API key.
    </p> : null}
    {needsCredential ? <SpeechCredential key={`credential:${capability}:${provider.id}`} capability={capability} provider={provider}/> : null}
    <ProviderOptions key={`${capability}:${provider.id}`} capability={capability} provider={provider}/>
  </section>;
}

function LocalAssetNotice({path, children}: {path: string; children: string}) {
  return <div className="gen-row gen-asset-drop deck-data-row">
    <span><strong>User-supplied runtime</strong><small>{children}</small>{path ? <code>{path}</code> : null}</span>
    <div><Button tone="quiet" disabled={!path} onClick={() => {
      if (path) void window.variant1Deck?.openLocalPath?.(path);
    }}>Open folder</Button><Button tone="quiet" onClick={refreshGeneral}>Refresh</Button></div>
  </div>;
}

export function VoiceSettings() {
  const state = useGeneralState();
  const [voiceDraft, setVoiceDraft] = useState(state.currentVoice);
  const voiceCancelled = useRef(false);
  const [speedDraft, setSpeedDraft] = useState(state.voiceSpeed);
  const [autoSpeakDraft, setAutoSpeakDraft] = useState(state.voiceEnabled);
  useEffect(() => setVoiceDraft(state.currentVoice), [state.currentVoice, state.ttsProvider]);
  useEffect(() => setSpeedDraft(state.voiceSpeed), [state.voiceSpeed]);
  useEffect(() => setAutoSpeakDraft(state.voiceEnabled), [state.voiceEnabled]);
  const selectedTts = useMemo(() => state.ttsProviders.find(item => item.id === state.ttsProvider),
    [state.ttsProvider, state.ttsProviders]);
  const anyCloud = selectedTts?.kind === "cloud"
    || state.sttProviders.find(item => item.id === state.sttProvider)?.kind === "cloud";

  return <div className="voice-settings">
    <ProviderPanel capability="stt" providers={state.sttProviders} selectedId={state.sttProvider}/>
    {state.sttProvider === "local" && !state.sttLocalAvailable ? <LocalAssetNotice path={state.sttDropPath}>
      Drop whisper-server.exe, its adjacent DLLs, and a compatible ggml .bin model here.
    </LocalAssetNotice> : null}
    <ProviderPanel capability="tts" providers={state.ttsProviders} selectedId={state.ttsProvider}/>
    {state.ttsProvider === "kokoro" && !state.ttsLocalAvailable ? <LocalAssetNotice path={state.ttsDropPath}>
      Drop kokoro-v1.0.onnx and voices-v1.0.bin here.
    </LocalAssetNotice> : null}

    <section className="gen-card gen-card--speaker deck-instrument">
      <header className="deck-instrument__header"><div><h3 className="deck-instrument__title">Playback</h3></div>
        <Button tone="quiet" disabled={!state.voiceAvailable} onClick={() => {
          sendGeneral({type: "tts:preview", purpose: "preview", voice: voiceDraft});
          notifyGeneral("Generating voice sample…");
        }}>Play sample</Button></header>
      <div className="gen-stack deck-data-list">
        {state.voicesError ? <p className="runtime-error" role="alert">{state.voicesError}</p> : null}
        <SwitchRow title="Speak replies out loud"
          detail="Automatically play a spoken version when a reply finishes. Manual Play remains available."
          checked={autoSpeakDraft} disabled={!state.voiceAvailable}
          onChange={value => {
            setAutoSpeakDraft(value);
            sendGeneral({type: "tts:set", key: "auto_tts", value});
          }}/>
        <div className="gen-row deck-data-row"><span><strong>Voice</strong>
          <small>Free input accepts provider voices that are not in the suggestions.</small></span>
          <input className="gen-select" value={voiceDraft}
            list="active-tts-voices" onFocus={() => { voiceCancelled.current = false; }} onChange={event => setVoiceDraft(event.target.value)}
            onBlur={event => {
              if (voiceCancelled.current) return;
              if (event.currentTarget.value !== state.currentVoice) sendGeneral({type: "tts:set", key: "voice", value: event.currentTarget.value});
            }}
            onKeyDown={event => {
              if (event.key === "Enter") { event.preventDefault(); event.currentTarget.blur(); }
              if (event.key === "Escape") {
                event.preventDefault();
                event.stopPropagation();
                voiceCancelled.current = true;
                setVoiceDraft(state.currentVoice);
                event.currentTarget.blur();
              }
            }}/>
          <datalist id="active-tts-voices">{state.voices.map(voice =>
            <option value={voice.id} key={voice.id}>{voice.name}</option>)}</datalist>
        </div>
        <label className="gen-row gen-row--range deck-data-row"><span><strong>Voice speed</strong>
          <small>Provider-safe speech speed.</small></span><span className="gen-range">
          <input type="range" min={25} max={400} value={Math.round(speedDraft * 100)}
            disabled={!state.voiceAvailable} onChange={event => setSpeedDraft(Number(event.target.value) / 100)}
            onPointerUp={event => sendGeneral({type: "tts:set", key: "speed", value: event.currentTarget.valueAsNumber / 100})}
            onKeyUp={event => sendGeneral({type: "tts:set", key: "speed", value: event.currentTarget.valueAsNumber / 100})}
            onBlur={event => sendGeneral({type: "tts:set", key: "speed", value: event.currentTarget.valueAsNumber / 100})}/><output>{speedDraft.toFixed(2)}x</output></span></label>
      </div>
      <div className={`gen-info deck-data-row${anyCloud ? " gen-info--cloud" : ""}`}>
        <span className="gen-info__icon" aria-hidden="true">{anyCloud ? "C" : "L"}</span>
        <div><strong>{anyCloud ? "Cloud speech provider selected" : "Speech stays on this machine"}</strong>
          <p>Only the selected provider receives audio or reply text.</p></div>
      </div>
    </section>
  </div>;
}
