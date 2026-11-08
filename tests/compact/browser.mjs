import {chromium} from 'playwright';
import {spawn} from 'node:child_process';
import {appendFileSync, createWriteStream, readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';

const out = process.argv[2];
const log = (step, detail = {}) => appendFileSync(`${out}/compact.jsonl`, JSON.stringify({step, ...detail}) + '\n');
const assert = (ok, message) => { if (!ok) throw new Error(message); };
async function until(fn, message) {
  const end = Date.now() + 15000;
  do { if (await fn()) return; await new Promise(resolve => setTimeout(resolve, 80)); } while (Date.now() < end);
  throw new Error(message);
}
const browser = await chromium.launch({args: ['--disable-gpu', '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream', '--disable-features=WebRtcHideLocalIpsWithMdns', '--autoplay-policy=no-user-gesture-required', '--allow-loopback-in-peer-connection']});
log('runtime', {browser: browser.version(), revision: process.env.TALK_SOURCE_REVISION, media: 'synthetic oscillator through real WebRTC tracks and remote receiver analyser', network: 'none'});
let fixture;
const errors = [];
let checks = 0;
try {
  for (const profile of ['pewds', 'shopy']) {
    fixture = spawn('python3', ['/src/tests/browser/fixture.py'], {env: {...process.env, TALK_COMPACT_PROFILE: profile}, stdio: ['ignore', 'pipe', 'pipe']});
    fixture.stderr.pipe(createWriteStream(`${out}/${profile}-fixture.stderr`));
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('fixture startup')), 10000);
      fixture.stdout.on('data', chunk => { if (String(chunk).includes('TALK FIXTURE READY')) { clearTimeout(timer); resolve(); } });
      fixture.on('exit', code => reject(new Error(`fixture exit ${code}`)));
    });
    const context = await browser.newContext({viewport: {width: 390, height: 844}, hasTouch: true, permissions: ['microphone']});
    const provider = await context.newPage();
    await provider.goto('http://127.0.0.1:8802/provider.html');
    const identity = await (await fetch('http://127.0.0.1:8802/identity')).json();
    await context.addInitScript(() => {
      window.__streams = [];
      navigator.mediaDevices.getUserMedia = async () => {
        const audio = new AudioContext(); await audio.resume();
        const tone = audio.createOscillator(); const gain = audio.createGain(); const destination = audio.createMediaStreamDestination();
        gain.gain.value = 0; tone.frequency.value = 220; tone.connect(gain).connect(destination); tone.start();
        window.__mic = {audio, tone, gain}; window.__streams.push(destination.stream);
        return destination.stream;
      };
    });
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(String(error)));
    await page.route('https://telegram.org/**', route => route.fulfill({contentType: 'text/javascript', body: ''}));
    const base = 'http://127.0.0.1:8801/tg-voice/';
    const url = `${base}?launch=${identity.launch}#tgWebAppData=${encodeURIComponent(identity.initData)}`;
    await page.goto(url);
    const phase = () => page.locator('#orb').getAttribute('data-state');
    const enabled = () => page.evaluate(() => window.__streams.at(-1)?.getAudioTracks()[0]?.enabled || false);
    const shot = async name => {
      const path = `${out}/${profile}-${name}.png`; await page.screenshot({path});
      const bytes = readFileSync(path);
      assert(bytes.subarray(0, 8).equals(Buffer.from([137,80,78,71,13,10,26,10])), 'PNG signature');
      log('screenshot', {profile, name, viewport: page.viewportSize(), revision: process.env.TALK_SOURCE_REVISION, sha256: createHash('sha256').update(bytes).digest('hex')});
    };
    try {
      await until(async () => await enabled() && await phase() === 'silent', 'connected silence is neutral');
    } catch (error) {
      log('startup-failure', {profile, errors, state: await page.evaluate(() => ({phase: call?.phase, error: document.getElementById('error').textContent, peer: call?.peer?.connectionState, stages: call?.stages, level: call?.outputLevel, playing: call?.playing, audio: call?.audio?.state, ice: call?.peer?.iceConnectionState, local: call?.peer?.localDescription?.sdp, remote: call?.peer?.remoteDescription?.sdp}))});
      await shot('startup-failure'); throw error;
    }
    const index = await provider.evaluate(() => window.peers.length - 1);
    const energy = () => provider.evaluate(i => window.peers[i].energy?.() || 0, index);
    assert(await page.locator('header').count() === 0, 'no app heading');
    assert(await page.locator('#thread-note').isHidden(), 'linked call has no unlinked warning');
    await shot('linked-silent'); checks++;
    await page.evaluate(() => { window.__mic.gain.gain.value = .15; });
    try {
      await until(async () => await phase() === 'listening' && await energy() > .01, 'blue requires actual outbound microphone speech energy');
    } catch (error) {
      log('outbound-observation', {app: await page.evaluate(() => ({phase:call.phase,input:call.inputLevel,output:call.outputLevel,track:call.stream.getAudioTracks()[0].enabled,gain:window.__mic.gain.gain.value,sourceAudio:window.__mic.audio.state})), receiver: await provider.evaluate(async i => ({audio:window.peers[i].audio?.state,paused:window.peers[i].receivedAudio?.paused,energy:window.peers[i].energy?.(),input:!!window.peers[i].input,stats:[...(await window.peers[i].peer.getStats()).values()].filter(r=>r.type==='inbound-rtp')}), index)});
      throw error;
    }
    await shot('listening'); checks++;
    await page.click('#mute');
    await until(async () => !await enabled() && await phase() === 'muted' && await energy() < .001, 'muted outbound audio is silent at receiver');
    await shot('muted');
    const contrast = await page.locator('#mute').evaluate(button => {
      const style = getComputedStyle(button), icon = getComputedStyle(button.querySelector('svg'));
      const rgb = value => value.match(/[\d.]+/g).slice(0, 3).map(Number);
      const bg = rgb(style.backgroundColor), ink = rgb(icon.color), alpha = Number(icon.opacity);
      const effective = ink.map((v, i) => v * alpha + bg[i] * (1 - alpha));
      const lum = values => values.map(v => v / 255).map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4)
        .reduce((sum, v, i) => sum + v * [.2126, .7152, .0722][i], 0);
      const a = lum(effective), b = lum(bg);
      return {ratio: (Math.max(a, b) + .05) / (Math.min(a, b) + .05), foreground: icon.color, background: style.backgroundColor, opacity: alpha};
    });
    log('pressed-hover-contrast', {profile, ...contrast});
    assert(contrast.ratio >= 3, 'pressed microphone hover icon has at least 3:1 contrast');
    await provider.evaluate(i => { window.peers[i].gain.gain.value = .18; }, index);
    await until(() => page.evaluate(() => call.outputLevel > .015), 'assistant output still plays while microphone muted');
    await until(async () => await phase() === 'speaking' && !await enabled() && await energy() < .001, 'muted microphone stays silent while actual assistant output turns ring orange');
    assert(await page.evaluate(() => !document.getElementById('remote').muted && !document.getElementById('remote').paused), 'mute leaves assistant output playing');
    await shot('speaking-mic-muted'); checks++;
    await page.click('#mute');
    await until(async () => await phase() === 'speaking' && await enabled(), 'orange requires assistant output');
    await shot('speaking'); checks++;
    await provider.evaluate(i => { window.peers[i].gain.gain.value = 0; }, index);
    for (let n = 0; n < 32; n++) await provider.evaluate(({i, n}) => window.peers[i].send({type: 'turn.done', turn: {id: `compact-${n}`, role: n % 2 ? 'assistant' : 'user', transcript: `Synthetic ${n}. A longer dialogue should remain readable on a small phone. `.repeat(2)}}), {i: index, n});
    await until(() => page.locator('#transcript li').count().then(count => count >= 32), 'long dialogue settled');
    for (const [width, height, name] of [[390,844,'long-portrait'], [320,568,'long-narrow'], [844,390,'long-landscape']]) {
      await page.setViewportSize({width, height});
      const result = await page.evaluate(() => {
        const list = document.getElementById('transcript'), row = document.querySelector('.control-row');
        const r = row.getBoundingClientRect(), t = list.getBoundingClientRect();
        return {overflow: document.documentElement.scrollWidth > innerWidth, transcriptHeight: t.height, bottom: r.bottom, end: t.bottom, rowTop: r.top, viewport: innerHeight};
      });
      assert(!result.overflow && result.bottom <= height && result.end <= result.rowTop && result.transcriptHeight > height * .45, 'dialogue owns available height above persistent controls');
      await shot(name); checks++;
    }
    await page.setViewportSize({width:390,height:844});
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await page.locator('#transcript').hover();
    await page.mouse.wheel(0, -10000);
    await until(() => page.evaluate(() => !followingTranscript && transcriptList.scrollTop < 10), 'reader reaches older messages after viewport settles');
    await provider.evaluate(i => window.peers[i].send({type:'turn.done',turn:{id:'while-reading',role:'assistant',transcript:'Synthetic new message while reading older dialogue.'}}), index);
    await until(() => page.locator('#transcript li').last().innerText().then(text => text.includes('while reading')), 'new message arrived');
    assert(await page.locator('#transcript').evaluate(el => el.scrollTop < 10), 'new message preserves reading position');
    await page.setViewportSize({width:360,height:640});
    assert(await page.locator('#transcript').evaluate(el => el.scrollTop < 10), 'rotation preserves reading position'); checks++;
    await page.click('#menu-toggle');
    assert(await page.locator('#voice').isVisible(), 'voice selector is in menu');
    assert(await page.locator('#voice option').count() === 9, 'complete native subscription nine');
    assert(await page.locator('#voice').inputValue() === (profile === 'shopy' ? 'sol' : 'cove'), 'profile default preserved');
    await shot('voice-menu');
    await page.keyboard.press('Escape');
    assert(await page.locator('#call-menu').isHidden(), 'Escape closes menu'); checks++;
    await page.click('#mute');
    await page.click('#menu-toggle');
    await page.selectOption('#voice', profile === 'shopy' ? 'cove' : 'sol');
    await until(() => page.locator('#reconnect').isVisible(), 'explicit reconnect contract');
    await page.click('#reconnect');
    try { await until(() => page.evaluate(() => call?.phase === 'ready'), 'reconnected'); }
    catch (error) {
      log('reconnect-failure', {profile, state: await page.evaluate(() => ({phase:call?.phase, error:document.getElementById('error').textContent, peer:call?.peer?.connectionState, stages:call?.stages, playing:call?.playing, audio:call?.audio?.state}))});
      throw error;
    }
    assert(!await enabled() && await phase() === 'muted', 'mute survives voice reconnect'); checks++;
    await page.evaluate(() => { document.getElementById('remote').pause(); });
    await until(async () => await phase() === 'blocked', 'blocked playback reflected');
    assert(!await enabled(), 'blocked playback cannot send microphone');
    await shot('blocked'); checks++;
    await page.click('#menu-toggle');
    await page.click('#end');
    await until(async () => await phase() === 'idle', 'end call');
    assert(await page.evaluate(() => window.__streams.every(s => s.getTracks().every(t => t.readyState === 'ended'))), 'end stops real microphone tracks');
    await shot('disconnected'); checks++;
    await page.goto(`${base}#tgWebAppData=${encodeURIComponent(identity.initData)}`);
    await until(() => page.locator('#thread-note').isVisible(), 'menu launch has no guessed binding');
    assert((await page.locator('#thread-note').innerText()).includes('Not linked to a chat'), 'truthful unlinked note');
    await shot('unlinked'); checks++;
    await context.close();
    fixture.kill('SIGTERM'); await new Promise(resolve => fixture.once('exit', resolve)); fixture = null;
    log('profile-complete', {profile, checks});
  }
  assert(errors.length === 0, `uncaught errors: ${errors.join('; ')}`);
  log('summary', {checks, profiles: 2, failures: 0});
  console.log('TALK_COMPACT_BROWSER_OK');
} finally {
  fixture?.kill('SIGTERM'); await browser.close();
}
