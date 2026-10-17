// Stateful low-pass/decimation to mono 16 kHz, in 100 ms PCM16LE packets.
// The area average attenuates high frequencies before decimation; production DSP
// quality and echo performance still require device testing.
class Capture extends AudioWorkletProcessor {
  constructor() {
    super(); this.ratio = sampleRate / 16000; this.fill = 0; this.sum = 0;
    this.packet = new ArrayBuffer(3200); this.view = new DataView(this.packet); this.offset = 0;
  }
  process(inputs) {
    const channels = inputs[0];
    if (!channels?.length) return true;
    for (let i = 0; i < channels[0].length; i++) {
      let sample = 0;
      for (const channel of channels) sample += channel[i] / channels.length;
      let remaining = 1;
      while (remaining > 1e-8) {
        const take = Math.min(remaining, this.ratio - this.fill);
        this.sum += sample * take; this.fill += take; remaining -= take;
        if (this.fill >= this.ratio - 1e-8) {
          const x = Math.max(-1, Math.min(1, this.sum / this.ratio));
          this.view.setInt16(this.offset, Math.round(x * (x < 0 ? 32768 : 32767)), true);
          this.offset += 2; this.fill = 0; this.sum = 0;
          if (this.offset === 3200) {
            this.port.postMessage(this.packet, [this.packet]);
            this.packet = new ArrayBuffer(3200); this.view = new DataView(this.packet); this.offset = 0;
          }
        }
      }
    }
    return true;
  }
}
registerProcessor('capture', Capture);
