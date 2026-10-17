const el = id => document.getElementById(id);
let current = null, generation = 0, config = null;
let assistant = null;
let device = null, bindingBusy = false;
function status(title, detail = '', state) {
  el('status').textContent = title; el('detail').textContent = detail;
  if (state) document.body.dataset.callState = state;
}
function buttons(busy, ready = false) {
  el('start').disabled = busy; el('stop').disabled = !busy; el('interrupt').disabled = !ready;
  el('start').hidden = busy; el('stop').hidden = !busy; el('mute').disabled = !ready;
  el('stop-label').textContent = ready ? '挂断' : '取消';
  el('stop').setAttribute('aria-label', ready ? '挂断语音通话' : '取消连接');
  document.body.dataset.callState = ready ? 'listening' : busy ? 'connecting' : 'idle';
  el('device-bind').disabled = busy || bindingBusy || !device?.configured;
  if (!busy) {
    el('mute').setAttribute('aria-pressed', 'false'); el('mute-label').textContent = '静音';
  }
}
function duration(s) {
  if (s.startedAt === undefined) return;
  const seconds = Math.max(0, Math.floor((performance.now() - s.startedAt) / 1000));
  el('duration').textContent = `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
  el('duration').setAttribute('datetime', `PT${seconds}S`);
}
function emptyConversation() {
  el('transcripts').replaceChildren(el('empty-transcripts').content.cloneNode(true));
}
function flush(s) {
  for (const source of s.playing) { source.onended = null; try { source.stop(); } catch {} source.disconnect(); }
  s.playing.clear(); s.nextTime = 0;
}
function dispose(s) {
  if (!s || s.disposed) return;
  s.disposed = true; clearTimeout(s.timer); clearInterval(s.clock); duration(s); s.abort.abort(); flush(s);
  if (s.capture) { s.capture.port.onmessage = null; s.capture.disconnect(); }
  s.source?.disconnect(); s.silent?.disconnect();
  s.stream?.getTracks().forEach(track => track.stop());
  if (s.socket) { s.socket.onclose = s.socket.onerror = s.socket.onmessage = null; s.socket.close(); }
  if (s.context && s.context.state !== 'closed') s.context.close().catch(() => {});
}
function finish(s, title, detail) {
  dispose(s);
  if (current === s) {
    current = null; buttons(false); status(title, detail);
    el('start-label').textContent = '重新接通'; el('start').setAttribute('aria-label', '重新接通语音通话');
  }
}
function transcript(role, text) {
  if (!text) return;
  el('transcripts').querySelector('.empty')?.remove();
  let target;
  if (role === 'assistant' && assistant) target = assistant;
  else {
    const row = document.createElement('div'); row.className = `utterance ${role}`;
    const label = document.createElement('small'); label.textContent = role === 'user' ? '你' : '助手';
    target = document.createElement('span'); row.append(label, target); el('transcripts').append(row);
    if (role === 'assistant') assistant = target;
    while (el('transcripts').children.length > 100) el('transcripts').firstElementChild.remove();
  }
  target.textContent = (target.textContent + text).slice(-32000);
  el('transcripts').scrollTop = el('transcripts').scrollHeight;
}
function play(s, bytes) {
  if (!bytes.byteLength || bytes.byteLength % 2 || s.context.state !== 'running') throw new Error('音频播放不可用，请重新连接。');
  const duration = bytes.byteLength / 48000;
  const at = Math.max(s.context.currentTime + .02, s.nextTime);
  if (at + duration - s.context.currentTime > 8) throw new Error('播放积压过多，请重新连接。');
  const data = new DataView(bytes), buffer = s.context.createBuffer(1, bytes.byteLength / 2, 24000);
  const floats = buffer.getChannelData(0);
  for (let i = 0; i < floats.length; i++) floats[i] = data.getInt16(i * 2, true) / 32768;
  const source = s.context.createBufferSource(); source.buffer = buffer; source.connect(s.context.destination);
  s.playing.add(source); source.onended = () => { s.playing.delete(source); source.disconnect(); };
  source.start(at); s.nextTime = at + buffer.duration;
}
async function start() {
  if (current || bindingBusy) return;
  const s = { id: ++generation, abort: new AbortController(), playing: new Set(), nextTime: 0, ready: false };
  current = s; assistant = null; buttons(true); status('准备连接', '正在检查配置…');
  el('duration').hidden = true; el('duration').textContent = '00:00';
  const stale = () => current !== s || s.disposed;
  s.timer = setTimeout(() => finish(s, '连接超时', '可点击重新连接。'), 30000);
  try {
    // Unlock audio synchronously inside the tap, before fetch/permission awaits.
    // This matters on mobile Safari where transient user activation is short.
    s.context = new AudioContext();
    const unlocked = s.context.resume();
    await unlocked;
    if (stale()) return;
    const response = await fetch('/api/config', { signal: s.abort.signal });
    if (!response.ok) throw new Error('无法读取服务配置。');
    config = await response.json();
    if (stale()) return;
    if (!config.ready) throw new Error(config.message || '服务端尚未准备好。');
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) throw new Error('麦克风需要 HTTPS 或 localhost 安全页面。');
    status('等待麦克风授权', '允许后开启麦克风；也可点击取消。');
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    if (stale()) { stream.getTracks().forEach(track => track.stop()); return; }
    s.stream = stream;
    for (const track of stream.getTracks()) track.onended = () => finish(s, '麦克风已断开', '请检查设备后重新连接。');
    await s.context.audioWorklet.addModule('/capture.js');
    if (stale()) return;
    s.capture = new AudioWorkletNode(s.context, 'capture');
    s.context.onstatechange = () => {
      if (s.ready && !s.disposed && s.context.state !== 'running') finish(s, '音频已暂停', '系统中断了音频，请点击重新连接。');
    };
    s.source = s.context.createMediaStreamSource(stream);
    s.silent = s.context.createGain(); s.silent.gain.value = 0;
    s.source.connect(s.capture); s.capture.connect(s.silent); s.silent.connect(s.context.destination);
    const socket = s.socket = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws`);
    socket.binaryType = 'arraybuffer';
    status('正在建立会话', '等待模型确认音频配置…');
    s.capture.port.onmessage = event => {
      if (stale() || !s.ready || s.muted || socket.readyState !== WebSocket.OPEN) return;
      if (socket.bufferedAmount + event.data.byteLength > 64000) { finish(s, '网络发送积压', '音频已停止，请重新连接。'); return; }
      socket.send(event.data);
    };
    socket.onmessage = event => {
      if (stale()) return;
      try {
        if (event.data instanceof ArrayBuffer) { if (s.ready && !s.suppressed) play(s, event.data); return; }
        const message = JSON.parse(event.data);
        switch (message.type) {
          case 'ready':
            clearTimeout(s.timer); s.ready = true; buttons(true, true);
            s.startedAt = performance.now(); duration(s); el('duration').hidden = false;
            s.clock = setInterval(() => duration(s), 1000);
            status('正在聆听', '直接说话，停顿后我会回应。', 'listening'); break;
          case 'responding': assistant = null; s.suppressed = false; status('正在回复', '随时开口，或轻点打断。', 'responding'); break;
          case 'interrupted': flush(s); s.suppressed = true; assistant = null; status('已打断，继续聆听', '可以继续说话。', 'listening'); break;
          case 'listening': status('正在聆听', '我在听，请继续。', 'listening'); break;
          case 'turn_done': assistant = null; status('继续聆听', '可以继续说话。', 'listening'); break;
          case 'transcript': transcript(message.role, message.text); break;
          case 'notice': el('detail').textContent = message.message; break;
          case 'error': finish(s, '会话错误', message.message); break;
        }
      } catch (error) { finish(s, '音频或协议错误', error.message); }
    };
    socket.onerror = () => finish(s, '连接失败', '请检查网关配置、网络或会话数量后重连。');
    socket.onclose = () => finish(s, '连接已断开', '麦克风已释放，点击重新连接建立新会话。');
  } catch (error) {
    if (stale()) return;
    const permission = error.name === 'NotAllowedError';
    finish(s, permission ? '麦克风权限被拒绝' : '无法开始对话', permission ? '请在浏览器设置允许麦克风后重新连接。' : error.message);
  }
}
el('start').onclick = start;
el('stop').onclick = () => {
  const s = current;
  if (s?.socket?.readyState === WebSocket.OPEN) s.socket.send(JSON.stringify({ type: 'stop' }));
  finish(s, '通话已结束', '麦克风已关闭，随时可以重新接通。');
};
el('mute').onclick = () => {
  const s = current;
  if (!s?.ready || s.disposed) return;
  s.muted = !s.muted;
  s.stream.getAudioTracks().forEach(track => { track.enabled = !s.muted; });
  el('mute').setAttribute('aria-pressed', String(s.muted));
  el('mute-label').textContent = s.muted ? '取消静音' : '静音';
};
el('interrupt').onclick = () => {
  const s = current;
  if (!s?.ready || s.socket.readyState !== WebSocket.OPEN) return;
  flush(s); s.suppressed = true; s.socket.send(JSON.stringify({ type: 'interrupt' }));
};
el('clear').onclick = () => { emptyConversation(); assistant = null; };
el('logout').onclick = async () => {
  if (current) finish(current, '已停止', '正在退出登录。');
  try {
    const response = await fetch('/api/logout', { method: 'POST', signal: AbortSignal.timeout(5000) });
    if (!response.ok) throw new Error('退出失败，请重试。');
    location.replace('/login');
  } catch { status('会话已停止', '网络恢复后请再次退出登录。'); }
};
window.addEventListener('pagehide', () => { if (current) finish(current, '会话已结束', '返回后可点击重新连接。'); });
window.addEventListener('offline', () => { if (current) finish(current, '网络已离线', '网络恢复后可点击重新连接。'); });
document.addEventListener('visibilitychange', () => {
  if (document.hidden && current) finish(current, '页面已进入后台', '麦克风已释放，返回后可重新连接。');
});
function renderDevice() {
  el('device-panel').hidden = !device?.configured;
  document.body.classList.toggle('has-device', Boolean(device?.configured));
  if (!device?.configured) return;
  const kind = device.simulated === true ? '模拟设备 · ' : device.simulated === false ? '真实设备 · ' : '';
  el('device-status').textContent = `${kind}${device.online ? '在线' : '离线'} · ${device.bound ? '已关联，下次接通生效' : '关联后允许助理调用'}`;
  el('device-bind').textContent = device.bound ? '取消关联' : '关联设备';
  el('device-bind').disabled = Boolean(current) || bindingBusy;
}
async function refreshDevice() {
  try {
    const response = await fetch('/api/stackchan', { signal: AbortSignal.timeout(5000) });
    if (!response.ok) return;
    device = await response.json(); renderDevice();
  } catch { if (device?.configured) el('device-status').textContent = '设备状态暂不可用'; }
}
el('device-bind').onclick = async () => {
  if (current || bindingBusy || !device?.configured) return;
  bindingBusy = true; el('start').disabled = true; renderDevice();
  try {
    const response = await fetch('/api/stackchan/bind', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ device_id: device.bound ? null : device.device_id }), signal: AbortSignal.timeout(5000) });
    if (!response.ok) throw new Error();
    device = await response.json();
  } catch { el('detail').textContent = '设备关联未确认，请刷新后重试。'; }
  finally { bindingBusy = false; el('start').disabled = Boolean(current); renderDevice(); }
};
refreshDevice();
window.addEventListener('focus', () => { if (!current && !bindingBusy) refreshDevice(); });
fetch('/api/config').then(r => r.json()).then(c => {
  config = c; el('model').textContent = c.model === 'qwen-audio-3.0-realtime-flash' ? 'Qwen Audio · Flash' : c.model;
  el('config').textContent = c.ready ? '接通与个人助理对话 · 设备能力以实际回执为准' : c.message;
}).catch(() => { el('config').textContent = '无法读取服务配置。'; });
