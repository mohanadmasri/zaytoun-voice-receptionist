const sampleRate = 24000;
const startupBytes = 1920; // 40 ms of 24 kHz, 16-bit PCM
const maxBytes = sampleRate * 2 * 90;

export function createSpeechStream() {
  let context;
  let controller;
  let resuming;
  let cached;
  let playbackEpoch = 0;
  const sources = new Set();

  function unlock() {
    if (!globalThis.AudioContext) return;
    try {
      context ??= new AudioContext();
      if (context.state !== "running" && !resuming) {
        resuming = context.resume().catch(() => {}).finally(() => { resuming = null; });
      }
    } catch {
      // play() reports unavailable audio so the user can retry.
    }
  }

  function stop() {
    playbackEpoch++;
    controller?.abort();
    controller = null;
    for (const source of sources) source.stop();
    sources.clear();
  }

  function schedule(bytes, at) {
    const count = bytes.byteLength / 2;
    const buffer = context.createBuffer(1, count, sampleRate);
    const samples = buffer.getChannelData(0);
    const pcm = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    for (let i = 0; i < count; i++) samples[i] = pcm.getInt16(i * 2, true) / 32768;
    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(context.destination);
    source.onended = () => {
      sources.delete(source);
      source.disconnect();
    };
    source.start(at);
    sources.add(source);
    return at + buffer.duration;
  }

  async function play(data, replyReceived) {
    const epoch = playbackEpoch;
    if (!context || (context.state !== "running" && !resuming)) return "playback_unavailable";
    const ready = resuming && Promise.race([resuming, new Promise(resolve => setTimeout(resolve, 300))]);
    if (cached?.data === data) {
      if (ready) await ready;
      if (epoch !== playbackEpoch) return "cancelled";
      if (context.state !== "running") return "playback_unavailable";
      try {
        schedule(cached.bytes, context.currentTime + 0.02);
        return "played";
      } catch {
        return "playback_unavailable";
      }
    }
    if (!data.speech_token) return "replay_unavailable";

    const token = data.speech_token;
    const request = new AbortController();
    controller = request;
    const timeout = setTimeout(() => request.abort("timeout"), 35000);
    let played = false;
    try {
      const requested = performance.now();
      const response = await fetch("/api/speech/" + encodeURIComponent(token), {
        method: "POST", cache: "no-store", signal: request.signal,
      });
      if (!response.ok) {
        if (response.status === 404) return "ticket_unavailable";
        return response.headers.get("x-speech-outcome") === "provider_payload"
          ? "provider_payload" : "provider_failure";
      }
      if (!response.body || response.headers.get("content-type") !== "audio/pcm" ||
          response.headers.get("x-audio-sample-rate") !== String(sampleRate)) return "invalid_stream";

      const reader = response.body.getReader();
      const chunks = [];
      let size = 0;
      let pending = new Uint8Array();
      let at = 0;
      let firstByte;
      while (true) {
        const { done, value } = await reader.read();
        if (request.signal.aborted) throw request.signal.reason;
        if (!done) {
          firstByte ??= performance.now();
          size += value.length;
          if (size > maxBytes) throw new Error("Speech limit exceeded");
          const bytes = new Uint8Array(pending.length + value.length);
          bytes.set(pending);
          bytes.set(value, pending.length);
          pending = bytes;
        }
        if (done || pending.length >= (played ? sampleRate / 5 : startupBytes)) {
          const even = pending.length - pending.length % 2;
          if (even) {
            const bytes = pending.subarray(0, even);
            try {
              const buffered = performance.now();
              if (!played && ready) await ready;
              if (request.signal.aborted) throw request.signal.reason;
              if (context.state !== "running") return "playback_unavailable";
              const decoding = performance.now();
              const startAt = Math.max(at, context.currentTime + (played ? 0.04 : 0.02));
              at = schedule(bytes, startAt);
              if (!played && replyReceived !== undefined) {
                const scheduled = performance.now();
                const provider = response.headers.get("server-timing")?.match(/^tts_first_byte;dur=([\d.]+)$/);
                console.info("clinic_speech_timing", {
                  processing_ms: Number(data.timings?.processing_ms) || undefined,
                  speech_ticket_ms: Number(data.timings?.speech_ticket_ms) || undefined,
                  reply_to_request_ms: requested - replyReceived,
                  provider_first_byte_ms: provider ? Number(provider[1]) : undefined,
                  request_to_first_pcm_ms: firstByte - requested,
                  first_pcm_to_buffer_ms: buffered - firstByte,
                  buffer_to_decode_ms: decoding - buffered,
                  decode_and_schedule_ms: scheduled - decoding,
                  reply_to_scheduled_start_ms: scheduled - replyReceived + Math.max(0, startAt - context.currentTime) * 1000,
                });
              }
            } catch {
              if (request.signal.aborted) throw request.signal.reason;
              return played ? "interrupted" : "playback_unavailable";
            }
            chunks.push(bytes);
            played = true;
          }
          pending = pending.slice(even);
        }
        if (done) break;
      }
      if (!played || pending.length) throw new Error("Incomplete audio");
      const bytes = new Uint8Array(size);
      let offset = 0;
      for (const chunk of chunks) {
        bytes.set(chunk, offset);
        offset += chunk.length;
      }
      cached = { data, bytes };
      return "played";
    } catch {
      if (controller !== request) return "cancelled";
      return played ? "interrupted" : "stream_failure";
    } finally {
      clearTimeout(timeout);
      request.abort();
      if (controller === request) controller = null;
    }
  }

  return { unlock, stop, play };
}
