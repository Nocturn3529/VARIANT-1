/**
 * AudioWorklet processor for Main Deck voice capture.
 * Runs on the audio rendering thread (not the UI thread).
 *
 * Posts { type: "frame", samples: Float32Array, rms: number, frames: number }
 * for each input quantum. Transferable sample buffers avoid an extra copy
 * on the main thread when possible.
 */
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
        [samples.buffer],
      );
    }
    return true;
  }
}

registerProcessor("variant1-mic-capture", Variant1MicCaptureProcessor);
