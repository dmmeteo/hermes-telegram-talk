"use strict";

const startButton = document.getElementById("start");
const muteButton = document.getElementById("mute");
const errorBox = document.getElementById("error");
const remoteAudio = document.getElementById("remote");
const orb = document.getElementById("orb");
const canvas = document.getElementById("ring");
const paint = canvas.getContext("2d");
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
const webApp = window.Telegram && window.Telegram.WebApp;
let call = null;
let conversation = null;
let exiting = false;
const APPEND_MAX_BYTES = 500;
const FINISHED = ["succeeded", "failed", "cancelled", "interrupted"];

function initData() {
  return webApp?.initData || new URLSearchParams(location.hash.slice(1)).get("tgWebAppData") || "";
}

function showError(message = "") {
  errorBox.textContent = message;
  errorBox.hidden = !message;
}

async function post(route, body, options = {}) {
  let response;
  try {
    response = await fetch(`api/${route}`, {
      method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body), ...options,
    });
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new Error("Network error. Check your connection, then tap Retry.");
  }
  let data;
  try { data = await response.json(); } catch (_) { data = {}; }
  if (!response.ok) {
    const error = new Error(({401: "Telegram sign-in expired. Close and reopen Hermes Telegram Talk.",
      403: "This Telegram account cannot use Hermes Telegram Talk.",
      404: "This conversation is unavailable. Close and reopen Hermes Telegram Talk.",
      409: "This conversation changed. Close and reopen Hermes Telegram Talk.",
      429: "Too many attempts. Wait a minute, then tap Retry.",
      503: "Voice is unavailable on the server. Try again in a minute."})[response.status]
      || "Voice connection failed. Check your connection, then tap Retry.");
    error.status = response.status;
    error.serverMessage = typeof data.error === "string" ? data.error : "";
    throw error;
  }
  return data;
}

function updateMic(current) {
  if (call !== current) return;
  const enabled = current.phase === "ready" && !current.muted;
  current.stream?.getAudioTracks().forEach(track => { track.enabled = enabled; });
  render();
}

function render() {
  const current = call;
  const state = !current ? "idle" : current.phase === "blocked" ? "blocked"
    : current.phase !== "ready" ? "connecting" : current.muted ? "muted"
    : current.outputLevel > .015 ? "speaking" : "listening";
  orb.dataset.state = state;
  orb.setAttribute("aria-label", ({idle: "Call stopped.", blocked: "Tap to enable audio. Microphone paused.",
    connecting: "Connecting. Microphone paused.", listening: "Listening.", speaking: "Speaking.",
    muted: "Microphone muted."})[state]);
  muteButton.disabled = !current || current.phase !== "ready";
  muteButton.setAttribute("aria-pressed", String(Boolean(current?.muted)));
  muteButton.querySelector("span").textContent = current?.muted ? "Unmute" : "Mute";
  startButton.hidden = Boolean(current && current.phase !== "blocked" && !current.conversation?.saveFailed);
  startButton.textContent = current?.phase === "blocked" ? "Enable audio"
    : current?.conversation?.saveFailed ? "Retry saving" : "Retry";
  draw(current);
}

function rms(analyser, samples) {
  if (!analyser) return 0;
  analyser.getFloatTimeDomainData(samples);
  let sum = 0;
  for (const value of samples) sum += value * value;
  return Math.sqrt(sum / samples.length);
}

function draw(current) {
  const state = orb.dataset.state;
  const css = getComputedStyle(orb);
  const level = current && current.phase === "ready"
    ? Math.min(1, (state === "speaking" ? current.outputLevel : current.muted ? 0 : current.inputLevel) * 5) : 0;
  paint.clearRect(0, 0, 640, 640);
  paint.beginPath(); paint.arc(320, 320, 248, 0, Math.PI * 2);
  paint.strokeStyle = "#2d3933"; paint.lineWidth = 2; paint.stroke();
  paint.beginPath();
  const radius = 248 + (reducedMotion.matches ? 0 : level * 20);
  paint.arc(320, 320, radius, state === "connecting" ? -.7 : 0,
    state === "connecting" ? Math.PI * 1.1 : Math.PI * 2);
  paint.strokeStyle = css.color;
  paint.lineWidth = state === "muted" ? 3 : 3 + (reducedMotion.matches ? 0 : level * 14);
  paint.setLineDash(state === "muted" ? [7, 14] : []);
  paint.stroke(); paint.setLineDash([]);
}

function sample(current) {
  if (call !== current) return;
  current.inputLevel = rms(current.inputAnalyser, current.inputSamples);
  current.outputLevel = rms(current.outputAnalyser, current.outputSamples);
  if (current.phase === "greeting" && current.outputLevel > .008) current.greetingHeard = true;
  if (current.phase === "greeting" && current.greetingDone && current.greetingHeard && current.outputLevel < .008) {
    if (!current.quietSince) current.quietSince = performance.now();
    if (performance.now() - current.quietSince > 350) {
      current.phase = "ready"; clearTimeout(current.timeout); updateMic(current);
    }
  } else current.quietSince = 0;
  render();
  current.frame = requestAnimationFrame(() => sample(current));
}

function analyse(current, stream, direction) {
  const source = current.audio.createMediaStreamSource(stream);
  const analyser = current.audio.createAnalyser();
  analyser.fftSize = 512;
  source.connect(analyser);
  current.nodes.push(source, analyser);
  current[`${direction}Analyser`] = analyser;
  current[`${direction}Samples`] = new Float32Array(analyser.fftSize);
}

function checkpoint(current) {
  if (!current.voiceId || current.ended) return;
  current.ended = true;
  void post("end", { initData: current.auth, conversation_id: current.conversation.id,
    voice_session_id: current.voiceId }, { keepalive: true }).catch(() => {});
}

function stop(message = "") {
  const current = call;
  call = null;
  if (current) {
    current.abort.abort();
    clearTimeout(current.timeout);
    clearTimeout(current.disconnectTimeout);
    cancelAnimationFrame(current.frame);
    current.stream?.getTracks().forEach(track => track.stop());
    current.remoteStream?.getTracks().forEach(track => track.stop());
    clearTimeout(current.pollTimer);
    if (current.channel) {
      current.channel.onopen = current.channel.onclose = current.channel.onmessage = null;
      current.channel.close();
    }
    if (current.peer) {
      current.peer.ontrack = current.peer.onconnectionstatechange = null;
      current.peer.close();
    }
    current.nodes.forEach(node => node.disconnect());
    if (current.audio) { current.audio.onstatechange = null; void current.audio.close().catch(() => {}); }
    void flush(current.conversation);
    checkpoint(current);
  }
  remoteAudio.pause();
  remoteAudio.srcObject = null;
  showError(message);
  render();
}

async function flush(owner) {
  if (!owner || owner.busy || owner.auth !== initData() || conversation !== owner) return;
  clearTimeout(owner.retryTimer);
  owner.busy = true;
  try {
    while (owner.pending.length && conversation === owner && owner.auth === initData()) {
      const voiceId = owner.pending[0].voiceId;
      const pending = owner.pending.filter(item => item.voiceId === voiceId).slice(0, owner.batchMax);
      const data = await post("events", {initData: owner.auth, conversation_id: owner.id,
        voice_session_id: voiceId, events: pending.map(item => item.event)}, { keepalive: exiting });
      if (data.conversation_id !== owner.id || !Array.isArray(data.accepted_ids)
        || pending.some(item => !data.accepted_ids.includes(item.event.id))) throw new Error("Incomplete event acknowledgment");
      const ids = new Set(data.accepted_ids);
      owner.pending = owner.pending.filter(item => !ids.has(item.event.id));
      owner.retries = 0;
    }
    if (!owner.pending.length && owner.saveFailed) {
      owner.saveFailed = false;
      if (!exiting) { showError(); render(); }
    }
  } catch (error) {
    if ([401, 403, 404, 409].includes(error.status)) {
      owner.pending = []; owner.disabled = true;
      if (call?.conversation === owner) stop(error.message);
    } else if (!exiting && owner.retries < 3) {
      owner.retries += 1;
      owner.retryTimer = setTimeout(() => void flush(owner), 500 * 2 ** owner.retries);
    } else if (!exiting) {
      owner.saveFailed = true;
      showError("Conversation could not be saved. Tap Retry saving.");
      render();
    }
  } finally { owner.busy = false; }
}

function settle(current, event) {
  const turn = event.turn;
  if (!turn || !["user", "assistant"].includes(turn.role) || typeof turn.transcript !== "string") return;
  const text = turn.transcript.trim();
  if (!text) return;
  const identity = turn.id || event.event_id;
  const key = identity ? String(identity) : `${turn.role}:${text}`;
  if (current.seen.has(key) || (!identity && current.lastSettled === key)) return;
  current.lastSettled = key;
  if (identity) current.seen.add(key);
  if (turn.role === "user") current.lastUserText = text;
  const owner = current.conversation;
  if (owner.disabled) return;
  const characters = Array.from(text);
  const chunks = [];
  for (let offset = 0; offset < characters.length; offset += 2000) chunks.push(characters.slice(offset, offset + 2000).join(""));
  if (characters.length > 16000 || owner.pending.length + chunks.length > 128 || owner.sequence + chunks.length > 1000000) {
    stop("Conversation is too long to save. Close and reopen Hermes Telegram Talk."); return;
  }
  for (const chunk of chunks) owner.pending.push({voiceId: current.voiceId, event: {
    id: crypto.randomUUID(), sequence: owner.sequence++, role: turn.role, text: chunk,
  }});
  void flush(owner);
}

function onMessage(current, message) {
  if (call !== current || typeof message.data !== "string" || message.data.length > 65536) return;
  let event;
  try { event = JSON.parse(message.data); } catch (_) { return; }
  if (!event || typeof event !== "object") return;
  if (["session.started", "session.updated"].includes(event.type)) { current.handshake = true; advance(current); }
  if (["input_transcript.added", "output_transcript.added"].includes(event.type)) current.lastSettled = null;
  if (event.type === "turn.done") {
    settle(current, event);
    if (event.turn?.role === "assistant" && current.greetingSent) current.greetingDone = true;
  }
  if (event.type === "session.closed") stop("Voice connection closed. Tap Retry to reconnect.");
  if (event.type === "error") stop("Voice service reported an error. Tap Retry to reconnect.");
  if (event.type === "delegation.created" && event.item?.target === "client"
    && typeof event.item.id === "string" && !current.delegations.has(event.item.id)) {
    current.delegations.add(event.item.id);
    if (current.conversation?.tasks) void delegate(current, event.item);
    else append(current, "Tasks are unavailable in Hermes Telegram Talk. Nothing was executed.", "speakable", event.item.id);
  }
}

// Message framing and 500-byte chunking follow Conduit GPTLiveProtocol.contextAppendMessages (MIT).
function chunks(text) {
  const encoder = new TextEncoder();
  const out = [];
  let current = "", bytes = 0;
  for (const character of text.trim()) {
    const size = encoder.encode(character).length;
    if (bytes + size > APPEND_MAX_BYTES && current) { out.push(current); current = ""; bytes = 0; }
    current += character; bytes += size;
  }
  if (current) out.push(current);
  return out;
}

function append(current, text, channel, delegationId) {
  if (call !== current || current.channel?.readyState !== "open") return false;
  for (const chunk of chunks(text)) {
    const message = {type: delegationId ? "delegation.context.append" : "session.context.append", channel,
      content: [{type: "input_text", text: chunk}]};
    if (delegationId) message.delegation_item_id = delegationId;
    current.channel.send(JSON.stringify(message));
  }
  return true;
}

function callFields(current) {
  return {initData: current.auth, conversation_id: current.conversation.id, voice_session_id: current.voiceId};
}

async function delegate(current, item) {
  const asked = (Array.isArray(item.content) ? item.content : [])
    .filter(part => part?.type === "input_text" && typeof part.text === "string").map(part => part.text).join("").trim();
  const request = Array.from(asked || current.lastUserText || "").slice(0, 2000).join("");
  if (!request || !current.voiceId) {
    append(current, "There was nothing to hand to Hermes. Nothing was executed.", "speakable", item.id); return;
  }
  try {
    const data = await post("task", {...callFields(current), delegation_item_id: item.id, request});
    if (call !== current) return;
    append(current, data.reply, data.channel === "speakable" ? "speakable" : "commentary", item.id);
    if (data.job?.delegation_item_id === item.id) current.jobs.set(data.job.id, item.id);
    schedulePoll(current, 500);
  } catch (error) {
    if (call !== current) return;
    append(current, `Hermes could not take this task. ${error.serverMessage || error.message} Nothing was executed.`,
      "speakable", item.id);
  }
}

function schedulePoll(current, delay) {
  if (call !== current || !current.conversation?.tasks) return;
  clearTimeout(current.pollTimer);
  current.pollTimer = setTimeout(() => void poll(current), delay);
}

async function poll(current) {
  if (call !== current || !current.voiceId) return;
  let data;
  try { data = await post("tasks", callFields(current)); } catch (_) { schedulePoll(current, 5000); return; }
  if (call !== current) return;
  for (const job of Array.isArray(data.jobs) ? data.jobs : []) {
    if (job.voice_session_id === current.voiceId && current.delegations.has(job.delegation_item_id)
      && !current.jobs.has(job.id)) current.jobs.set(job.id, job.delegation_item_id);
    const delegationId = current.jobs.get(job.id);
    if (!delegationId || job.delegation_item_id !== delegationId || job.voice_session_id !== current.voiceId
      || !FINISHED.includes(job.state) || current.spoken.has(job.id)) continue;
    current.spoken.add(job.id);
    if (append(current, job.spoken || `Hermes task ${job.number} ${job.state}.`, "speakable", delegationId)) {
      void acknowledge(current, job.id, 0);
    }
  }
  if (data.context_pending || current.polls++ % 6 === 0) void refresh(current);
  schedulePoll(current, [...current.jobs.keys()].some(id => !current.spoken.has(id)) ? 2000 : 10000);
}

async function acknowledge(current, jobId, attempt) {
  try {
    await post("task/ack", {...callFields(current), job_id: jobId});
  } catch (error) {
    if (attempt < 2 && call === current && ![401, 403, 404, 409].includes(error.status)) {
      setTimeout(() => void acknowledge(current, jobId, attempt + 1), 1000 * 2 ** attempt);
    }
  }
}

async function refresh(current) {
  if (current.refreshing || call !== current || !current.voiceId) return;
  current.refreshing = true;
  try {
    const data = await post("context", callFields(current));
    if (call !== current || !Number.isInteger(data.revision) || typeof data.text !== "string" || !data.text) return;
    if (append(current, data.text, data.sources?.jobs ? "speakable" : "commentary")) {
      await post("context/ack", {...callFields(current), revision: data.revision});
    }
  } catch (_) {
    // A refresh is retried by the next poll; nothing counts as delivered without its ack.
  } finally { current.refreshing = false; }
}

function advance(current) {
  if (call !== current || !current.handshake || current.channel.readyState !== "open"
    || current.peer.connectionState !== "connected" || !current.playing || current.audio.state !== "running") return;
  if (current.conversation.greeting && !current.greetingSent) {
    current.greetingSent = true;
    current.phase = "greeting";
    current.channel.send(JSON.stringify({ type: "session.context.append", channel: "speakable",
      content: [{ type: "input_text", text: "Begin the configured opening greeting now." }] }));
  } else if (!current.conversation.greeting) current.phase = "ready";
  else if (current.greetingDone) current.phase = "greeting";
  if (current.phase === "ready") clearTimeout(current.timeout);
  updateMic(current);
}

async function enableAudio(current) {
  if (call !== current) return;
  try {
    let timer;
    try {
      await Promise.race([current.audio.resume(), new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error("Audio needs a tap")), 1200);
      })]);
    } finally { clearTimeout(timer); }
    if (current.audio.state !== "running") throw new Error("Audio paused");
    if (remoteAudio.srcObject) {
      await remoteAudio.play();
      if (remoteAudio.paused) throw new Error("Audio paused");
      current.playing = true;
    }
    if (call !== current) return;
    if (current.phase === "blocked") current.phase = current.greetingSent ? "greeting" : "connecting";
    showError(); advance(current); render();
  } catch (_) {
    if (call !== current) return;
    current.playing = false; current.phase = "blocked";
    updateMic(current);
    showError("Tap Enable audio to hear the call. Your microphone is paused.");
  }
}

async function waitForIce(current) {
  if (current.peer.iceGatheringState === "complete") return;
  await new Promise((resolve, reject) => {
    const peer = current.peer;
    const done = () => { clearTimeout(timer); peer.removeEventListener("icegatheringstatechange", check);
      current.abort.signal.removeEventListener("abort", cancel); resolve(); };
    const check = () => { if (peer.iceGatheringState === "complete") done(); };
    const cancel = () => { done(); reject(new DOMException("Aborted", "AbortError")); };
    const timer = setTimeout(done, 1500);
    peer.addEventListener("icegatheringstatechange", check);
    current.abort.signal.addEventListener("abort", cancel, { once: true });
  });
}

function applyProfile(data) {
  if (typeof data.profile?.display_name !== "string" || !/^#[0-9a-f]{6}$/i.test(data.appearance?.listen_color)
    || !/^#[0-9a-f]{6}$/i.test(data.appearance?.speak_color)) throw new Error("Voice configuration is unavailable. Tap Retry.");
  document.getElementById("name").textContent = data.profile.display_name;
  document.documentElement.style.setProperty("--listen", data.appearance.listen_color);
  document.documentElement.style.setProperty("--speak", data.appearance.speak_color);
  const avatar = document.getElementById("avatar");
  const path = new URL(data.profile.avatar_url || "", location.href);
  if (path.origin === location.origin && path.pathname === "/tg-voice/avatar" && !path.search && !path.hash && !path.username && !path.password) {
    avatar.onload = () => { avatar.hidden = false; document.getElementById("face").setAttribute("hidden", ""); };
    avatar.onerror = () => { avatar.hidden = true; document.getElementById("face").removeAttribute("hidden"); };
    avatar.src = path.href;
  }
}

async function start() {
  if (exiting) return;
  if (call) {
    if (call.phase === "blocked") void enableAudio(call);
    else if (call.conversation?.saveFailed) {
      call.conversation.retries = 0;
      void flush(call.conversation);
    }
    return;
  }
  const auth = initData();
  if (!auth) { stop("Open Hermes Telegram Talk from Telegram to sign in."); return; }
  const current = { auth, phase: "connecting", abort: new AbortController(), nodes: [],
    seen: new Set(), delegations: new Set(), jobs: new Map(), spoken: new Set(), polls: 0,
    muted: false, inputLevel: 0, outputLevel: 0 };
  call = current; showError(); render();
  try {
    current.audio = new AudioContext();
    current.audio.onstatechange = () => {
      if (call !== current) return;
      if (current.audio.state === "closed") { stop("Audio stopped. Tap Retry to reconnect."); return; }
      if (current.audio.state !== "running" && current.phase !== "blocked") {
        current.phase = "blocked"; current.playing = false; updateMic(current);
      }
    };
    void enableAudio(current);
    current.timeout = setTimeout(() => { if (call === current) stop("Voice did not become ready. Tap Retry to reconnect."); }, 30000);
    const data = await post("bootstrap", {initData: auth}, {signal: current.abort.signal});
    if (call !== current) return;
    if (typeof data.conversation_id !== "string" || !Number.isInteger(data.limits?.event_batch_max)
      || data.limits.event_batch_max < 1 || data.limits.event_batch_max > 128
      || typeof data.greeting_enabled !== "boolean") throw new Error("Voice configuration is unavailable. Tap Retry.");
    applyProfile(data);
    if (!conversation || conversation.id !== data.conversation_id || conversation.auth !== auth) {
      if (conversation) clearTimeout(conversation.retryTimer);
      conversation = {id: data.conversation_id, auth, pending: [], sequence: 0, retries: 0};
    }
    current.conversation = conversation;
    conversation.batchMax = data.limits.event_batch_max;
    conversation.greeting = data.greeting_enabled;
    conversation.tasks = data.tasks?.available === true;
    conversation.disabled = false;
    conversation.retries = 0;
    void flush(conversation);
    try {
      current.stream = await navigator.mediaDevices.getUserMedia({audio: {
        echoCancellation: true, noiseSuppression: true, autoGainControl: true,
      }});
    } catch (error) {
      throw new Error(error.name === "NotAllowedError"
        ? "Microphone access denied. Allow microphone access in Telegram or device settings, then tap Retry."
        : "Microphone unavailable. Check your device, then tap Retry.");
    }
    current.stream.getAudioTracks().forEach(track => { track.enabled = false; });
    if (call !== current) { current.stream.getTracks().forEach(track => track.stop()); return; }
    analyse(current, current.stream, "input");
    current.peer = new RTCPeerConnection();
    current.stream.getTracks().forEach(track => current.peer.addTrack(track, current.stream));
    current.peer.ontrack = event => {
      if (call !== current) { event.track.stop(); return; }
      current.remoteStream = event.streams[0] || new MediaStream([event.track]);
      remoteAudio.srcObject = current.remoteStream;
      analyse(current, current.remoteStream, "output");
      void enableAudio(current);
    };
    current.peer.onconnectionstatechange = () => {
      if (call !== current) return;
      const state = current.peer.connectionState;
      if (state === "failed" || state === "closed") stop("Connection lost. Tap Retry to reconnect.");
      else if (state === "disconnected") {
        current.phase = "connecting"; updateMic(current);
        current.disconnectTimeout = setTimeout(() => { if (call === current) stop("Connection lost. Tap Retry to reconnect."); }, 6000);
      } else if (state === "connected") { clearTimeout(current.disconnectTimeout); advance(current); }
    };
    current.channel = current.peer.createDataChannel("oai-events");
    current.channel.onopen = () => advance(current);
    current.channel.onmessage = message => onMessage(current, message);
    current.channel.onclose = () => { if (call === current) stop("Connection lost. Tap Retry to reconnect."); };
    await current.peer.setLocalDescription(await current.peer.createOffer());
    await waitForIce(current);
    const answer = await post("session", {initData: auth, sdp: current.peer.localDescription.sdp,
      conversation_id: conversation.id}, {signal: current.abort.signal});
    if (answer.conversation_id !== current.conversation.id || typeof answer.voice_session_id !== "string"
      || typeof answer.sdp !== "string" || answer.type !== "answer") throw new Error("Voice handshake failed. Tap Retry.");
    current.voiceId = answer.voice_session_id;
    if (call !== current) { checkpoint(current); return; }
    await current.peer.setRemoteDescription({type: "answer", sdp: answer.sdp});
    schedulePoll(current, 1500);
    sample(current);
  } catch (error) {
    if (call !== current) return;
    if ([401, 403, 404, 409].includes(error.status) && conversation) {
      clearTimeout(conversation.retryTimer); conversation.pending = []; conversation = null;
    }
    stop(error.name === "AbortError" ? "" : error.message || "Voice is unavailable. Tap Retry.");
  }
}

function onPageHide(event) {
  exiting = true;
  stop();
  if (conversation) clearTimeout(conversation.retryTimer);
  startButton.removeEventListener("click", start);
  muteButton.removeEventListener("click", toggleMute);
  remoteAudio.removeEventListener("pause", onAudioPause);
  const avatar = document.getElementById("avatar");
  avatar.onload = avatar.onerror = null;
  window.removeEventListener("pagehide", onPageHide);
  if (!event.persisted) window.removeEventListener("pageshow", onPageShow);
}
function onPageShow(event) {
  if (!event.persisted) return;
  exiting = false;
  bindControls();
  void start();
}
function onAudioPause() {
  if (!call || !remoteAudio.srcObject) return;
  call.phase = "blocked"; call.playing = false; updateMic(call);
}
function toggleMute() {
  if (call?.phase !== "ready") return;
  call.muted = !call.muted; updateMic(call);
}
if (webApp) { webApp.ready(); webApp.expand(); }
function bindControls() {
  startButton.addEventListener("click", start);
  muteButton.addEventListener("click", toggleMute);
  remoteAudio.addEventListener("pause", onAudioPause);
  window.addEventListener("pagehide", onPageHide);
}
bindControls();
window.addEventListener("pageshow", onPageShow);
render();
void start();
