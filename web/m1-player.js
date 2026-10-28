/** Small M1 test receiver. Each instance owns one decoder, canvas and audio clock. */
export function createM1Player({canvas, videoUrl, audioUrl, onStatus = () => {}, isCurrent = () => true}) {
  let videoSocket, audioSocket, decoder, audioContext;
  let codec, generation = 0, decoderGeneration = 0, receivedBytes = 0, stopped = false, waitingKey = true, decodedFrames = 0;
  let audioEpoch, audioNextFrame, audioNextTime = 0, audioFrames = 0;
  const playing = new Set();
  const context = canvas.getContext('2d', {alpha: false});

  function requestKeyframe() {
    waitingKey = true;
    if (videoSocket?.readyState === WebSocket.OPEN) {
      videoSocket.send(JSON.stringify({type: 'keyframe'}));
    }
  }

  function configureDecoder() {
    if (decoder && decoder.state !== 'closed') decoder.close();
    const outputGeneration = ++decoderGeneration;
    decoder = new VideoDecoder({
      output(frame) {
        try {
          if (stopped || !isCurrent() || outputGeneration !== decoderGeneration) return;
          context.drawImage(frame, 0, 0, canvas.width, canvas.height);
          decodedFrames++;
          if (decodedFrames % 30 === 0 && videoSocket?.readyState === WebSocket.OPEN) {
            videoSocket.send(JSON.stringify({type: 'decoded', frames: decodedFrames, timestampUs: frame.timestamp, generation}));
          }
          onStatus({type: 'video-frame', decodedFrames, timestamp: frame.timestamp});
        } finally { frame.close(); }
      },
      error(error) {
        onStatus({type: 'video-error', message: error.message});
        if (!stopped) requestKeyframe();
      },
    });
    decoder.configure({codec, optimizeForLatency: true, hardwareAcceleration: 'prefer-hardware'});
    waitingKey = true;
  }

  function start() {
    if (!globalThis.VideoDecoder) throw new Error('This browser does not support WebCodecs VideoDecoder');
    if (videoSocket && videoSocket.readyState < WebSocket.CLOSING) return;
    stopped = false;
    videoSocket = new WebSocket(videoUrl);
    videoSocket.binaryType = 'arraybuffer';
    videoSocket.onmessage = (event) => {
      if (stopped || !isCurrent()) return;
      if (typeof event.data === 'string') {
        const value = JSON.parse(event.data);
        if (value.type === 'video') {
          generation = value.generation ?? 0;
          codec = value.codec;
          canvas.width = value.width; canvas.height = value.height;
          configureDecoder();
        }
        onStatus(value);
        return;
      }
      const data = event.data;
      receivedBytes += data.byteLength;
      if (!decoder || data.byteLength < 10) return;
      const view = new DataView(data), key = view.getUint8(8) === 1;
      if (decoder.state !== 'configured') {
        if (!key) return;
        configureDecoder();
      }
      if (decoder.decodeQueueSize > 6) {
        configureDecoder(); requestKeyframe();
      }
      if (waitingKey && !key) return;
      waitingKey = false;
      try {
        decoder.decode(new EncodedVideoChunk({type: key ? 'key' : 'delta',
          timestamp: Number(view.getBigUint64(0)), data: new Uint8Array(data, 9)}));
      } catch (error) {
        onStatus({type: 'video-error', message: error.message});
        configureDecoder(); requestKeyframe();
      }
    };
    videoSocket.onclose = () => onStatus({type: 'video-closed'});
    videoSocket.onerror = () => onStatus({type: 'video-error', message: 'Video connection failed'});
  }

  async function startAudio() {
    if (!audioUrl) return;
    if (!audioContext) audioContext = new AudioContext({sampleRate: 48000, latencyHint: 'interactive'});
    await audioContext.resume();
    if (stopped || !audioContext || !isCurrent()) return;
    if (audioSocket && audioSocket.readyState < WebSocket.CLOSING) return;
    audioSocket = new WebSocket(audioUrl);
    audioSocket.binaryType = 'arraybuffer';
    audioSocket.onmessage = (event) => {
      if (stopped || !audioContext || !isCurrent()) return;
      if (typeof event.data === 'string') { onStatus(JSON.parse(event.data)); return; }
      const data = event.data;
      if (data.byteLength < 16 || (data.byteLength - 12) % 4) return;
      const view = new DataView(data), epoch = view.getUint32(0);
      const startFrame = Number(view.getBigUint64(4)), frames = (data.byteLength - 12) / 4;
      if (epoch !== audioEpoch || startFrame !== audioNextFrame ||
          audioNextTime < audioContext.currentTime || audioNextTime > audioContext.currentTime + .3) {
        for (const source of playing) { try { source.stop(); } catch {} }
        playing.clear();
        audioNextTime = audioContext.currentTime + .04;
      }
      audioEpoch = epoch; audioNextFrame = startFrame + frames;
      const buffer = audioContext.createBuffer(2, frames, 48000);
      for (let channel = 0; channel < 2; channel++) {
        const output = buffer.getChannelData(channel);
        for (let i = 0; i < frames; i++) output[i] = view.getInt16(12 + (i * 2 + channel) * 2, true) / 32768;
      }
      const source = audioContext.createBufferSource();
      source.buffer = buffer; source.connect(audioContext.destination);
      source.onended = () => { playing.delete(source); source.disconnect(); };
      playing.add(source); source.start(audioNextTime);
      audioNextTime += frames / 48000; audioFrames += frames;
      onStatus({type: 'audio-frames', audioFrames, epoch, startFrame, frames});
    };
    audioSocket.onclose = () => onStatus({type: 'audio-closed'});
    audioSocket.onerror = () => onStatus({type: 'audio-error', message: 'Audio connection failed'});
  }

  function stop() {
    stopped = true;
    videoSocket?.close(); audioSocket?.close();
    if (decoder?.state !== 'closed') decoder?.close();
    for (const source of playing) { try { source.stop(); } catch {} }
    playing.clear();
    audioContext?.close(); audioContext = undefined;
  }

  return {start, startAudio, stop, snapshot: () => ({decodedFrames, receivedBytes, audioFrames, audioEpoch,
    videoState: videoSocket?.readyState, audioState: audioSocket?.readyState})};
}
