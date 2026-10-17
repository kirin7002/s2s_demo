// Run with node --test tests/test_capture.js. Executes the actual worklet DSP.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
for (const rate of [16000, 44100, 48000]) {
  test(`PCM sample count, clipping, mono mix and packet continuity at ${rate} Hz`, () => {
    const packets = [];
    let Capture;
    const sandbox = {
      sampleRate: rate, AudioWorkletProcessor: class { constructor() { this.port = { postMessage: packet => packets.push(packet) }; } },
      registerProcessor: (name, cls) => { Capture = cls; },
    };
    vm.runInNewContext(fs.readFileSync('web/capture.js', 'utf8'), sandbox);
    const capture = new Capture();
    for (let offset = 0; offset < rate; offset += 128) {
      const n = Math.min(128, rate - offset);
      capture.process([[new Float32Array(n).fill(2), new Float32Array(n).fill(0)]]);
    }
    assert.equal(packets.length, 10);
    for (const packet of packets) {
      assert.equal(packet.byteLength, 3200);
      const view = new DataView(packet);
      for (let i = 0; i < packet.byteLength; i += 2) assert.equal(view.getInt16(i, true), 32767);
    }
    assert.equal(capture.offset, 0);
  });
}
test('varying signal is identical across irregular capture block boundaries', () => {
  const run = sizes => {
    const packets = []; let Capture;
    const sandbox = {
      sampleRate: 44100,
      AudioWorkletProcessor: class { constructor() { this.port = {postMessage: packet => packets.push(Buffer.from(packet))}; } },
      registerProcessor: (name, cls) => { Capture = cls; },
    };
    vm.runInNewContext(fs.readFileSync('web/capture.js', 'utf8'), sandbox);
    const capture = new Capture();
    const signal = Float32Array.from({length: 13230}, (_, i) => Math.sin(i * .17) * 1.2);
    for (let offset = 0, i = 0; offset < signal.length; i++) {
      const end = Math.min(signal.length, offset + sizes[i % sizes.length]);
      capture.process([[signal.subarray(offset, end)]]); offset = end;
    }
    return Buffer.concat(packets);
  };
  const full = run([13230]), split = run([17, 128, 59, 3, 211]);
  assert.equal(full.length, 9600);
  assert.deepEqual(split, full);
});
