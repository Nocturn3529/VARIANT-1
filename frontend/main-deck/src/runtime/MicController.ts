import type {WsCommand} from "../protocol";

export const MIC_SILENCE_LIMIT_MS = 1_200;
export const MIC_RMS_GATE = 0.012;
export const MIC_MAX_MS = 30_000;
export const MIC_WORKLET_NAME = "variant1-mic-capture";

export type MicPhase =
  | "idle"
  | "requesting"
  | "recording"
  | "encoding"
  | "transcribing"
  | "error";

type CaptureNode = {
  port: {onmessage: ((event: MessageEvent) => void) | null};
  connect(destination: unknown): unknown;
  disconnect(): void;
};

type SourceNode = {
  connect(destination: unknown): unknown;
  disconnect(): void;
};

type GainNodeLike = {
  gain: {value: number};
  connect(destination: unknown): unknown;
  disconnect(): void;
};

export type AudioContextLike = {
  readonly sampleRate: number;
  readonly state: string;
  readonly destination: unknown;
  readonly audioWorklet?: {addModule(url: string): Promise<void>};
  resume(): Promise<void>;
  close(): Promise<void>;
  createMediaStreamSource(stream: MediaStream): SourceNode;
  createGain(): GainNodeLike;
};

export type MicControllerOptions = {
  isOpen: () => boolean;
  send: (command: WsCommand) => boolean;
  notify: (message: string) => void;
  onPhase: (phase: MicPhase, error?: string) => void;
  getSessionId?: () => string;
  stopSpeech?: () => void;
  getUserMedia?: () => Promise<MediaStream>;
  createAudioContext?: () => AudioContextLike;
  loadWorklet?: (context: AudioContextLike) => Promise<void>;
  createWorkletNode?: (context: AudioContextLike) => CaptureNode;
  encodeAudio?: (
    chunks: Float32Array[],
    inputRate: number,
    outputRate: number,
  ) => string;
  now?: () => number;
  setTimer?: (callback: () => void, delayMs: number) => ReturnType<typeof setTimeout>;
  clearTimer?: (timer: ReturnType<typeof setTimeout>) => void;
};

const WORKLET_SOURCE = `
class Variant1MicCaptureProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel && channel.length) {
      const samples = new Float32Array(channel.length);
      samples.set(channel);
      let sum = 0;
      for (let i = 0; i < samples.length; i += 1) sum += samples[i] * samples[i];
      const rms = Math.sqrt(sum / samples.length);
      this.port.postMessage(
        { type: "frame", samples, rms, frames: samples.length },
        [samples.buffer]
      );
    }
    return true;
  }
}
registerProcessor("${MIC_WORKLET_NAME}", Variant1MicCaptureProcessor);
`;

export function flattenAudioChunks(chunks: Float32Array[]): Float32Array {
  const length = chunks.reduce((total, chunk) => total + chunk.length, 0);
  const output = new Float32Array(length);
  let offset = 0;
  chunks.forEach(chunk => {
    output.set(chunk, offset);
    offset += chunk.length;
  });
  return output;
}

export function downsampleAudio(
  buffer: Float32Array,
  inputRate: number,
  outputRate: number,
): Float32Array {
  if (outputRate >= inputRate) return buffer;
  const ratio = inputRate / outputRate;
  const output = new Float32Array(Math.floor(buffer.length / ratio));
  for (let index = 0; index < output.length; index += 1) {
    const start = Math.floor(index * ratio);
    const end = Math.min(buffer.length, Math.floor((index + 1) * ratio));
    let sum = 0;
    for (let source = start; source < end; source += 1) sum += buffer[source];
    output[index] = end > start ? sum / (end - start) : 0;
  }
  return output;
}

export function encodeWav(
  chunks: Float32Array[],
  inputRate: number,
  outputRate: number,
): ArrayBuffer {
  const mono = downsampleAudio(flattenAudioChunks(chunks), inputRate, outputRate);
  const buffer = new ArrayBuffer(44 + mono.length * 2);
  const view = new DataView(buffer);
  const write = (offset: number, text: string) => {
    for (let index = 0; index < text.length; index += 1) {
      view.setUint8(offset + index, text.charCodeAt(index));
    }
  };
  write(0, "RIFF");
  view.setUint32(4, 36 + mono.length * 2, true);
  write(8, "WAVE");
  write(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, outputRate, true);
  view.setUint32(28, outputRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  write(36, "data");
  view.setUint32(40, mono.length * 2, true);
  let offset = 44;
  mono.forEach(sample => {
    const clamped = Math.max(-1, Math.min(1, sample));
    view.setInt16(
      offset,
      clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff,
      true,
    );
    offset += 2;
  });
  return buffer;
}

export function audioBufferToBase64(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let index = 0; index < bytes.length; index += 0x8000) {
    binary += String.fromCharCode(
      ...bytes.subarray(index, Math.min(bytes.length, index + 0x8000)),
    );
  }
  return btoa(binary);
}

export async function loadMicWorklet(context: AudioContextLike): Promise<void> {
  if (!context.audioWorklet?.addModule) {
    throw new Error("AudioWorklet unavailable");
  }
  try {
    const url = new URL(
      "public/mic-capture-processor.js",
      window.location.href,
    ).href;
    await context.audioWorklet.addModule(url);
  } catch {
    const blob = new Blob([WORKLET_SOURCE], {type: "application/javascript"});
    const url = URL.createObjectURL(blob);
    try {
      await context.audioWorklet.addModule(url);
    } finally {
      URL.revokeObjectURL(url);
    }
  }
}

function defaultAudioContext(): AudioContextLike {
  const AudioContextConstructor = (
    window as typeof window & {webkitAudioContext?: typeof AudioContext}
  ).AudioContext || (
    window as typeof window & {webkitAudioContext?: typeof AudioContext}
  ).webkitAudioContext;
  if (!AudioContextConstructor) throw new Error("AudioContext unavailable");
  return new AudioContextConstructor() as unknown as AudioContextLike;
}

function defaultWorkletNode(context: AudioContextLike): CaptureNode {
  return new AudioWorkletNode(
    context as unknown as AudioContext,
    MIC_WORKLET_NAME,
    {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      channelCount: 1,
      outputChannelCount: [1],
    },
  ) as unknown as CaptureNode;
}

function defaultEncode(
  chunks: Float32Array[],
  inputRate: number,
  outputRate: number,
): string {
  return audioBufferToBase64(encodeWav(chunks, inputRate, outputRate));
}

export class MicController {
  private readonly options: Required<
    Pick<MicControllerOptions, "isOpen" | "send" | "notify" | "onPhase">
  > & MicControllerOptions;
  private phase: MicPhase = "idle";
  private operation = 0;
  private stream: MediaStream | null = null;
  private audioContext: AudioContextLike | null = null;
  private source: SourceNode | null = null;
  private worklet: CaptureNode | null = null;
  private gain: GainNodeLike | null = null;
  private chunks: Float32Array[] = [];
  private sampleRate = 48_000;
  private silenceMs = 0;
  private spoke = false;
  private startedAt = 0;
  private requestSequence = 0;
  private pendingRequestId = "";
  private pendingSessionId = "";
  private captureSessionId = "";
  private maxTimer: ReturnType<typeof setTimeout> | null = null;
  private trackEndListeners: Array<{
    track: MediaStreamTrack;
    listener: () => void;
  }> = [];
  private deviceChangeListener: (() => void) | null = null;

  constructor(options: MicControllerOptions) {
    this.options = options;
  }

  getPhase(): MicPhase {
    return this.phase;
  }

  toggle(): void {
    if (this.phase === "recording") {
      this.stop(true);
    } else if (this.phase === "requesting") {
      this.stop(false);
    } else if (this.phase === "transcribing") {
      this.cancelTranscription();
    } else if (this.phase === "idle" || this.phase === "error") {
      void this.start();
    }
  }

  async start(): Promise<void> {
    if (!this.options.isOpen()) {
      this.options.notify("VARIANT-1 is reconnecting to the local backend");
      return;
    }
    if (this.phase !== "idle" && this.phase !== "error") return;
    // Capture owns the chat selected before the permission prompt starts.
    this.captureSessionId = String(this.options.getSessionId?.() || "");
    if (this.options.getSessionId && !this.captureSessionId) {
      this.options.notify("Open a chat before recording.");
      return;
    }
    this.options.stopSpeech?.();
    const operation = ++this.operation;
    this.setPhase("requesting");

    try {
      const stream = await (
        this.options.getUserMedia
        ?? (() => navigator.mediaDevices.getUserMedia({
          audio: {
            channelCount: 1,
            echoCancellation: true,
            noiseSuppression: true,
          },
        }))
      )();
      if (operation !== this.operation) {
        stream.getTracks().forEach(track => track.stop());
        return;
      }
      this.stream = stream;
      this.watchCaptureLiveness(stream);
      const context = (
        this.options.createAudioContext ?? defaultAudioContext
      )();
      this.audioContext = context;
      if (context.state === "suspended") await context.resume();
      if (operation !== this.operation) {
        this.cleanupGraph();
        return;
      }
      this.sampleRate = context.sampleRate;
      this.source = context.createMediaStreamSource(stream);
      this.chunks = [];
      this.silenceMs = 0;
      this.spoke = false;
      this.startedAt = (this.options.now ?? (() => performance.now()))();
      await (this.options.loadWorklet ?? loadMicWorklet)(context);
      if (operation !== this.operation) {
        this.cleanupGraph();
        return;
      }
      this.worklet = (
        this.options.createWorkletNode ?? defaultWorkletNode
      )(context);
      this.worklet.port.onmessage = event => {
        const data = event.data as {
          type?: string;
          samples?: Float32Array;
          rms?: number;
          frames?: number;
        };
        if (data?.type !== "frame") return;
        this.onAudioFrame(
          data.samples,
          Number(data.rms) || 0,
          Number(data.frames) || data.samples?.length || 0,
        );
      };
      this.gain = context.createGain();
      this.gain.gain.value = 0;
      this.source.connect(this.worklet);
      this.worklet.connect(this.gain);
      this.gain.connect(context.destination);
      this.setPhase("recording");
      this.maxTimer = (this.options.setTimer ?? setTimeout)(() => {
        if (this.phase === "recording") this.stop(true);
      }, MIC_MAX_MS);
    } catch (error) {
      if (operation !== this.operation) return;
      this.cleanupGraph();
      const message = error instanceof DOMException && error.name === "NotAllowedError"
        ? "Couldn't access the microphone"
        : "Microphone capture isn't available in this environment";
      this.options.notify(message);
      this.setPhase("error", message);
    }
  }

  stop(sendClip = true): void {
    if (!sendClip && this.phase === "transcribing") {
      this.cancelTranscription();
      return;
    }
    if (this.phase === "requesting") {
      this.operation += 1;
      this.cleanupGraph();
      this.setPhase("idle");
      return;
    }
    if (this.phase !== "recording") return;
    this.operation += 1;
    const rate = this.sampleRate;
    const captured = this.chunks;
    const hasSpeech = this.spoke;
    this.chunks = [];
    this.cleanupGraph();
    if (!sendClip || !hasSpeech) {
      this.setPhase("idle");
      if (sendClip && !hasSpeech) this.options.notify("I didn't catch that");
      return;
    }
    this.setPhase("encoding");
    try {
      const audio = (this.options.encodeAudio ?? defaultEncode)(
        captured,
        rate,
        16_000,
      );
      this.setPhase("transcribing");
      const sessionId = this.captureSessionId;
      const requestId = `voice-${Date.now()}-${++this.requestSequence}`;
      this.pendingRequestId = requestId;
      this.pendingSessionId = sessionId;
      if (!this.options.send({
        type: "voice:transcribe",
        audio,
        request_id: requestId,
        session_id: sessionId,
      })) {
        this.pendingRequestId = "";
        this.pendingSessionId = "";
        this.setPhase("error", "Backend offline — reconnecting");
        this.options.notify("VARIANT-1 is reconnecting to the local backend");
      }
    } catch {
      this.setPhase("error", "Voice capture failed");
      this.options.notify("Voice capture failed");
    }
  }

  completeTranscription(requestId = "", sessionId = ""): boolean {
    if (this.phase !== "transcribing" || !this.pendingRequestId
        || requestId !== this.pendingRequestId || sessionId !== this.pendingSessionId) return false;
    this.pendingRequestId = "";
    this.pendingSessionId = "";
    this.setPhase("idle");
    return true;
  }

  dispose(): void {
    this.operation += 1;
    this.captureSessionId = "";
    this.pendingRequestId = "";
    this.pendingSessionId = "";
    this.chunks = [];
    this.cleanupGraph();
    this.setPhase("idle");
  }

  private onAudioFrame(
    samples: Float32Array | undefined,
    rms: number,
    frames: number,
  ): void {
    if (this.phase !== "recording") return;
    if (samples?.length) this.chunks.push(samples);
    const frameMs = frames / this.sampleRate * 1000;
    if (rms >= MIC_RMS_GATE) {
      this.spoke = true;
      this.silenceMs = 0;
    } else if (this.spoke) {
      this.silenceMs += frameMs;
    }
    const now = (this.options.now ?? (() => performance.now()))();
    if (
      (this.spoke && this.silenceMs >= MIC_SILENCE_LIMIT_MS)
      || now - this.startedAt > MIC_MAX_MS
    ) {
      this.stop(true);
    }
  }

  private cleanupGraph(): void {
    if (this.maxTimer !== null) {
      (this.options.clearTimer ?? clearTimeout)(this.maxTimer);
      this.maxTimer = null;
    }
    for (const {track, listener} of this.trackEndListeners) {
      try { track.removeEventListener?.("ended", listener); } catch {}
    }
    this.trackEndListeners = [];
    if (this.deviceChangeListener) {
      try {
        navigator.mediaDevices?.removeEventListener?.(
          "devicechange", this.deviceChangeListener,
        );
      } catch {}
      this.deviceChangeListener = null;
    }
    try {
      if (this.worklet) this.worklet.port.onmessage = null;
    } catch {}
    try { this.worklet?.disconnect(); } catch {}
    try { this.gain?.disconnect(); } catch {}
    try { this.source?.disconnect(); } catch {}
    try {
      this.stream?.getTracks().forEach(track => track.stop());
    } catch {}
    try {
      void this.audioContext?.close();
    } catch {}
    this.worklet = null;
    this.gain = null;
    this.source = null;
    this.stream = null;
    this.audioContext = null;
  }

  private cancelTranscription(): void {
    const requestId = this.pendingRequestId;
    const sessionId = this.pendingSessionId;
    // Retire locally before requesting backend cancellation: a late normal
    // transcript must never submit speech the user has just discarded.
    this.pendingRequestId = "";
    this.pendingSessionId = "";
    this.setPhase("idle");
    if (requestId) this.options.send({
      type: "voice:transcribe:cancel",
      request_id: requestId,
      session_id: sessionId,
    });
  }

  private watchCaptureLiveness(stream: MediaStream): void {
    const lost = () => {
      if (this.phase !== "requesting" && this.phase !== "recording") return;
      this.operation += 1;
      this.chunks = [];
      this.cleanupGraph();
      const message = "Microphone device became unavailable";
      this.setPhase("error", message);
      this.options.notify(message);
    };
    for (const track of stream.getAudioTracks?.() || stream.getTracks()) {
      if (typeof track.addEventListener !== "function") continue;
      const listener = () => lost();
      track.addEventListener("ended", listener, {once: true});
      this.trackEndListeners.push({track, listener});
    }
    /* Track `ended` already covers the capture device disappearing.
       `devicechange` also fires for headphones/Bluetooth and must not abort
       a still-live recording track. */
  }

  private setPhase(phase: MicPhase, error = ""): void {
    this.phase = phase;
    this.options.onPhase(phase, error);
  }
}
