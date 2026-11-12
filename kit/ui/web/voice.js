const processorName = "zaytoun-mic";
const maxPcmBytes = 10 * 1024 * 1024 - 44;

// The browser executes this same module in the audio rendering thread.
if (typeof registerProcessor === "function") {
  registerProcessor(processorName, class extends AudioWorkletProcessor {
    constructor() {
      super();
      this.buffer = new Float32Array(2048);
      this.length = 0;
      this.finished = false;
      this.port.onmessage = () => {
        this.finished = true;
        this.flush();
        this.port.postMessage("finished");
      };
    }

    flush() {
      if (!this.length) return;
      const samples = this.buffer.slice(0, this.length);
      this.port.postMessage(samples, [samples.buffer]);
      this.length = 0;
    }

    process(inputs) {
      const samples = inputs[0]?.[0];
      if (this.finished || !samples) return true;
      for (const sample of samples) {
        this.buffer[this.length++] = sample;
        if (this.length === this.buffer.length) this.flush();
      }
      return true;
    }
  });
}

function encodeWav(chunks, frames, sampleRate) {
  const bytes = new ArrayBuffer(44 + frames * 2);
  const header = new DataView(bytes);
  const write = (offset, label) => {
    for (let i = 0; i < label.length; i++) header.setUint8(offset + i, label.charCodeAt(i));
  };
  write(0, "RIFF");
  header.setUint32(4, bytes.byteLength - 8, true);
  write(8, "WAVE");
  write(12, "fmt ");
  header.setUint32(16, 16, true);
  header.setUint16(20, 1, true);
  header.setUint16(22, 1, true);
  header.setUint32(24, sampleRate, true);
  header.setUint32(28, sampleRate * 2, true);
  header.setUint16(32, 2, true);
  header.setUint16(34, 16, true);
  write(36, "data");
  header.setUint32(40, frames * 2, true);
  let offset = 44;
  for (const chunk of chunks) {
    for (const sample of chunk) {
      const bounded = Math.max(-1, Math.min(1, sample));
      header.setInt16(offset, bounded * (bounded < 0 ? 32768 : 32767), true);
      offset += 2;
    }
  }
  return new Blob([bytes], { type: "audio/wav" });
}

export function createRecorder({ onState, onAudio, onError }) {
  let current;
  let requestingPermission = false;

  function stopTracks(stream) {
    stream?.getTracks().forEach(track => track.stop());
  }

  function release(recording) {
    current = undefined;
    clearInterval(recording.clock);
    clearTimeout(recording.deadline);
    if (recording.node) {
      recording.node.port.onmessage = null;
      recording.node.port.close();
      recording.node.disconnect();
    }
    recording.source?.disconnect();
    stopTracks(recording.stream);
    if (recording.context?.state !== "closed") recording.context?.close().catch(() => {});
    recording.chunks.length = 0;
  }

  function fail(recording, code) {
    if (current !== recording) return;
    release(recording);
    onState("idle", 0);
    onError(code);
  }

  async function start(language) {
    if (current || requestingPermission) return;
    if (!globalThis.navigator?.mediaDevices?.getUserMedia || !globalThis.AudioContext || !globalThis.AudioWorkletNode) {
      onError("unsupported");
      return;
    }

    const recording = { language, chunks: [], frames: 0, state: "requesting" };
    current = recording;
    onState("requesting", 0);
    try {
      // Request resume in the click's activation window, including on iOS.
      // Keep the device rate so browsers can attach the microphone directly.
      // The existing STT pipeline resamples WAV before transcription.
      recording.context = new AudioContext();
      const ready = recording.context.resume().then(() => true, () => false);
      requestingPermission = true;
      try {
        recording.stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1 } });
      } finally {
        requestingPermission = false;
      }
      if (current !== recording) {
        stopTracks(recording.stream);
        return;
      }

      await recording.context.audioWorklet.addModule(import.meta.url);
      if (!(await ready)) throw new Error("Audio context did not start");
      if (current !== recording) return;
      recording.source = recording.context.createMediaStreamSource(recording.stream);
      recording.node = new AudioWorkletNode(recording.context, processorName, {
        channelCount: 1,
        channelCountMode: "explicit",
      });
      recording.node.port.onmessage = ({ data }) => {
        if (current !== recording) return;
        if (data === "finished") {
          if (recording.state !== "stopping") return;
          if (recording.frames * 2 + 44 < 1000) {
            fail(recording, "recording");
            return;
          }
          const audio = encodeWav(recording.chunks, recording.frames, recording.context.sampleRate);
          release(recording);
          try {
            onAudio(audio, recording.language);
          } finally {
            onState("idle", 0);
          }
          return;
        }
        if ((recording.frames + data.length) * 2 > maxPcmBytes) {
          fail(recording, "tooLong");
          return;
        }
        recording.chunks.push(data);
        recording.frames += data.length;
      };
      recording.node.onprocessorerror = () => fail(recording, "recording");
      for (const track of recording.stream.getAudioTracks()) {
        track.onended = () => {
          if (recording.state === "recording") fail(recording, "recording");
        };
        if (track.readyState === "ended") throw new Error("Microphone disconnected");
      }
      // The worklet writes no output, so connecting it keeps capture running silently.
      recording.source.connect(recording.node);
      recording.node.connect(recording.context.destination);
      recording.state = "recording";
      recording.started = performance.now();
      recording.clock = setInterval(() => onState("recording", elapsed(recording)), 250);
      onState("recording", 0);
    } catch (error) {
      const denied = error.name === "NotAllowedError" || error.name === "SecurityError";
      fail(recording, denied ? "permission" : "recording");
    }
  }

  function elapsed(recording) {
    return Math.floor((performance.now() - recording.started) / 1000);
  }

  function stop() {
    const recording = current;
    if (recording?.state !== "recording") return;
    recording.state = "stopping";
    clearInterval(recording.clock);
    stopTracks(recording.stream);
    onState("stopping", elapsed(recording));
    recording.deadline = setTimeout(() => fail(recording, "recording"), 2000);
    recording.node.port.postMessage("finish");
  }

  function cancel() {
    if (!current) return;
    release(current);
    onState("idle", 0);
  }

  return { start, stop, cancel };
}
