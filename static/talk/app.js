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
const transcriptList = document.getElementById("transcript");
const voiceBox = document.getElementById("voice-box");
const voiceSelect = document.getElementById("voice");
const voiceNote = document.getElementById("voice-note");
const threadNote = document.getElementById("thread-note");
// The /talk button URL carries a server-issued handle for the chat thread it was sent in; nothing else binds one.
const launch = /^[A-Za-z0-9_-]{32,64}$/.test(new URLSearchParams(location.search).get("launch") || "")
  ? new URLSearchParams(location.search).get("launch") : null;
const THREAD_NOTES = {unbound: "Not linked to a chat. Send /talk there to link it.",
  expired: "Talk link expired. Send /talk in the chat again.", used: "Talk link already used. Send /talk again.",
  invalid: "Talk link not valid. Send /talk in the chat again.", foreign: "Talk link is for another account.",
  not_private: "Chat input works only in a private chat.", unavailable: "Chat input is unavailable here."};
const reconnectButton = document.getElementById("reconnect");
const menuButton = document.getElementById("menu-toggle");
const callMenu = document.getElementById("call-menu");
const endButton = document.getElementById("end");
let microphoneMuted = false;
let followingTranscript = true;
let call = null;
let conversation = null;
let exiting = false;
const APPEND_MAX_BYTES = 500;
const FINISHED = ["succeeded", "failed", "cancelled", "interrupted"];
// Statuses that prove the server refused the task before accepting it; anything else may have been accepted.
const REFUSED = [400, 401, 403, 404, 409, 413, 415, 429, 503];
const TRANSCRIPT_LINES = 100;
const TRANSCRIPT_CHARS = 4000;
// The on-screen transcript of this page: interim fragments per role form one live line that the final turn
// replaces (a correction); a turn id is shown once; an assistant turn the user spoke over is marked interrupted.
// It is display only: nothing here is posted to Telegram, and only final turns are saved (see settle).
const transcript = {lines: [], live: {user: null, assistant: null}, shown: new Set()};
let voiceState = null;
const PULSE_FEEDBACK = /^\s*pulse\s+(?:feedback\s+)?(?:card\s+)?p\s*\d{1,3}\b/i;

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
  const mediaReady = current && current.phase === "ready" && current.playing && current.audio?.state === "running"
    && current.peer?.connectionState === "connected";
  const state = !current ? "idle" : current.phase === "blocked" ? "blocked"
    : current.phase !== "ready" ? "connecting" : !mediaReady ? "blocked" : current.outputLevel > .015 && !remoteAudio.muted && remoteAudio.volume !== 0
      ? "speaking" : current.muted ? "muted" : current.inputLevel > .005 && current.stream?.getAudioTracks().some(track => track.enabled && track.readyState !== "ended")
      ? "listening" : "silent";
  orb.dataset.state = state;
  orb.setAttribute("aria-label", ({idle: "Call stopped.", blocked: "Tap to enable audio. Microphone paused.",
    connecting: "Connecting. Microphone paused.", listening: "Listening.", speaking: "Speaking.",
    silent: "Connected. Quiet.", muted: "Microphone muted."})[state]);
  const status = document.getElementById("call-status");
  const label = orb.getAttribute?.("aria-label") || state;
  if (status.textContent !== label) status.textContent = label;
  if (endButton) endButton.disabled = !current;
  muteButton.setAttribute("aria-label", current?.muted ? "Unmute microphone" : "Mute microphone");
  muteButton.disabled = !current || current.phase !== "ready";
  muteButton.setAttribute("aria-pressed", String(Boolean(current?.muted)));
  muteButton.querySelector("span").textContent = current?.muted ? "Unmute" : "Mute";
  startButton.hidden = Boolean(current && current.phase !== "blocked" && !current.conversation?.saveFailed);
  startButton.textContent = current?.phase === "blocked" ? "Enable audio"
    : current?.conversation?.saveFailed ? "Retry saving" : errorBox.textContent ? "Retry" : "Connect";
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
  const level = current && ["speaking", "listening"].includes(state)
    ? Math.min(1, (state === "speaking" ? current.outputLevel : current.inputLevel) * 5) : 0;
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
      current.phase = "ready"; current.greetingOver = true; clearTimeout(current.greetingTimer);
      mark(current, "greeting_done"); mark(current, "ready");
      updateMic(current);
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

const CONNECT_MS = 30000;
const GREETING_MS = 12000;
const SETUP = [["bootstrap", c => c.conversation], ["microphone", c => c.stream], ["session", c => c.voiceId]];
const TRANSPORT = [["peer", c => c.peer?.connectionState === "connected"],
  ["channel", c => c.channel?.readyState === "open"], ["handshake", c => c.handshake],
  ["audio", c => c.audio?.state === "running"], ["playing", c => c.playing]];

function mark(current, stage) {
  current.stages ??= {};
  if (stage && current.stages[stage] === undefined) current.stages[stage] = Math.round(performance.now() - (current.began ?? 0));
  globalThis.talkReadiness = {stages: {...current.stages}, failure: current.failure || "none"};
}

function unmet(current, gates = [...SETUP, ...TRANSPORT]) {
  return (gates.find(([, met]) => !met(current)) || ["none"])[0];
}

function checkpoint(current) {
  if (!current.voiceId || current.ended) return;
  current.ended = true;
  void post("end", { initData: current.auth, conversation_id: current.conversation.id,
    voice_session_id: current.voiceId, readiness: {stages: current.stages || {}, failure: current.failure || "none"} },
    { keepalive: true }).catch(() => {});
}

function stop(message = "") {
  const current = call;
  call = null;
  if (current) {
    current.abort.abort();
    clearTimeout(current.timeout);
    clearTimeout(current.avatarTimer);
    if (current.avatarObject) URL.revokeObjectURL(current.avatarObject);
    clearTimeout(current.greetingTimer);
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
  transcriptCut();
  showError(message);
  render();
  renderVoice(null);
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

function renderTranscript() {
  if (typeof document.createElement !== "function" || !transcriptList?.replaceChildren) return;
  const stick = followingTranscript;
  transcriptList.replaceChildren(...transcript.lines.map(line => {
    const item = document.createElement("li");
    item.dataset.role = line.role;
    item.dataset.state = line.state;
    item.textContent = line.text;
    const note = line.state === "interrupted" ? "Interrupted" : line.state === "cut" ? "Cut off" : "";
    if (note) { const small = document.createElement("small"); small.textContent = note; item.append(small); }
    return item;
  }));
  transcriptList.hidden = transcript.lines.length === 0;
  if (stick) transcriptList.scrollTop = transcriptList.scrollHeight;
}

function pinTranscript() {
  // A rotation or resize keeps the newest line in view when the reader was already at the end.
  if (followingTranscript && transcriptList && !transcriptList.hidden) transcriptList.scrollTop = transcriptList.scrollHeight;
}

function transcriptLine(role) {
  let line = transcript.live[role];
  if (!line) {
    line = {role, text: "", state: "interim"};
    transcript.live[role] = line;
    transcript.lines.push(line);
    if (transcript.lines.length > TRANSCRIPT_LINES) transcript.lines.splice(0, transcript.lines.length - TRANSCRIPT_LINES);
  }
  return line;
}

function transcriptFragment(role, text) {
  if (typeof text !== "string" || !text) return;
  const line = transcriptLine(role);
  line.text = (line.text + text).slice(0, TRANSCRIPT_CHARS);
  renderTranscript();
}

function transcriptFinal(role, key, text, interrupted) {
  if (transcript.shown.has(key)) return;
  transcript.shown.add(key);
  const line = transcriptLine(role);
  line.text = text.slice(0, TRANSCRIPT_CHARS);
  line.state = interrupted ? "interrupted" : "final";
  transcript.live[role] = null;
  renderTranscript();
}

function transcriptCut() {
  // A reconnect ends unsettled lines: what was shown stays, marked as never finished; nothing is saved for them.
  for (const role of ["user", "assistant"]) {
    const line = transcript.live[role];
    if (line) { if (line.text) line.state = "cut"; else transcript.lines.splice(transcript.lines.indexOf(line), 1); }
    transcript.live[role] = null;
  }
  renderTranscript();
}

function renderVoice(current) {
  if (!voiceBox || !voiceSelect) return;
  const choices = Array.isArray(voiceState?.choices) ? voiceState.choices : [];
  voiceBox.hidden = choices.length < 2;
  if (typeof document.createElement === "function" && voiceSelect.replaceChildren && voiceSelect.dataset.built !== JSON.stringify(choices)) {
    voiceSelect.replaceChildren(...choices.map(choice => {
      const option = document.createElement("option");
      option.value = choice.id; option.textContent = choice.label;
      return option;
    }));
    voiceSelect.dataset.built = JSON.stringify(choices);
  }
  if (voiceState) voiceSelect.value = voiceState.current;
  // The provider sets the voice when a session starts; a live call keeps its voice until it reconnects.
  const pending = Boolean(current && current.voice && voiceState && current.voice !== voiceState.current);
  voiceNote.hidden = !pending;
  reconnectButton.hidden = !pending;
}

async function loadVoice(current) {
  try {
    const data = await post("voice", {initData: current.auth, ...(launch ? {launch} : {})}, {signal: current.abort.signal});
    const status = data.thread && typeof data.thread.status === "string" ? data.thread.status : "bound";
    threadNote.textContent = THREAD_NOTES[status] || "";
    threadNote.hidden = !THREAD_NOTES[status];
    if (data.voice && Array.isArray(data.voice.choices) && typeof data.voice.current === "string") {
      voiceState = data.voice;
      // The voice this call's session was created with: the account's stored choice when the call started.
      if (call === current && current.voice === undefined) current.voice = voiceState.current;
    }
  } catch (_) { /* no selector without an answer; the call itself is unaffected */ }
  renderVoice(call);
}

async function chooseVoice() {
  const current = call;
  const auth = current?.auth || initData();
  if (!auth || !voiceSelect.value || voiceSelect.value === voiceState?.current) return;
  voiceSelect.disabled = true;
  try {
    const data = await post("voice", {initData: auth, voice: voiceSelect.value});
    voiceState = data.voice;
    showError();
  } catch (error) {
    showError(error.status === 400 ? "That voice is not available." : error.message);
  } finally {
    voiceSelect.disabled = false;
    renderVoice(call);
  }
}

function reconnectVoice() {
  if (!call || reconnectButton.hidden) return;
  if (callMenu) callMenu.hidden = true;
  menuButton?.setAttribute("aria-expanded", "false");
  stop();
  void start();
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
  transcriptFinal(turn.role, identity ? `${current.voiceId}:${identity}` : `${current.voiceId}:${turn.role}:${text}:${current.seen.size}:${transcript.lines.length}`,
    text, turn.role === "assistant" && current.interrupted);
  if (turn.role === "user") current.lastUserText = text;
  const owner = current.conversation;
  if (owner.disabled) return;
  const characters = Array.from(text);
  const chunks = [];
  for (let offset = 0; offset < characters.length; offset += 2000) chunks.push(characters.slice(offset, offset + 2000).join(""));
  if (characters.length > 16000 || owner.pending.length + chunks.length > 128 || owner.sequence + chunks.length > 1000000) {
    stop("Conversation is too long to save. Close and reopen Hermes Telegram Talk."); return;
  }
  // Spoken over by the user before it settled: its text is not evidence that any Pulse update was told.
  const interrupted = turn.role === "assistant" && current.interrupted;
  for (const chunk of chunks) owner.pending.push({voiceId: current.voiceId, event: {
    id: crypto.randomUUID(), sequence: owner.sequence++, role: turn.role, text: chunk,
    ...(interrupted ? {interrupted: true} : {}),
  }});
  void flush(owner);
}

function onMessage(current, message) {
  if (call !== current || typeof message.data !== "string" || message.data.length > 65536) return;
  let event;
  try { event = JSON.parse(message.data); } catch (_) { return; }
  if (!event || typeof event !== "object") return;
  if (["session.started", "session.updated"].includes(event.type)) {
    current.handshake = true; mark(current, "handshake"); advance(current);
  }
  if (["input_transcript.added", "output_transcript.added"].includes(event.type)) current.lastSettled = null;
  if (event.type === "output_transcript.added") { current.answering = true; transcriptFragment("assistant", event.item?.text); }
  if (event.type === "input_transcript.added") transcriptFragment("user", event.item?.text);
  if (event.type === "input_transcript.added" && current.answering) current.interrupted = true;
  if (event.type === "turn.done") {
    settle(current, event);
    if (event.turn?.role === "assistant") { current.answering = false; current.interrupted = false; }
    if (event.turn?.role === "assistant" && current.greetingSent) current.greetingDone = true;
  }
  if (event.type === "session.closed") stop("Voice connection closed. Tap Retry to reconnect.");
  if (event.type === "error") stop("Voice service reported an error. Tap Retry to reconnect.");
  if (event.type === "delegation.created" && event.item?.target === "client"
    && typeof event.item.id === "string" && !current.delegations.has(event.item.id)) {
    current.delegations.add(event.item.id);
    if (PULSE_FEEDBACK.test(delegationText(event.item))) void feedback(current, event.item);
    else if (current.conversation?.tasks) void delegate(current, event.item);
    else current.channel.send(JSON.stringify({type: "delegation.context.append", delegation_item_id: event.item.id,
      channel: "speakable", content: [{type: "input_text", text: "Tasks are unavailable in Hermes Telegram Talk. Nothing was executed."}]}));
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

// "sent": every chunk was queued; "none": nothing was; "partial": some chunks were, so it must not be repeated.
function append(current, text, channel, delegationId) {
  if (call !== current || current.channel?.readyState !== "open") return "none";
  const parts = chunks(text);
  let queued = 0;
  try {
    for (const chunk of parts) {
      const message = {type: delegationId ? "delegation.context.append" : "session.context.append", channel,
        content: [{type: "input_text", text: chunk}]};
      if (delegationId) message.delegation_item_id = delegationId;
      current.channel.send(JSON.stringify(message));
      queued += 1;
    }
  } catch (_) {
    return queued ? "partial" : "none";
  }
  return "sent";
}

function callFields(current) {
  return {initData: current.auth, conversation_id: current.conversation.id, voice_session_id: current.voiceId};
}

function delegationText(item) {
  return (Array.isArray(item.content) ? item.content : [])
    .filter(part => part?.type === "input_text" && typeof part.text === "string").map(part => part.text).join("").trim();
}

// Feedback is idempotent per delegation on the server, so a lost answer is resubmitted with the same id.
async function feedback(current, item) {
  const request = Array.from(delegationText(item)).slice(0, 200).join("");
  for (let attempt = 0; attempt < 3 && call === current && current.voiceId; attempt += 1) {
    try {
      const data = await post("pulse/feedback", {...callFields(current), delegation_item_id: item.id, request});
      if (typeof data.reply === "string" && data.reply) append(current, data.reply, "speakable", item.id);
      return;
    } catch (error) {
      if (REFUSED.includes(error.status)) {
        append(current, "Pulse could not record that reaction. Nothing changed.", "speakable", item.id); return;
      }
      await new Promise(resolve => setTimeout(resolve, 1000 * 2 ** attempt));
    }
  }
}

async function delegate(current, item) {
  const asked = delegationText(item);
  const request = Array.from(asked || current.lastUserText || "").slice(0, 2000).join("");
  if (!request || !current.voiceId) {
    append(current, "There was nothing to hand to Hermes. Nothing was executed.", "speakable", item.id); return;
  }
  // The same delegation id is idempotent on the server, so an unconfirmed submission is safe to repeat.
  for (let attempt = 0; ; attempt += 1) {
    let data = null, refused = null;
    try {
      data = await post("task", {...callFields(current), delegation_item_id: item.id, request});
    } catch (error) {
      if (REFUSED.includes(error.status)) refused = error;
    }
    if (call !== current) return;
    // A refusal of a resubmission does not show that the first, unanswered submission did not run.
    if (refused && attempt === 0) {
      append(current, `Hermes could not take this task. ${refused.serverMessage || refused.message} Nothing was executed.`,
        "speakable", item.id);
      return;
    }
    if (data && typeof data.reply === "string" && typeof data.job?.id === "string") {
      // "cancel task N" and "result of task N" answer with the job they name, not one for this delegation.
      if (data.job.delegation_item_id === item.id) current.jobs.set(data.job.id, item.id);
      append(current, data.reply, data.channel === "speakable" ? "speakable" : "commentary", item.id);
      schedulePoll(current, 500);
      return;
    }
    if (attempt === 0) append(current, "Checking whether Hermes received this task.", "commentary", item.id);
    if (attempt >= 2 || refused) {
      append(current, "It is not confirmed whether Hermes received this task. If it did, its result will still arrive "
        + "here or in Telegram. Do not say that it ran or that nothing ran.", "speakable", item.id);
      schedulePoll(current, 500);
      return;
    }
    await new Promise(resolve => setTimeout(resolve, 1000 * 2 ** attempt));
    if (call !== current) return;
  }
}

function schedulePoll(current, delay) {
  if (call !== current) return;
  clearTimeout(current.pollTimer);
  current.pollTimer = setTimeout(() => void poll(current), delay);
}

async function poll(current) {
  if (call !== current || !current.voiceId) return;
  if (!current.conversation?.tasks) {
    const n = current.polls++;
    await refresh(current);
    schedulePoll(current, n < 3 ? 10000 : 60000);
    return;
  }
  let delay = 5000;
  try {
    const data = await post("tasks", callFields(current));
    if (call !== current) return;
    for (const job of Array.isArray(data.jobs) ? data.jobs : []) {
      if (job.voice_session_id === current.voiceId && current.delegations.has(job.delegation_item_id)
        && !current.jobs.has(job.id)) current.jobs.set(job.id, job.delegation_item_id);
      const delegationId = current.jobs.get(job.id);
      if (!delegationId || job.delegation_item_id !== delegationId || job.voice_session_id !== current.voiceId
        || !FINISHED.includes(job.state) || current.spoken.has(job.id) || current.cut.has(job.id)) continue;
      const outcome = append(current, job.spoken || `Hermes task ${job.number} ${job.state}.`, "speakable", delegationId);
      // Only a whole append is acknowledged. A cut one stays undelivered for the next call, never repeated here.
      if (outcome === "sent") { current.spoken.add(job.id); void acknowledge(current, job.id, 0); }
      else if (outcome === "partial") current.cut.add(job.id);
    }
    const n = current.polls++;
    if (data.context_pending || n % 6 === 0 || n < 3) void refresh(current);
    delay = [...current.jobs.keys()].some(id => !current.spoken.has(id) && !current.cut.has(id)) ? 2000 : 10000;
  } catch (_) {
    delay = 5000;
  } finally {
    schedulePoll(current, delay);
  }
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
  if (current.refreshing || current.contextCut || call !== current || !current.voiceId) return;
  current.refreshing = true;
  try {
    const data = await post("context", callFields(current));
    if (call !== current || !Number.isInteger(data.revision) || typeof data.text !== "string" || !data.text) return;
    const outcome = append(current, data.text, data.sources?.jobs ? "speakable" : "commentary");
    if (outcome === "sent") await post("context/ack", {...callFields(current), revision: data.revision});
    else if (outcome === "partial") current.contextCut = true;
  } catch (_) {
    // A refresh is retried by the next poll; nothing counts as delivered without its ack.
  } finally { current.refreshing = false; }
}

function advance(current) {
  if (call !== current) return;
  for (const [stage, met] of TRANSPORT) if (met(current)) mark(current, stage);
  if (unmet(current, TRANSPORT) !== "none") return;
  // Transport and audio are usable: the connection deadline is met. A greeting never ends a connected call.
  mark(current, "usable");
  clearTimeout(current.timeout);
  if (current.conversation.greeting && !current.greetingSent) {
    current.greetingSent = true;
    current.phase = "greeting";
    mark(current, "greeting");
    current.channel.send(JSON.stringify({ type: "session.context.append", channel: "speakable",
      content: [{ type: "input_text", text: "Begin the configured opening greeting now." }] }));
    current.greetingTimer = setTimeout(() => {
      if (call !== current) return;
      current.greetingOver = true;
      current.failure = "greeting:" + (current.greetingHeard ? "unfinished" : current.greetingDone ? "unheard" : "no_turn");
      mark(current);
      if (current.phase !== "greeting") return;
      current.phase = "ready"; mark(current, "ready"); updateMic(current);
    }, GREETING_MS);
  } else if (!current.conversation.greeting) { current.phase = "ready"; mark(current, "ready"); }
  else if (current.greetingOver) current.phase = "ready";
  else if (current.greetingDone || current.phase === "blocked" || current.phase === "connecting") current.phase = "greeting";
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
  document.documentElement.style.setProperty("--listen", "#4FC3F7");
  document.documentElement.style.setProperty("--speak", "#FFB74D");
  const avatar = document.getElementById("avatar");
  const path = new URL(data.profile.avatar_url || "", location.href);
  if (path.origin === location.origin && path.pathname === "/tg-voice/avatar" && !path.search && !path.hash && !path.username && !path.password) {
    avatar.onload = () => { avatar.hidden = false; document.getElementById("face").setAttribute("hidden", ""); };
    avatar.onerror = () => { avatar.hidden = true; document.getElementById("face").removeAttribute("hidden"); };
    avatar.src = path.href;
  }
}

async function refreshAvatar(current) {
  try {
    const response = await fetch("api/avatar", {method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify({initData: current.auth}), signal: current.abort.signal});
    if (!response.ok || call !== current) return;
    const blob = await response.blob();
    if (call !== current || !["image/png", "image/jpeg", "image/svg+xml"].includes(blob.type) || blob.size > 1024 * 1024) return;
    const avatar = document.getElementById("avatar");
    const previous = current.avatarObject;
    current.avatarObject = URL.createObjectURL(blob);
    avatar.src = current.avatarObject;
    if (previous) URL.revokeObjectURL(previous);
  } catch (_) { /* Keep the last good image on a temporary failure. */ }
  finally {
    if (call === current) {
      const delay = current.avatarRequested ? 30000 : 1000;
      current.avatarRequested = true;
      current.avatarTimer = setTimeout(() => void refreshAvatar(current), delay);
    }
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
  const current = { auth, phase: "connecting", abort: new AbortController(), nodes: [], began: performance.now(), stages: {},
    seen: new Set(), delegations: new Set(), jobs: new Map(), spoken: new Set(), cut: new Set(), polls: 0,
    muted: microphoneMuted, inputLevel: 0, outputLevel: 0 };
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
    current.timeout = setTimeout(() => {
      if (call !== current) return;
      current.failure = "connect:" + unmet(current); mark(current);
      stop("Voice did not become ready. Tap Retry to reconnect.");
    }, CONNECT_MS);
    const data = await post("bootstrap", {initData: auth}, {signal: current.abort.signal});
    if (call !== current) return;
    if (typeof data.conversation_id !== "string" || !Number.isInteger(data.limits?.event_batch_max)
      || data.limits.event_batch_max < 1 || data.limits.event_batch_max > 128
      || typeof data.greeting_enabled !== "boolean") throw new Error("Voice configuration is unavailable. Tap Retry.");
    applyProfile(data);
    void refreshAvatar(current);
    void loadVoice(current);  // off the startup path; the selector appears when it answers
    if (!conversation || conversation.id !== data.conversation_id || conversation.auth !== auth) {
      if (conversation) clearTimeout(conversation.retryTimer);
      conversation = {id: data.conversation_id, auth, pending: [], sequence: 0, retries: 0};
    }
    current.conversation = conversation;
    mark(current, "bootstrap");
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
    mark(current, "microphone");
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
    mark(current, "offer");
    const answer = await post("session", {initData: auth, sdp: current.peer.localDescription.sdp,
      conversation_id: conversation.id, ...(launch ? {launch} : {})}, {signal: current.abort.signal});
    if (answer.conversation_id !== current.conversation.id || typeof answer.voice_session_id !== "string"
      || typeof answer.sdp !== "string" || answer.type !== "answer") throw new Error("Voice handshake failed. Tap Retry.");
    current.voiceId = answer.voice_session_id;
    if (call !== current) { checkpoint(current); return; }
    mark(current, "session");
    await current.peer.setRemoteDescription({type: "answer", sdp: answer.sdp});
    mark(current, "remote");
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
  menuButton?.removeEventListener("click", toggleMenu);
  endButton?.removeEventListener("click", endCall);
  document.removeEventListener?.("keydown", closeMenu);
  transcriptList?.removeEventListener("scroll", trackTranscriptScroll);
  startButton.removeEventListener("click", start);
  muteButton.removeEventListener("click", toggleMute);
  voiceSelect?.removeEventListener("change", chooseVoice);
  reconnectButton?.removeEventListener("click", reconnectVoice);
  window.removeEventListener("resize", pinTranscript);
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
  microphoneMuted = !call.muted; call.muted = microphoneMuted; updateMic(call);
}
if (webApp) { webApp.ready(); webApp.expand(); }
function toggleMenu() {
  callMenu.hidden = !callMenu.hidden;
  menuButton.setAttribute("aria-expanded", String(!callMenu.hidden));
  if (!callMenu.hidden) voiceSelect?.focus?.();
}
function closeMenu(event) {
  if (event.key === "Escape" && !callMenu.hidden) { toggleMenu(); menuButton.focus(); }
}
function endCall() { stop(); callMenu.hidden = true; menuButton.setAttribute("aria-expanded", "false"); }
function trackTranscriptScroll() {
  followingTranscript = transcriptList.scrollTop + transcriptList.clientHeight >= transcriptList.scrollHeight - 24;
}
function bindControls() {
  menuButton?.addEventListener("click", toggleMenu);
  endButton?.addEventListener("click", endCall);
  document.addEventListener?.("keydown", closeMenu);
  transcriptList?.addEventListener("scroll", trackTranscriptScroll);
  startButton.addEventListener("click", start);
  muteButton.addEventListener("click", toggleMute);
  voiceSelect?.addEventListener("change", chooseVoice);
  reconnectButton?.addEventListener("click", reconnectVoice);
  window.addEventListener("resize", pinTranscript);
  remoteAudio.addEventListener("pause", onAudioPause);
  window.addEventListener("pagehide", onPageHide);
}
bindControls();
window.addEventListener("pageshow", onPageShow);
render();
void start();
