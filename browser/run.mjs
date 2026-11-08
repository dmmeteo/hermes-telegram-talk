// Offline browser evidence: real Chromium, real static page and server, fake provider. Not live voice proof.
import { spawn } from "node:child_process";
import { writeFileSync, appendFileSync } from "node:fs";
import { chromium } from "playwright";

const OUT = "/output";
const LOG = `${OUT}/browser.log`;
const APP = "http://127.0.0.1:8801/tg-voice/";
const FIXTURE = "http://127.0.0.1:8802";
writeFileSync(LOG, "");
const log = (step, detail = {}) => appendFileSync(LOG, JSON.stringify({ t: new Date().toISOString(), step, ...detail }) + "\n");
let failed = 0;
async function step(name, fn) {
  try { log(name, { result: "pass", ...(await fn()) }); }
  catch (error) { failed += 1; log(name, { result: "fail", error: String(error && error.stack || error).slice(0, 600) }); }
}
function assert(cond, message) { if (!cond) throw new Error(message); }
async function until(fn, message, ms = 15000) {
  const deadline = Date.now() + ms;
  for (;;) {
    const value = await fn();
    if (value) return value;
    if (Date.now() > deadline) throw new Error(`timeout: ${message}`);
    await new Promise((r) => setTimeout(r, 150));
  }
}

const fixture = spawn("python3", ["/src/browser/fixture.py"], { stdio: ["ignore", "pipe", "pipe"] });
fixture.stderr.on("data", (d) => log("fixture-stderr", { text: String(d).slice(0, 400) }));
await new Promise((resolve, reject) => {
  fixture.stdout.on("data", (d) => String(d).includes("FIXTURE READY") && resolve());
  fixture.on("exit", (code) => reject(new Error(`fixture exited ${code}`)));
  setTimeout(() => reject(new Error("fixture start timeout")), 15000);
});

const browser = await chromium.launch({
  // This headless Chromium returns NotSupportedError from getUserMedia without the fake UI flag.
  args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "--allow-loopback-in-peer-connection",
         "--disable-features=WebRtcHideLocalIpsWithMdns", "--autoplay-policy=no-user-gesture-required"],
});
log("browser", { version: browser.version(), network: "container --network none" });

const providerContext = await browser.newContext();
const provider = await providerContext.newPage();
await provider.goto(`${FIXTURE}/provider.html`);

const { initData } = await (await fetch(`${FIXTURE}/initdata`)).json();
const appUrl = `${APP}#tgWebAppData=${encodeURIComponent(initData)}&tgWebAppVersion=8.0&tgWebAppPlatform=ios`;

const instrument = () => {
  if (!navigator.mediaDevices) return;
  window.__pcs = [];
  window.__streams = [];
  window.__gum = 0;
  window.__gumErrors = [];
  window.__denyNext = false;
  const Native = window.RTCPeerConnection;
  window.RTCPeerConnection = function (...args) {
    const pc = new Native(...args);
    const nativeCreate = pc.createDataChannel.bind(pc);
    pc.createDataChannel = (...a) => { const dc = nativeCreate(...a); pc.__dc = dc; return dc; };
    window.__pcs.push(pc);
    return pc;
  };
  window.RTCPeerConnection.prototype = Native.prototype;
  const gum = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
  navigator.mediaDevices.getUserMedia = async (c) => {
    window.__gum += 1;
    try {
      // Fixture-only: one injected denial, since fake-UI Chromium ignores permission denial.
      if (window.__denyNext) { window.__denyNext = false; throw new DOMException("Permission denied", "NotAllowedError"); }
      const s = await gum(c); window.__streams.push(s); return s;
    } catch (e) { window.__gumErrors.push(e.name); throw e; }
  };
};

const context = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2,
  isMobile: true, hasTouch: true, permissions: ["microphone"] });
await context.addInitScript(instrument);
const page = await context.newPage();
const consoleErrors = [];
page.on("console", (m) => m.type() === "error" && consoleErrors.push(m.text().slice(0, 200)));
page.on("pageerror", (e) => consoleErrors.push(`pageerror: ${String(e).slice(0, 200)}`));
await page.route("https://telegram.org/**", (r) => r.abort("internetdisconnected"));

const ui = () => page.evaluate(() => ({
  start: [document.getElementById("start").disabled, document.getElementById("start").textContent],
  mute: [document.getElementById("mute").disabled, document.getElementById("mute").textContent,
         document.getElementById("mute").getAttribute("aria-pressed")],
  end: document.getElementById("end").disabled,
  error: document.getElementById("error").hidden ? "" : document.getElementById("error").textContent,
  buttons: [...document.querySelectorAll("button")].map((b) => b.textContent),
}));
const live = () => page.evaluate(() => ({
  pcs: window.__pcs.map((pc) => [pc.connectionState, pc.signalingState, pc.__dc ? pc.__dc.readyState : "none"]),
  liveMic: window.__streams.flatMap((s) => s.getTracks()).filter((t) => t.readyState === "live").length,
  gum: window.__gum,
  audio: (() => { const a = document.getElementById("remote"); const s = a.srcObject;
    return { paused: a.paused, tracks: s ? s.getAudioTracks().map((t) => t.readyState) : [] }; })(),
}));
async function inboundBytes() {
  return page.evaluate(async () => {
    const pc = window.__pcs[window.__pcs.length - 1];
    let bytes = 0;
    (await pc.getStats()).forEach((r) => { if (r.type === "inbound-rtp" && r.kind === "audio") bytes += r.bytesReceived; });
    return bytes;
  });
}
async function connectCall(label) {
  const before = (await live()).pcs.length;
  await page.click("#start");
  await until(async () => (await live()).pcs.length === before + 1, `${label} peer created`);
  await until(async () => (await live()).pcs[before][0] === "connected", `${label} connected`);
  await until(async () => !(await ui()).mute[0], `${label} mute enabled`);
  await until(async () => (await inboundBytes()) > 2000, `${label} inbound audio bytes`);
  const state = await live();
  assert(state.audio.tracks.length === 1 && state.audio.tracks[0] === "live" && !state.audio.paused, `${label} remote audio playing`);
  return { inbound_bytes: await inboundBytes(), ui: await ui() };
}
async function assertClosed(label) {
  const state = await until(async () => {
    const s = await live();
    return s.liveMic === 0 && s.pcs.every(([c, sig, dc]) => c === "closed" && sig === "closed" && ["closed", "none"].includes(dc)) && s;
  }, `${label} everything closed`, 5000);
  assert(state.audio.tracks.length === 0, `${label} remote audio detached`);
  const u = await ui();
  assert(!u.start[0] && u.mute[0] && u.end, `${label} idle buttons`);
  return { state, ui: u };
}

await step("load-initial-idle", async () => {
  await page.goto(appUrl);
  await page.waitForLoadState("networkidle");
  const u = await ui();
  const l = await live();
  assert(JSON.stringify(u.buttons) === JSON.stringify(["Start", "Mute", "End"]), "only Start/Mute/End");
  assert(!u.start[0] && u.mute[0] && u.end && u.error === "", "initial idle state");
  assert(l.gum === 0 && l.pcs.length === 0 && l.liveMic === 0, "not recording before Start");
  await page.screenshot({ path: `${OUT}/mobile-390-idle.png` });
  return { ui: u, live: l };
});

await step("start-connects-and-plays-remote-audio", async () => {
  const r = await connectCall("call1");
  await page.screenshot({ path: `${OUT}/mobile-390-in-call.png` });
  return r;
});

await step("delegation-rejected-honestly", async () => {
  const got = await until(() => provider.evaluate(() => {
    const e = window.peers[window.peers.length - 1];
    return e && e.got.length >= 2 && e.got;
  }), "provider received function_call_output", 8000);
  const output = got.find((e) => e.type === "conversation.item.create");
  assert(output && output.item.type === "function_call_output" && output.item.call_id === "call_fixture", "function_call_output sent");
  assert(/nothing was executed/.test(output.item.output), "honest no-backend output");
  return { types: got.map((e) => e.type) };
});

await step("mute-toggles-track", async () => {
  await page.click("#mute");
  const muted = await page.evaluate(() => window.__streams.at(-1).getAudioTracks().map((t) => t.enabled));
  const u1 = await ui();
  assert(muted.every((e) => e === false) && u1.mute[1] === "Unmute" && u1.mute[2] === "true", "muted");
  await page.click("#mute");
  const unmuted = await page.evaluate(() => window.__streams.at(-1).getAudioTracks().map((t) => t.enabled));
  const u2 = await ui();
  assert(unmuted.every((e) => e === true) && u2.mute[1] === "Mute" && u2.mute[2] === "false", "unmuted");
  return { muted, unmuted };
});

await step("end-closes-everything", async () => {
  await page.click("#end");
  const r = await assertClosed("end1");
  const providerChannel = await until(() => provider.evaluate(() => window.peers[0].channelState === "closed" && "closed"),
    "provider saw data channel close", 8000);
  return { ...r, providerChannel };
});

await step("restart-twice", async () => {
  const results = [];
  for (const n of [2, 3]) {
    results.push(await connectCall(`call${n}`));
    await page.click("#end");
    results.push(await assertClosed(`end${n}`));
  }
  return { cycles: 2, inbound: results.filter((r) => r.inbound_bytes).map((r) => r.inbound_bytes) };
});

await step("network-failure-recoverable", async () => {
  await page.route("**/tg-voice/api/session", (r) => r.abort("internetdisconnected"));
  await page.click("#start");
  const u = await until(async () => { const x = await ui(); return x.error && x; }, "network error shown");
  assert(/Network error/.test(u.error), `actionable network error: ${u.error}`);
  await assertClosed("after-network-failure");
  await page.screenshot({ path: `${OUT}/mobile-390-network-error.png` });
  await page.unroute("**/tg-voice/api/session");
  const r = await connectCall("after-network-recovery");
  assert((await ui()).error === "", "error cleared on recovery");
  await page.click("#end");
  await assertClosed("end-after-network-recovery");
  return { error: u.error, recovered_inbound_bytes: r.inbound_bytes };
});

await step("server-rejection-shown", async () => {
  await page.route("**/tg-voice/api/session", (r) => r.continue({ postData: JSON.stringify({ initData: "", sdp: "v=0" }) }));
  await page.click("#start");
  const u = await until(async () => { const x = await ui(); return x.error && x; }, "server error shown");
  await page.unroute("**/tg-voice/api/session");
  assert(/reopen the voice test/i.test(u.error), `server 401 message surfaced: ${u.error}`);
  await assertClosed("after-401");
  return { error: u.error };
});

await step("permission-denied-recoverable-same-page", async () => {
  await page.evaluate(() => { window.__denyNext = true; });
  const before = await live();
  await page.click("#start");
  const u = await until(async () => { const x = await ui(); return x.error && x; }, "permission error shown");
  assert(/Microphone access was denied/.test(u.error), `actionable permission error: ${u.error}`);
  const after = await live();
  assert(after.pcs.length === before.pcs.length, "no peer created after denial");
  await assertClosed("after-denial");
  await page.screenshot({ path: `${OUT}/mobile-390-permission-error.png` });
  const r = await connectCall("after-permission-grant");
  await page.click("#end");
  await assertClosed("end-after-grant");
  return { error: u.error, recovered_inbound_bytes: r.inbound_bytes };
});

await step("real-chromium-permission-denial", async () => {
  // Full Chromium channel: the headless shell reports NotSupportedError instead of a real denial.
  const denying = await chromium.launch({ channel: "chromium", args: ["--use-fake-device-for-media-stream", "--deny-permission-prompts"] });
  const ctx = await denying.newContext({ viewport: { width: 390, height: 844 } });
  await ctx.addInitScript(instrument);
  const p = await ctx.newPage();
  await p.route("https://telegram.org/**", (r) => r.abort("internetdisconnected"));
  await p.goto(appUrl);
  const errors = [];
  for (const attempt of [1, 2]) {
    await p.click("#start");
    const err = await until(() => p.evaluate(() => !document.getElementById("error").hidden && document.getElementById("error").textContent), `denial error ${attempt}`);
    assert(/Microphone access was denied/.test(err), err);
    const st = await p.evaluate(() => ({ pcs: window.__pcs.length, gumErrors: window.__gumErrors,
      start: document.getElementById("start").disabled, end: document.getElementById("end").disabled }));
    assert(st.pcs === 0 && !st.start && st.end, `idle after denial ${JSON.stringify(st)}`);
    errors.push({ err, ...st });
  }
  assert(errors[1].gumErrors.every((n) => n === "NotAllowedError"), `real NotAllowedError: ${errors[1].gumErrors}`);
  await denying.close();
  return { attempts: errors };
});

await step("end-while-connecting-cancels", async () => {
  await page.route("**/tg-voice/api/session", async (r) => { await new Promise((x) => setTimeout(x, 1500)); r.continue().catch(() => {}); });
  await page.click("#start");
  await until(async () => (await ui()).start[1] === "Starting…", "loading state");
  await page.click("#end");
  await page.unroute("**/tg-voice/api/session");
  await page.waitForTimeout(2500);
  const r = await assertClosed("cancel");
  assert(r.ui.error === "", "no error after user cancel");
  return r;
});

await step("pagehide-closes-everything", async () => {
  await connectCall("before-pagehide");
  await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent("pagehide", { persisted: false })));
  return assertClosed("pagehide");
});

await step("navigation-away-closes-upstream", async () => {
  await connectCall("before-navigation");
  const idx = await provider.evaluate(() => window.peers.length - 1);
  await page.goto("about:blank");
  const closed = await until(() => provider.evaluate((i) => window.peers[i].channelState === "closed"
    || ["closed", "disconnected", "failed"].includes(window.peers[i].peer.connectionState), idx),
    "provider saw peer leave after navigation", 15000);
  return { provider_side: closed };
});

await step("desktop-screenshot", async () => {
  const desktop = await browser.newContext({ viewport: { width: 1280, height: 800 }, permissions: ["microphone"] });
  const d = await desktop.newPage();
  await d.route("https://telegram.org/**", (r) => r.abort("internetdisconnected"));
  await d.goto(appUrl);
  await d.screenshot({ path: `${OUT}/desktop-1280-idle.png` });
  await desktop.close();
  return {};
});

await step("no-auth-bypass-in-plain-browser", async () => {
  const plain = await browser.newContext({ permissions: ["microphone"] });
  const p = await plain.newPage();
  await p.route("https://telegram.org/**", (r) => r.abort("internetdisconnected"));
  await p.goto(APP);
  await p.click("#start");
  const err = await until(() => p.evaluate(() => !document.getElementById("error").hidden && document.getElementById("error").textContent), "no-initData error");
  assert(/Open this page from the Pewds bot/.test(err), err);
  await plain.close();
  return { error: err };
});

await step("server-log-clean", async () => {
  const state = await (await fetch(`${FIXTURE}/state`)).json();
  for (const secret of [state.synthetic_token, state.bot_token, "v=0", "hash=", "fixture-acct", "a=ice"]) {
    assert(!state.log.includes(secret), `log contains ${secret}`);
  }
  assert(state.log.includes("POST /tg-voice/api/session 200"), "request lines logged");
  return { resolves: state.resolves, posts: state.posts, log_lines: state.log.trim().split("\n").length };
});

log("console-errors", { errors: consoleErrors });
const unexpected = consoleErrors.filter((e) => !/telegram\.org|ERR_INTERNET_DISCONNECTED|Failed to load resource/.test(e));
if (unexpected.length) { failed += 1; log("unexpected-console-errors", { result: "fail", unexpected }); }
await browser.close();
fixture.kill();
log("summary", { failed });
process.exit(failed ? 1 : 0);
