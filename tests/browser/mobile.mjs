import {spawn} from 'node:child_process';
import {appendFileSync, createWriteStream, readFileSync, writeFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {chromium, webkit} from 'playwright';

const out = process.argv[2];
const engine = process.argv[3] || 'webkit';
const origin = 'http://127.0.0.1:8801';
const log = (step, data = {}) => appendFileSync(`${out}/mobile.jsonl`, JSON.stringify({step, ...data}) + '\n');
const assert = (value, message) => { if (!value) throw new Error(message); };
async function until(fn, message) {
  const deadline = Date.now() + 12000;
  do { if (await fn()) return; await new Promise(resolve => setTimeout(resolve, 80)); } while (Date.now() < deadline);
  throw new Error(message);
}
writeFileSync(`${out}/mobile.jsonl`, '');
const fixture = spawn('python3', ['/src/tests/browser/mobile-fixture.py'], {stdio: ['ignore', 'pipe', 'pipe']});
fixture.stderr.pipe(createWriteStream(`${out}/fixture.stderr`));
await new Promise((resolve, reject) => {
  const timer = setTimeout(() => reject(new Error('fixture startup timeout')), 10000);
  fixture.stdout.on('data', data => { if (String(data).includes('MOBILE FIXTURE READY')) { clearTimeout(timer); resolve(); } });
  fixture.on('exit', code => reject(new Error(`fixture exit ${code}`)));
});
const browser = await ({chromium, webkit}[engine]).launch();
log('runtime', {engine, version: browser.version(), playwright: JSON.parse(readFileSync('/app/node_modules/playwright/package.json')).version,
  source: process.env.TALK_SOURCE_REVISION, network: 'none', fixture: 'real backend, synthetic signed identity; microphone denial and pending permission are injected'});
const mode = async value => { const r = await fetch(`${origin}/fixture/mode`, {method: 'POST', body: JSON.stringify({mode: value})}); assert(r.ok, 'fixture mode'); };
const state = async () => (await fetch(`${origin}/fixture/state`)).json();
async function shot(page, name) {
  const path = `${out}/${name}.png`;
  await page.screenshot({path, fullPage: true});
  const bytes = readFileSync(path);
  assert(bytes.subarray(0, 8).equals(Buffer.from([137,80,78,71,13,10,26,10])), 'valid PNG');
  log('screenshot', {path, viewport: page.viewportSize(), source: process.env.TALK_SOURCE_REVISION, sha256: createHash('sha256').update(bytes).digest('hex')});
}
let passes = 0, failures = 0;
async function check(name, fn) {
  try { await fn(); passes++; log(name, {result: 'pass'}); }
  catch (error) { failures++; log(name, {result: 'fail', error: String(error.stack)}); }
}
try {
  for (const warm of [false, true]) for (const [width, height, orientation] of [[390, 690, 'portrait'], [844, 390, 'landscape']]) {
    const name = `${warm ? 'warm' : 'cold'}-${orientation}`;
    await check(name, async () => {
      const context = await browser.newContext({viewport: {width, height}, hasTouch: true});
      const errors = [];
      try {
        await context.addInitScript(() => {
          if (location.protocol === 'about:') return;
          window.__clicks = [];
          document.addEventListener('click', event => window.__clicks.push({id: event.target.id, trusted: event.isTrusted}), {capture: true});
          window.__gum = 0;
          // Keep the wrapper reachable: WebKit may collect an unreferenced navigator.mediaDevices wrapper, and with it
          // this expando, before the app asks for the microphone (then the native call answers and __gum stays 0).
          window.__mediaDevices = navigator.mediaDevices;
          navigator.mediaDevices.getUserMedia = async () => { window.__gum++; throw new DOMException('Synthetic denial', 'NotAllowedError'); };
        });
        const page = await context.newPage();
        page.on('pageerror', error => errors.push(String(error)));
        if (warm) {
          await mode('legacy');
          await page.goto(`${origin}/tg-voice/`);
          assert(await page.locator('#end').count() === 1, 'legacy page primed');
          await page.goto('about:blank');
        }
        const before = (await state()).requests.length;
        await mode('current');
        const identity = (await state()).initData;
        await page.goto(`${origin}/tg-voice/#tgWebAppData=${encodeURIComponent(identity)}`);
        await page.waitForTimeout(1700);
        const requests = (await state()).requests.slice(before);
        const geometry = await page.evaluate(() => ({header: document.querySelector('header') === null,
          face: document.getElementById('face').getBoundingClientRect().toJSON(),
          overflow: document.documentElement.scrollWidth > innerWidth, name: document.getElementById('name').textContent,
          avatar: !document.getElementById('avatar').hidden && document.getElementById('avatar').naturalWidth > 0,
          error: document.getElementById('error').textContent, gum: window.__gum,
          control: document.getElementById('start').getBoundingClientRect().toJSON()}));
        log(name + '-observed', {errors, requests, geometry});
        await shot(page, name);
        assert(errors.length === 0, `startup page errors: ${errors.join('; ')}`);
        assert(geometry.header && !geometry.overflow, 'app heading removed and bounded layout');
        assert(geometry.name === 'Synthetic Talk' && geometry.avatar, 'bootstrap replaces default name and paints configured avatar');
        assert(geometry.error.includes('Microphone access denied') && geometry.gum === 1, 'visible permission error');
        assert(geometry.control.width >= 44 && geometry.control.y >= 0 && geometry.control.y + geometry.control.height <= height, 'retry visible inside viewport');
        if (warm) {
          assert(!requests.some(path => path === '/tg-voice/app.js' || path === '/tg-voice/app.css'), 'old unversioned asset cache remains in use only for legacy');
          assert(requests.some(path => path.startsWith('/tg-voice/app.js?')) && requests.some(path => path.startsWith('/tg-voice/app.css?')), 'upgrade fetched distinct current assets');
        }
        const retried = page.waitForResponse(response => response.url().endsWith('/api/bootstrap'));
        await page.tap('#start');
        assert((await retried).ok(), 'retry reaches authenticated backend');
        await until(() => page.locator('#error').innerText().then(text => text.includes('Microphone access denied')), 'retry returns actionable denial');
        log(name + '-retry', {state: await page.evaluate(() => ({error: document.getElementById('error').textContent,
          phase: document.getElementById('orb').dataset.state, clicks: window.__clicks})), requests: (await state()).requests.slice(before), errors});
        assert(errors.length === 0, 'retry has no runtime errors');
        await page.setViewportSize({width: height, height: width});
        const box = await page.locator('#start').boundingBox();
        assert(box && box.y >= 0 && box.y + box.height <= width, 'retry remains visible after rotation');
      } finally { await context.close(); }
    });
  }
  await check('native-audio-unlock-and-page-lifecycle', async () => {
    await mode('current');
    const context = await browser.newContext({viewport: {width: 390, height: 690}, hasTouch: true});
    try {
      await context.addInitScript(() => {
        const NativeAudio = AudioContext;
        window.__audios = [];
        window.AudioContext = function(...args) { const ctx = new NativeAudio(...args); window.__audios.push(ctx); return ctx; };
        window.__mediaDevices = navigator.mediaDevices;  // pinned, as above
        // A denial answers every pending request and any later one: the restored document asks for the microphone
        // only after its bootstrap returns, which may be after the check denies.
        let denied = false;
        const pending = [];
        const deny = reject => reject(new DOMException('Synthetic denial', 'NotAllowedError'));
        navigator.mediaDevices.getUserMedia = () => new Promise((resolve, reject) => { if (denied) deny(reject); else pending.push(reject); });
        window.__denyMic = () => { denied = true; pending.splice(0).forEach(deny); };
      });
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', error => errors.push(String(error)));
      await page.goto(`${origin}/tg-voice/#tgWebAppData=${encodeURIComponent((await state()).initData)}`);
      await until(() => page.evaluate(() => document.getElementById('orb').dataset.state === 'blocked' && window.__audios[0].state === 'suspended'), 'native engine did not expose suspended audio');
      assert(await page.locator('#start').innerText() === 'Enable audio', 'audio unlock action');
      assert(await page.locator('#mute').isDisabled(), 'mic control blocked before audio unlock');
      await shot(page, 'enable-audio');
      await page.tap('#start');
      await until(() => page.evaluate(() => window.__audios[0].state === 'running'), 'trusted tap did not resume native audio');
      assert(await page.evaluate(() => window.__audios.length === 1), 'one audio context retained');
      await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
      assert(await page.evaluate(() => window.__audios[0].state === 'running'), 'visibility event preserves call');
      await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true})));
      await until(() => page.evaluate(() => window.__audios[0].state === 'closed'), 'page exit closes audio');
      await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true})));
      await until(() => page.evaluate(() => window.__audios.length === 2), 'persisted restore restarts');
      await page.evaluate(() => window.__denyMic());
      await until(() => page.locator('#error').innerText().then(text => text.includes('Microphone access denied')), 'restored document error visible');
      assert(errors.length === 0, errors.join('; '));
    } finally { await context.close(); }
  });
  await check('desktop-current-assets', async () => {
    const context = await browser.newContext({viewport: {width: 1280, height: 900}});
    try {
      const page = await context.newPage();
      await page.goto(`${origin}/tg-voice/`);
      await until(() => page.locator('#error').innerText().then(text => text.includes('Telegram')), 'missing-auth state');
      assert(await page.evaluate(() => document.querySelector('header') === null && document.documentElement.scrollWidth <= innerWidth), 'desktop layout');
      await page.addInitScript(() => {
        window.__mediaDevices = navigator.mediaDevices;  // pinned, as above
        navigator.mediaDevices.getUserMedia = async () => { throw new DOMException('Synthetic denial', 'NotAllowedError'); };
      });
      await page.goto(`${origin}/tg-voice/?desktop#tgWebAppData=${encodeURIComponent((await state()).initData)}`);
      await until(() => page.evaluate(() => !document.getElementById('avatar').hidden && document.getElementById('avatar').naturalWidth > 0), 'desktop configured avatar');
      await shot(page, 'desktop');
    } finally { await context.close(); }
  });
} finally {
  await browser.close();
  fixture.kill('SIGTERM');
}
log('summary', {passes, failures, mandatory: 6});
assert(passes + failures === 6, 'complete mobile inventory');
process.exitCode = failures ? 1 : 0;
