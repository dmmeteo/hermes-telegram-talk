import {chromium} from 'playwright';
import {spawn} from 'node:child_process';
import {appendFileSync} from 'node:fs';

const out = process.argv[2];
const base = '/tg-voice-michael/';
const origin = 'http://127.0.0.1:8801';
const log = (step, detail = {}) => appendFileSync(`${out}/talk-ui.jsonl`, JSON.stringify({step, ...detail}) + '\n');
const assert = (ok, message) => { if (!ok) throw new Error(message); };
const fixture = spawn('python3', ['/src/tests/browser/mobile-fixture.py'], {
  env: {...process.env, TALK_TEST_BASE: base}, stdio: ['ignore', 'pipe', 'inherit']});
await new Promise((resolve, reject) => {
  const timer = setTimeout(() => reject(new Error('fixture startup timeout')), 10000);
  fixture.stdout.on('data', bytes => { if (String(bytes).includes('MOBILE FIXTURE READY')) { clearTimeout(timer); resolve(); } });
  fixture.on('exit', code => reject(new Error(`fixture exit ${code}`)));
});
const browser = await chromium.launch();
const errors = [];
async function open(context) {
  const page = await context.newPage();
  page.on('pageerror', error => errors.push(String(error)));
  const state = await (await fetch(`${origin}/fixture/state`)).json();
  await page.goto(`${origin}${base}#tgWebAppData=${encodeURIComponent(state.initData)}`);
  return page;
}
const visibleAvatar = page => page.waitForFunction(() => {
  const image = document.getElementById('avatar'), box = image.getBoundingClientRect();
  return !image.hidden && image.naturalWidth > 0 && box.width > 40 && box.height > 40;
}, null, {timeout: 10000}).catch(() => { throw new Error('a decoded avatar at a custom base is visible'); });
try {
  const context = await browser.newContext({viewport: {width: 390, height: 844}, hasTouch: true});
  await context.addInitScript(() => { navigator.mediaDevices.getUserMedia = () => new Promise(() => {}); });
  const page = await open(context);
  await visibleAvatar(page);
  assert(await page.locator('#face').isHidden(), 'loaded avatar at a custom base replaces the inline face');
  await page.screenshot({path: `${out}/avatar-custom-base.png`});
  log('avatar-custom-base', {base});

  for (const type of ['image/png', 'image/jpeg']) {
    const bytes = await page.evaluate(type => {
      const canvas = document.createElement('canvas'); canvas.width = 80; canvas.height = 80;
      const paint = canvas.getContext('2d'); paint.fillStyle = '#4fc3f7'; paint.fillRect(0, 0, 80, 80);
      paint.fillStyle = '#ffb74d'; paint.fillRect(20, 20, 40, 40);
      return canvas.toDataURL(type).split(',')[1];
    }, type);
    await page.route('**/api/avatar', async route => {
      const authenticated = await route.fetch();
      assert(authenticated.ok(), 'synthetic avatar request passes real authentication');
      await route.fulfill({response: authenticated, contentType: type, body: Buffer.from(bytes, 'base64')});
    });
    await page.evaluate(() => refreshAvatar(call));
    await page.waitForFunction(() => {
      const image = document.getElementById('avatar'); return !image.hidden && image.naturalWidth === 80;
    });
    await page.unroute('**/api/avatar');
  }
  log('avatar-authenticated-blob', {types: ['image/png', 'image/jpeg']});

  await page.evaluate(() => { document.getElementById('avatar').src = 'data:image/png;base64,broken'; });
  await page.waitForFunction(() => document.getElementById('avatar').hidden);
  assert(await page.locator('#face').isVisible(), 'decode failure restores the neutral face');
  await page.evaluate(() => refreshAvatar(call));
  await page.waitForFunction(() => !document.getElementById('avatar').hidden);
  await page.route('**/api/avatar', route => route.fulfill({status: 503, body: ''}));
  await page.evaluate(() => refreshAvatar(call));
  assert(await page.locator('#avatar').isVisible(), 'a temporary refresh failure keeps the last good image');
  await page.unroute('**/api/avatar');

  const foreign = [];
  page.on('request', request => { if (!request.url().startsWith(`${origin}/`)) foreign.push(request.url()); });
  for (const url of ['https://example.invalid/avatar', '/another-profile/avatar', `${base}avatar?private=1`]) {
    await page.evaluate(url => applyProfile({profile: {display_name: 'Synthetic replacement', avatar_url: url},
      appearance: {listen_color: '#4fc3f7', speak_color: '#ffb74d'}}), url);
    assert(await page.locator('#avatar').isHidden() && await page.locator('#face').isVisible(), `${url} clears the old image and is not loaded`);
  }
  assert(foreign.length === 0, 'an untrusted profile image URL makes no cross-origin request');
  await page.route('**/api/bootstrap', () => new Promise(() => {}));
  await page.evaluate(() => refreshAvatar(call));
  await page.waitForFunction(() => !document.getElementById('avatar').hidden);
  await page.evaluate(() => { stop(); void start(); });
  assert(await page.locator('#avatar').isHidden() && await page.locator('#face').isVisible(), 'a new call clears the previous image before bootstrap');
  await page.unroute('**/api/bootstrap');
  log('avatar-replacement');
  await context.close();

  assert(errors.length === 0, `no uncaught browser errors: ${errors.join('; ')}`);
  log('summary', {failures: 0});
  console.log('TALK_UI_BROWSER_OK');
} finally {
  fixture.kill('SIGTERM');
  await browser.close();
}
