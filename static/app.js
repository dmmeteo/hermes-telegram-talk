"use strict";

const startButton = document.getElementById("start");
const muteButton = document.getElementById("mute");
const endButton = document.getElementById("end");
const errorBox = document.getElementById("error");
const remoteAudio = document.getElementById("remote");
const webApp = window.Telegram && window.Telegram.WebApp;
const NO_BACKEND = "This Pewds voice test has no backend: nothing was executed or delegated.";

let call = null;

if (webApp) {
  webApp.ready();
  webApp.expand();
}

function telegramInitData() {
  if (webApp && webApp.initData) return webApp.initData;
  return new URLSearchParams(location.hash.slice(1)).get("tgWebAppData") || "";
}

function showError(message) {
  errorBox.textContent = message || "";
  errorBox.hidden = !message;
}

function render() {
  const live = Boolean(call);
  startButton.disabled = live;
  startButton.textContent = live && !call.connected ? "Starting…" : "Start";
  muteButton.disabled = !live || !call.connected;
  muteButton.setAttribute("aria-pressed", String(live && call.muted));
  muteButton.textContent = live && call.muted ? "Unmute" : "Mute";
  endButton.disabled = !live;
}

function end(message) {
  const current = call;
  call = null;
  if (current) {
    if (current.stream) current.stream.getTracks().forEach((track) => track.stop());
    if (current.channel) current.channel.close();
    if (current.peer) current.peer.close();
    if (current.request) current.request.abort();
  }
  remoteAudio.srcObject = null;
  showError(message);
  render();
}

function rejectDelegation(current, event) {
  const item = event.item || {};
  const callId = event.type === "response.function_call_arguments.done" ? event.call_id
    : item.type === "function_call" && event.type === "response.output_item.done" ? item.call_id : null;
  if (!callId || current.channel.readyState !== "open") return;
  current.channel.send(JSON.stringify({
    type: "conversation.item.create",
    item: { type: "function_call_output", call_id: callId, output: JSON.stringify({ error: NO_BACKEND }) },
  }));
  current.channel.send(JSON.stringify({ type: "response.create" }));
}

async function requestAnswer(current, sdp) {
  const response = await fetch("api/session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ initData: telegramInitData(), sdp }),
    credentials: "same-origin",
    signal: current.request.signal,
  });
  let payload = {};
  try { payload = await response.json(); } catch (_) { /* non-JSON error page */ }
  if (!response.ok || typeof payload.sdp !== "string") {
    throw new Error(payload.error || `Server error (HTTP ${response.status}). Tap Start to try again.`);
  }
  return payload.sdp;
}

function waitForIce(peer) {
  if (peer.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => { peer.removeEventListener("icegatheringstatechange", check); resolve(); };
    const check = () => { if (peer.iceGatheringState === "complete") done(); };
    peer.addEventListener("icegatheringstatechange", check);
    setTimeout(done, 1500);
  });
}

async function start() {
  if (call) return;
  if (!telegramInitData()) {
    showError("Open this page from the Pewds bot in Telegram.");
    return;
  }
  const current = { connected: false, muted: false, request: new AbortController() };
  call = current;
  showError("");
  render();
  remoteAudio.play().catch(() => {});
  try {
    try {
      current.stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch (error) {
      throw new Error(error && error.name === "NotAllowedError"
        ? "Microphone access was denied. Allow microphone for Telegram in iOS Settings, then tap Start."
        : "Microphone is unavailable. Check that no other app is using it, then tap Start.");
    }
    if (call !== current) { current.stream.getTracks().forEach((track) => track.stop()); return; }
    const peer = new RTCPeerConnection();
    current.peer = peer;
    current.stream.getAudioTracks().forEach((track) => peer.addTrack(track, current.stream));
    peer.ontrack = (event) => {
      remoteAudio.srcObject = event.streams[0] || new MediaStream([event.track]);
      remoteAudio.play().catch(() => {});
    };
    peer.onconnectionstatechange = () => {
      if (call !== current) return;
      if (peer.connectionState === "connected") { current.connected = true; render(); }
      if (peer.connectionState === "failed" || peer.connectionState === "closed") {
        end("Connection lost. Tap Start to try again.");
      }
    };
    current.channel = peer.createDataChannel("oai-events");
    current.channel.onmessage = (message) => {
      let event;
      try { event = JSON.parse(message.data); } catch (_) { return; }
      if (call === current && event && typeof event === "object") rejectDelegation(current, event);
    };
    await peer.setLocalDescription(await peer.createOffer());
    await waitForIce(peer);
    const answer = await requestAnswer(current, peer.localDescription.sdp);
    if (call !== current) return;
    await peer.setRemoteDescription({ type: "answer", sdp: answer });
  } catch (error) {
    if (call !== current) return;
    end(error && error.name === "AbortError" ? "" :
      error instanceof TypeError ? "Network error. Check your connection, then tap Start." :
      (error && error.message) || "Could not start the call. Tap Start to try again.");
  }
}

function toggleMute() {
  if (!call || !call.stream) return;
  call.muted = !call.muted;
  call.stream.getAudioTracks().forEach((track) => { track.enabled = !call.muted; });
  render();
}

startButton.addEventListener("click", start);
muteButton.addEventListener("click", toggleMute);
endButton.addEventListener("click", () => end(""));
window.addEventListener("pagehide", () => end(""));
render();
