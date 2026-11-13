// The real static/talk/app.js in node:vm against a real Talk server over HTTP (synthetic identity, fake WebRTC/audio).
// Usage: node tests/dialogue/e2e.mjs <app.js> <base url> <origin> <initData> <provider: silent|self-greet|none>
import vm from 'node:vm';
import {readFileSync} from 'node:fs';

const [appPath, base, origin, initData, provider] = process.argv.slice(2);
const source = readFileSync(appPath, 'utf8');
const sent = [], log = [];
let outputLevel = 0, peer = null;
const t0 = performance.now();
const note = what => log.push([Math.round(performance.now() - t0), what]);
const els = new Map();
const element = id => {
  if (!els.has(id)) els.set(id, {id, hidden: false, textContent: '', disabled: false, dataset: {}, style: {setProperty() {}},
    setAttribute(n, v) { this[n] = v; }, getAttribute(n) { return this[n]; }, removeAttribute() {}, querySelector() { return this; },
    addEventListener() {}, removeEventListener() {}, getContext() { return new Proxy({}, {get: () => () => {}}); },
    paused: true, muted: false, volume: 1, srcObject: null, focus() {},
    play() { this.paused = false; return Promise.resolve(); }, pause() { this.paused = true; }});
  return els.get(id);
};
class Track { constructor(remote) { this.kind = 'audio'; this.enabled = true; this.readyState = 'live'; this.remote = remote; } stop() { this.readyState = 'ended'; } }
class MediaStream { constructor(tracks = []) { this.tracks = tracks; } getTracks() { return this.tracks; } getAudioTracks() { return this.tracks; } }
const local = new MediaStream([new Track(false)]);
class AudioContext {
  constructor() { this.state = 'suspended'; }
  resume() { this.state = 'running'; this.onstatechange?.(); return Promise.resolve(); }
  close() { this.state = 'closed'; return Promise.resolve(); }
  createMediaStreamSource(stream) { return {connect(a) { a.remote = stream.tracks[0]?.remote; }, disconnect() {}}; }
  createAnalyser() { return {fftSize: 512, connect() {}, disconnect() {}, getFloatTimeDomainData(a) { a.fill(this.remote ? outputLevel : 0); }}; }
}
function emit(event) { if (peer?.channel.readyState === 'open') peer.channel.onmessage?.({data: JSON.stringify(event)}); }
function greet(text) {
  note('greeting spoken'); outputLevel = 0.05;
  emit({type: 'output_transcript.added', item: {text: text.slice(0, 8)}});
  setTimeout(() => { emit({type: 'turn.done', turn: {id: 'opening-' + log.length, role: 'assistant', transcript: text}}); outputLevel = 0; }, 700);
}
const GREETING = 'Привіт! Я на зв’язку. Слухаю.';
class RTCPeerConnection {
  constructor() { peer = this; this.connectionState = 'new'; this.iceGatheringState = 'complete'; }
  addTrack() {} addEventListener() {} removeEventListener() {}
  createDataChannel() {
    this.channel = {readyState: 'connecting', send(m) {
      if (this.readyState !== 'open') throw new Error('InvalidStateError');
      const message = JSON.parse(m); sent.push([Math.round(performance.now() - t0), message]);
      if (message.type === 'session.context.append' && message.channel === 'speakable' && /opening greeting/.test(message.content?.[0]?.text || '')) {
        note('opening requested'); if (provider !== 'none') setTimeout(() => greet(GREETING), 300);
      } }, close() { this.readyState = 'closed'; }};
    return this.channel;
  }
  async createOffer() { return {type: 'offer', sdp: 'v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\ns=offer-marker\r\n'}; }
  async setLocalDescription(d) { this.localDescription = d; }
  async setRemoteDescription() {
    setTimeout(() => {
      this.connectionState = 'connected';
      const remote = new MediaStream([new Track(true)]);
      this.ontrack?.({streams: [remote], track: remote.tracks[0]});
      this.onconnectionstatechange?.();
      this.channel.readyState = 'open'; this.channel.onopen?.();
      setTimeout(() => { emit({type: 'session.started', session: {id: 'synthetic'}}); note('session.started');
        if (provider === 'self-greet') setTimeout(() => greet(GREETING), 200); }, 100);
    }, 50);
  }
  close() { this.connectionState = 'closed'; }
}
const ctx = vm.createContext({
  document: {getElementById: element, documentElement: element('html')},
  window: {addEventListener() {}, removeEventListener() {}},
  location: {hash: '#tgWebAppData=' + encodeURIComponent(initData), search: '', href: base, origin: new URL(base).origin},
  navigator: {mediaDevices: {getUserMedia: async () => local}},
  URL, URLSearchParams, AbortController, DOMException, TextEncoder, crypto: globalThis.crypto, performance,
  MediaStream, RTCPeerConnection, AudioContext, setTimeout, clearTimeout,
  requestAnimationFrame: fn => setTimeout(fn, 16), cancelAnimationFrame: id => clearTimeout(id),
  matchMedia: () => ({matches: false}), getComputedStyle: () => ({color: '#ffffff'}),
  fetch: (url, options = {}) => fetch(new URL(url, base), {...options, headers: {...(options.headers || {}), Origin: origin}}),
});
vm.runInContext(source, ctx, {filename: appPath});
const wait = ms => new Promise(r => setTimeout(r, ms));
const until = async (fn, what, ms = 20000) => { const end = Date.now() + ms; while (!fn()) { if (Date.now() > end) throw new Error('timeout: ' + what); await wait(25); } };
const run = code => vm.runInContext(code, ctx);
let error = null;
try {
  await until(() => run('call?.phase') === 'ready', 'call ready');
  note('ready');
  assert(local.tracks[0].enabled, 'microphone open once the call is ready');
  await wait(4500);
  emit({type: 'input_transcript.added', item: {text: 'Синтетичне '}});
  emit({type: 'turn.done', turn: {id: 'u1', role: 'user', transcript: 'Синтетичне питання.'}});
  emit({type: 'output_transcript.added', item: {text: 'Синтетична '}}); outputLevel = 0.05;
  emit({type: 'turn.done', turn: {id: 'a1', role: 'assistant', transcript: 'Синтетична відповідь.'}}); outputLevel = 0;
  await wait(300);
  run('endCall()'); note('end');
  await wait(1500);
} catch (e) { error = e.message; }
function assert(ok, message) { if (!ok) throw new Error(message); }
console.log('DIALOGUE_E2E ' + JSON.stringify({provider, error, log, readiness: ctx.talkReadiness ?? ctx.globalThis?.talkReadiness ?? null,
  openingRequests: log.filter(([, w]) => w === 'opening requested').length,
  greetingsSpoken: log.filter(([, w]) => w === 'greeting spoken').length,
  sent: sent.map(([t, m]) => [t, m.type, m.channel, m.delegation_item_id || null, (m.content?.[0]?.text || '').slice(0, 60)])}));
process.exit(0);
