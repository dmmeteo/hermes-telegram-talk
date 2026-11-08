// Startup lifecycle gate: runs the real static/talk/app.js in node:vm with a virtual clock and fake
// fetch/getUserMedia/RTCPeerConnection/AudioContext (synthetic identity, no network) and asserts which calls become
// usable, which genuinely fail and with which unmet gate, and that every exit releases its resources.
// Usage: node tests/startup/lifecycle.mjs <path/to/static/talk/app.js> [--report]
// Virtual-clock times are fixture times, not device latency.
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';

const appPath = process.argv[2];
const reportOnly = process.argv.includes('--report');
const source = readFileSync(appPath, 'utf8');
const sha = createHash('sha256').update(source).digest('hex');

function harness(sc) {
  let now = 0, nextId = 1;
  const timers = new Map();            // id -> {at, fn, kind}
  const log = [];                      // [t, what]
  const requests = [];                 // {route, t, body, aborted, status}
  const res = {audio: [], peers: [], channels: [], localTracks: [], remoteTracks: [], nodes: []};
  const note = (what) => log.push([now, what]);
  const setT = (fn, ms = 0, kind = 'timeout') => { const id = nextId++; timers.set(id, {at: now + Math.max(0, ms), fn, kind}); return id; };
  const clearT = (id) => { timers.delete(id); };

  const els = new Map();
  function element(id) {
    if (!els.has(id)) els.set(id, {id, hidden: false, textContent: '', disabled: false, dataset: {}, style: {setProperty() {}},
      setAttribute(n, v) { this[n] = v; }, removeAttribute() {}, querySelector() { return this; },
      addEventListener() {}, removeEventListener() {}, getContext() { return new Proxy({}, {get: () => () => {}}); },
      paused: true, srcObject: null,
      play() { this.paused = !!sc.playRejects; return sc.playRejects ? Promise.reject(new Error('NotAllowedError')) : Promise.resolve(); },
      pause() { this.paused = true; }});
    return els.get(id);
  }
  const winListeners = {};
  class Track { constructor(kind, remote) { this.kind = kind; this.enabled = true; this.stopped = false; this.remote = remote;
    (remote ? res.remoteTracks : res.localTracks).push(this); } stop() { this.stopped = true; } }
  class MediaStream { constructor(tracks = []) { this.tracks = tracks; } getTracks() { return this.tracks; }
    getAudioTracks() { return this.tracks.filter(t => t.kind === 'audio'); } }
  class AudioContext {
    constructor() { this.state = 'suspended'; this.onstatechange = null; res.audio.push(this); }
    resume() { if (sc.audioNeedsGesture) return new Promise(() => {}); this.state = 'running'; return Promise.resolve(); }
    close() { this.state = 'closed'; this.closed = true; return Promise.resolve(); }
    createMediaStreamSource(stream) { const n = {stream, connect(a) { a.stream = stream; }, disconnect() { this.off = true; }}; res.nodes.push(n); return n; }
    createAnalyser() {
      const n = {fftSize: 2048, stream: null, connect() {}, disconnect() { this.off = true; },
        getFloatTimeDomainData(a) { const remote = this.stream?.tracks?.[0]?.remote; a.fill(remote ? sc.outputLevel(now) : 0); }};
      res.nodes.push(n); return n;
    }
  }
  class Peer {
    constructor() { this.connectionState = 'new'; this.iceGatheringState = 'new'; this.closed = false; res.peers.push(this); this.l = {}; }
    addTrack() {}
    addEventListener(t, f) { this.l[t] = f; } removeEventListener(t) { delete this.l[t]; }
    createDataChannel() {
      const peer = this;
      const ch = {readyState: 'connecting', closed: false, sent: [], onopen: null, onclose: null, onmessage: null,
        send(m) { if (this.readyState !== 'open') throw new Error('InvalidStateError'); this.sent.push(JSON.parse(m)); sent.push([now, JSON.parse(m)]);
          const msg = JSON.parse(m);
          if (/opening greeting/.test(msg.content?.[0]?.text || '')) { note('greeting requested'); peer.greet(); } },
        close() { this.readyState = 'closed'; this.closed = true; }};
      this.channel = ch; res.channels.push(ch); return ch;
    }
    async createOffer() { return {type: 'offer', sdp: 'v=0\r\no=- synthetic offer'}; }
    async setLocalDescription(d) { this.localDescription = d; this.iceGatheringState = 'complete'; }
    async setRemoteDescription() {
      if (sc.neverConnect) return;
      setT(() => { if (this.closed) return;
        this.connectionState = 'connected'; note('peer connected');
        const remote = new MediaStream([new Track('audio', true)]);
        this.ontrack?.({streams: [remote], track: remote.tracks[0]});
        this.onconnectionstatechange?.();
        this.channel.readyState = 'open'; this.channel.onopen?.();
      }, sc.connectMs ?? 500, 'fixture');
      if (!sc.noHandshake) setT(() => { if (!this.closed) this.emit({type: 'session.started'}); }, (sc.connectMs ?? 500) + (sc.handshakeMs ?? 300), 'fixture');
    }
    emit(ev) { if (this.channel.readyState === 'open') this.channel.onmessage?.({data: JSON.stringify(ev)}); }
    greet() {
      const t0 = now;
      sc.greetStart = t0;
      if (sc.greetingTurnDoneMs != null) setT(() => { if (!this.closed) { note('assistant turn.done'); this.emit({type: 'turn.done', turn: {id: 'g1', role: 'assistant', transcript: 'Synthetic greeting.'}}); } }, sc.greetingTurnDoneMs, 'fixture');
    }
    close() { this.closed = true; this.connectionState = 'closed'; }
  }
  const sent = [];
  async function fetch(url, opts = {}) {
    const route = String(url).replace(/^api\//, '');
    const body = JSON.parse(opts.body || '{}');
    const r = {route, t: now, body, aborted: false, status: null, keepalive: !!opts.keepalive};
    requests.push(r);
    const delay = (sc.delays || {})[route] ?? 50;
    return new Promise((resolve, reject) => {
      const id = setT(() => {
        const data = respond(route, body); r.status = data.status; r.done = now;
        resolve({ok: data.status < 300, status: data.status, json: async () => data.body});
      }, delay, 'fetch');
      opts.signal?.addEventListener('abort', () => { if (r.done != null) return; clearT(id); r.aborted = true; r.done = now; note(`abort ${route}`);
        reject(new DOMException('Aborted', 'AbortError')); }, {once: true});
    });
  }
  let voiceN = 0;
  function respond(route, body) {
    if (route === 'bootstrap') return {status: 200, body: {conversation_id: 'c1', limits: {event_batch_max: 16}, greeting_enabled: sc.greeting ?? true,
      profile: {display_name: 'Synthetic', avatar_url: '/tg-voice/avatar'}, appearance: {listen_color: '#00ff00', speak_color: '#0000ff'}, tasks: {available: sc.tasks ?? true}}};
    if (route === 'session') return {status: 200, body: {conversation_id: 'c1', voice_session_id: `v${++voiceN}`, sdp: 'v=0\r\nanswer', type: 'answer'}};
    if (route === 'events') return {status: 200, body: {conversation_id: 'c1', accepted_ids: body.events.map(e => e.id)}};
    if (route === 'tasks') return {status: 200, body: {jobs: [], context_pending: false}};
    if (route === 'context') {
      if (sc.lateContextAt != null && now >= sc.lateContextAt && !sc.contextAcked) return {status: 200, body: {revision: 1,
        text: '## Context update 1\n- P1 [failed] Synthetic late job (cli session s1, 2026-01-01 00:00 UTC)', sources: {jobs: 0, activity: 1}}};
      return {status: 200, body: {revision: null, text: '', sources: {jobs: 0, activity: 0}}};
    }
    if (route === 'context/ack') { sc.contextAcked = true; return {status: 200, body: {ok: true}}; }
    return {status: 200, body: {ended: true}};
  }
  const navigator = {mediaDevices: {getUserMedia: () => new Promise((resolve, reject) => setT(() => {
    if (sc.micDenied) { const e = new Error('denied'); e.name = 'NotAllowedError'; reject(e); return; }
    resolve(new MediaStream([new Track('audio', false)])); }, sc.gumMs ?? 200, 'gum'))}};
  const ctx = vm.createContext({
    document: {getElementById: element, documentElement: element('html')},
    window: {addEventListener(t, f) { winListeners[t] = f; }, removeEventListener(t) { delete winListeners[t]; }},
    location: {hash: '#tgWebAppData=synthetic', href: 'http://127.0.0.1/tg-voice/', origin: 'http://127.0.0.1'},
    URL, URLSearchParams, AbortController, DOMException, TextEncoder, crypto: globalThis.crypto, console,
    matchMedia: () => ({matches: false}), getComputedStyle: () => ({color: '#ffffff'}),
    setTimeout: (f, ms) => setT(f, ms), clearTimeout: clearT,
    requestAnimationFrame: (f) => setT(f, 16, 'raf'), cancelAnimationFrame: clearT,
    performance: {now: () => now}, fetch, navigator, RTCPeerConnection: Peer, AudioContext, MediaStream,
  });
  vm.runInContext(source, ctx, {filename: appPath});  // the real app.js; its last line calls start()
  async function settle() { for (let i = 0; i < 6; i++) await new Promise(r => setImmediate(r)); }
  async function runUntil(t) {
    for (;;) {
      await settle();
      let best = null;
      for (const [id, tm] of timers) if (tm.at <= t && (!best || tm.at < best[1].at || (tm.at === best[1].at && id < best[0]))) best = [id, tm];
      if (!best) { now = t; await settle(); return; }
      timers.delete(best[0]); now = best[1].at; best[1].fn();
    }
  }
  return {ctx, els, res, requests, log, timers, winListeners, runUntil, sent, get now() { return now; }, run: code => vm.runInContext(code, ctx)};
}

function snapshot(h) {
  const live = [...h.timers.values()];
  return {
    audioContextsOpen: h.res.audio.filter(a => !a.closed).length, audioContexts: h.res.audio.length,
    peersOpen: h.res.peers.filter(p => !p.closed).length, peers: h.res.peers.length,
    channelsOpen: h.res.channels.filter(c => !c.closed).length,
    localTracksLive: h.res.localTracks.filter(t => !t.stopped).length, localTracks: h.res.localTracks.length,
    remoteTracksLive: h.res.remoteTracks.filter(t => !t.stopped).length,
    nodesConnected: h.res.nodes.filter(n => !n.off).length,
    pendingAppTimers: live.filter(t => t.kind === 'timeout').length, pendingRaf: live.filter(t => t.kind === 'raf').length,
    pendingFetch: live.filter(t => t.kind === 'fetch').length,
  };
}

const base = {delays: {bootstrap: 300, session: 2500, end: 120, events: 80, tasks: 60, context: 60}, gumMs: 200, connectMs: 500, handshakeMs: 300};
// Greeting audio is heard from 1 s after the greeting request until `audibleUntil` ms after it.
const greetingAudio = (h, from, until) => (t) => (h.sc.greetStart != null && t >= h.sc.greetStart + from && t < h.sc.greetStart + until) ? 0.05 : 0;

const SCENARIOS = {
  'short-greeting': {greet: [1000, 4000], greetingTurnDoneMs: 4200, expect: {usable: true}},
  'long-greeting': {greet: [1000, 40000], greetingTurnDoneMs: 40000, expect: {usable: true}},
  'greeting-done-not-heard': {greet: null, greetingTurnDoneMs: 4000, expect: {usable: true}},
  'slow-session-normal-greeting': {delays: {session: 22000}, greet: [1000, 9000], greetingTurnDoneMs: 9000, expect: {usable: true}},
  'no-greeting': {greeting: false, greet: null, expect: {usable: true}},
  'session-past-deadline': {delays: {session: 31000}, greet: [1000, 4000], greetingTurnDoneMs: 4200,
    expect: {usable: false, failure: 'connect:session', end: false}},
  'no-handshake': {noHandshake: true, greet: null, expect: {usable: false, failure: 'connect:handshake', end: true}},
  'never-connects': {neverConnect: true, greet: null, expect: {usable: false, failure: 'connect:peer', end: true}},
  'audio-needs-gesture': {audioNeedsGesture: true, greet: [1000, 4000], greetingTurnDoneMs: 4200,
    expect: {usable: false, failure: 'connect:audio', end: true}},
  'greeting-suspended-then-unlocked-silent': {greet: null, actions: [[5000, 'call.audio.state = "suspended"; call.audio.onstatechange()'],
    [20000, 'call.audio.state = "running"; void enableAudio(call)']], expect: {usable: true}},
  'late-context-no-tasks': {tasks: false, lateContextAt: 9000, greet: [1000, 4000], greetingTurnDoneMs: 4200,
    expect: {usable: true, lateContext: true}},
};

const failures = [];
const check = (ok, what) => { if (!ok) failures.push(what); };
const released = (s) => s.audioContextsOpen === 0 && s.peersOpen === 0 && s.channelsOpen === 0 && s.localTracksLive === 0
  && s.remoteTracksLive === 0 && s.nodesConnected === 0 && s.pendingAppTimers === 0 && s.pendingRaf === 0;

async function runScenario(name) {
  const def = SCENARIOS[name];
  const sc = {...base, ...def, delays: {...base.delays, ...(def.delays || {})}};
  const h = harness(sc); h.sc = sc;
  sc.outputLevel = def.greet ? greetingAudio(h, def.greet[0], def.greet[1]) : () => 0;
  for (const [at, code] of def.actions || []) { await h.runUntil(at); h.run(code); }
  await h.runUntil(45000);
  const phase = h.run('call ? call.phase : "idle"');
  const error = h.els.get('error');
  const end = h.requests.find(r => r.route === 'end');
  const readiness = h.run('globalThis.talkReadiness || null');
  const greetings = h.sent.filter(([, m]) => /opening greeting/.test(m.content?.[0]?.text || '')).length;
  const appended = h.sent.filter(([, m]) => /Synthetic late job/.test(m.content?.[0]?.text || '')).map(([t]) => t);
  const result = {scenario: name, phaseAt45s: phase, errorShown: error.hidden ? '' : error.textContent,
    endRequestAt: end ? end.t : null, endReadiness: end?.body.readiness ?? null, readiness,
    sessionRequests: h.requests.filter(r => r.route === 'session').length, greetingRequests: greetings,
    lateContextAppendedAt: appended, contextAcks: h.requests.filter(r => r.route === 'context/ack').map(r => r.body.revision),
    resourcesAt45s: snapshot(h)};
  if (phase !== 'idle') {
    h.winListeners.pagehide?.({persisted: false}); await h.runUntil(60000); result.afterPagehide = snapshot(h);
    result.endAfterPagehide = h.requests.filter(r => r.route === 'end').map(r => ({t: r.t, failure: r.body.readiness?.failure}));
  }
  const e = def.expect;
  if (e.usable) {
    check(phase === 'ready', `${name}: phase at 45 s is ${phase}, not ready`);
    check(!result.errorShown && result.endRequestAt === null, `${name}: a usable call was ended (${result.errorShown})`);
    check(readiness?.stages?.usable !== undefined && readiness.stages.usable < 30000, `${name}: no usable stage before the deadline`);
    check(result.sessionRequests === 1 && greetings === (def.greeting === false ? 0 : 1), `${name}: reconnected or re-greeted`);
    check(released(result.afterPagehide || {}), `${name}: pagehide left resources ${JSON.stringify(result.afterPagehide)}`);
  } else {
    check(phase === 'idle' && /did not become ready/.test(result.errorShown), `${name}: a genuine failure was not reported`);
    check(readiness?.failure === e.failure, `${name}: failure ${readiness?.failure}, expected ${e.failure}`);
    check(readiness?.stages?.usable === undefined, `${name}: marked usable`);
    check(e.end ? end?.t === 30000 && end.body.readiness?.failure === e.failure : end === undefined,
      `${name}: /api/end ${JSON.stringify(end ?? null)}`);
    check(released(result.resourcesAt45s), `${name}: failure left resources ${JSON.stringify(result.resourcesAt45s)}`);
  }
  if (e.lateContext) {
    check(appended.length === 1 && appended[0] >= 9000, `${name}: late context appended at ${appended}`);
    check(JSON.stringify(result.contextAcks) === '[1]', `${name}: context acks ${result.contextAcks}`);
  }
  return result;
}

async function retryLoop() {
  // Five attempts that never get a handshake, each retried: only the newest attempt may hold anything.
  const def = SCENARIOS['no-handshake'];
  const sc = {...base, ...def, delays: {...base.delays}};
  const h = harness(sc); h.sc = sc; sc.outputLevel = () => 0;
  const perAttempt = [];
  let t = 0;
  for (let i = 0; i < 5; i++) {
    t += 32000; await h.runUntil(t);
    perAttempt.push({attempt: i + 1, phase: h.run('call ? call.phase : "idle"'), ...snapshot(h)});
    h.run('start()');
  }
  await h.runUntil(t + 32000);
  const final = snapshot(h);
  const ends = h.requests.filter(r => r.route === 'end');
  check(perAttempt.every(a => a.phase === 'idle' && released(a)), `retry-loop: an attempt kept resources`);
  check(released(final) && ends.length === 6 && new Set(ends.map(r => r.body.voice_session_id)).size === 6,
    `retry-loop: ${ends.length} ends, final ${JSON.stringify(final)}`);
  return {scenario: 'retry-loop-6x-no-handshake', perAttempt, final, endRequests: ends.map(r => [r.t, r.body.readiness?.failure])};
}

async function interruptedSetup() {
  const out = [];
  for (const [label, at] of [['during-bootstrap', 100], ['during-getUserMedia', 400], ['during-session', 1500],
    ['during-greeting', 5000], ['ready', 9000]]) {
    const def = SCENARIOS['short-greeting'];
    const sc = {...base, ...def, delays: {...base.delays}};
    const h = harness(sc); h.sc = sc; sc.outputLevel = greetingAudio(h, 1000, 4000);
    await h.runUntil(at);
    h.winListeners.pagehide?.({persisted: false});
    await h.runUntil(40000);
    const snap = snapshot(h);
    const acks = h.requests.filter(r => r.route === 'context/ack').length;
    check(released(snap) && acks === 0, `interrupted ${label}: ${JSON.stringify(snap)} acks ${acks}`);
    out.push({stage: label, at, ...snap, endRequests: h.requests.filter(r => r.route === 'end').map(r => r.t)});
  }
  return {scenario: 'interrupted-setup-pagehide', stages: out};
}

console.log(JSON.stringify({app_js: appPath, app_js_sha256: sha}));
for (const name of [...Object.keys(SCENARIOS), 'retry-loop', 'interrupted-setup']) {
  const r = name === 'retry-loop' ? await retryLoop() : name === 'interrupted-setup' ? await interruptedSetup() : await runScenario(name);
  console.log(JSON.stringify(r));
}
for (const f of failures) console.log('FAIL ' + f);
console.log(failures.length ? `TALK_STARTUP_LIFECYCLE_FAILED ${failures.length}` : 'TALK_STARTUP_LIFECYCLE_OK');
process.exit(failures.length && !reportOnly ? 1 : 0);
