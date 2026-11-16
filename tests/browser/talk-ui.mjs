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

  const panelContext = await browser.newContext({viewport: {width: 390, height: 844}, hasTouch: true});
  await panelContext.addInitScript(() => { navigator.mediaDevices.getUserMedia = () => new Promise(() => {}); });
  const panel = await open(panelContext);
  await panel.waitForFunction(() => !document.getElementById('voice-box').hidden);
  const inPanel = () => panel.evaluate(() => document.getElementById('call-menu').contains(document.activeElement));
  const closedWithFocusOn = id => panel.evaluate(id => document.getElementById('call-menu').hidden
    && document.getElementById('menu-toggle').getAttribute('aria-expanded') === 'false'
    && (id === null ? !document.getElementById('call-menu').contains(document.activeElement) : document.activeElement?.id === id), id);
  const states = {
    connecting: () => {},
    blocked: () => { call.phase = 'blocked'; render(); },
    stopped: () => stop('Voice did not become ready. Tap Retry to reconnect.'),
  };
  const contrast = () => panel.evaluate(() => {
    const parse = value => { const n = value.match(/[\d.]+/g)?.map(Number) ?? []; return n.length < 3 ? null : [n[0], n[1], n[2], n.length > 3 ? n[3] : 1]; };
    const over = (top, base) => top.slice(0, 3).map((v, i) => v * top[3] + base[i] * (1 - top[3]));
    const lum = rgb => rgb.map(v => v / 255).map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4).reduce((sum, v, i) => sum + v * [.2126, .7152, .0722][i], 0);
    const ratio = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + .05) / (y + .05); };
    const bases = element => {
      const layers = [];
      const settle = base => layers.reduceRight((below, layer) => over(layer, below), base);
      for (let node = element; node; node = node.parentElement) {
        const style = getComputedStyle(node);
        if (style.backgroundImage.includes('gradient')) return [...style.backgroundImage.matchAll(/rgba?\([^)]*\)/g)].map(m => settle(parse(m[0]).slice(0, 3)));
        const color = parse(style.backgroundColor);
        if (color && color[3] >= 1) return [settle(color.slice(0, 3))];
        if (color && color[3] > 0) layers.push(color);
      }
      return [settle([255, 255, 255])];
    };
    const texts = [...document.body.querySelectorAll('*')].filter(element => element.getClientRects().length
      && !element.closest(':disabled, [hidden], svg') && getComputedStyle(element).visibility === 'visible'
      && ([...element.childNodes].some(node => node.nodeType === 3 && node.textContent.trim()) || element.matches('select')));
    return texts.map(element => {
      const ink = parse(getComputedStyle(element).color);
      const worst = Math.min(...bases(element).map(base => ratio(over(ink, base), base)));
      return {text: (element.id || element.textContent.trim()).slice(0, 40), ratio: Math.round(worst * 100) / 100};
    });
  });
  for (const theme of ['modern', 'typesafe']) {
  await panel.evaluate(theme => document.getElementById(`theme-${theme}`).click(), theme);
  for (const [state, enter] of Object.entries(states)) {
    await panel.evaluate(enter);
    await panel.evaluate(() => {
      transcriptFinal('user', `synthetic-${Math.random()}`, 'Synthetic question about the settings panel.', false);
      transcriptFinal('assistant', `synthetic-${Math.random()}`, 'Synthetic answer, cut short.', true);
      transcriptFragment('assistant', 'Synthetic interim reply');
    });
    for (const [width, height] of [[390, 844], [360, 640], [320, 568], [844, 390], [1280, 800]]) {
      await panel.setViewportSize({width, height});
      await panel.click('#menu-toggle');
      const g = await panel.evaluate(() => {
        const box = element => element.getBoundingClientRect().toJSON();
        const menu = document.getElementById('call-menu');
        const targets = [...menu.querySelectorAll('button, select, label:has(input)')].filter(element => element.getClientRects().length)
          .map(element => { const r = element.getBoundingClientRect();
            return {id: element.id || element.textContent.trim(), width: r.width, height: r.height,
              hit: element.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2))}; });
        return {panel: box(menu), heading: box(document.getElementById('menu-title') || menu), row: box(document.querySelector('.control-row')),
          order: ['menu-toggle', 'orb', 'mute'].map(id => box(document.getElementById(id)).x), targets,
          overflow: document.documentElement.scrollWidth > innerWidth, status: !document.getElementById('call-status').hidden,
          lines: ['error', 'start'].filter(id => !document.getElementById(id).hidden)};
      });
      const where = `${theme} ${state} ${width}x${height}`;
      assert(g.panel.top >= 0 && g.panel.bottom <= height && g.panel.left >= 0 && g.panel.right <= width && !g.overflow, `panel fits the viewport (${where})`);
      assert(g.heading.top >= g.panel.top && g.heading.top >= 0, `panel heading is not clipped (${where})`);
      assert(g.panel.bottom <= g.row.top, `panel sits above the control row (${where})`);
      assert(g.order[0] < g.order[1] && g.order[1] < g.order[2], `menu left, avatar center, microphone right (${where})`);
      assert(g.targets.every(t => t.width >= 44 && t.height >= 44 && t.hit), `panel controls are reachable 44 px targets (${where}): ${JSON.stringify(g.targets)}`);
      assert(await inPanel(), `opening moves focus into the panel (${where})`);
      await panel.screenshot({path: `${out}/menu-${theme}-${state}-${width}x${height}.png`});
      if (width === 390) {
        const texts = await contrast();
        const low = texts.filter(text => text.ratio < 4.5);
        assert(texts.length > 8 && low.length === 0, `text contrast is at least 4.5:1 (${where}): ${JSON.stringify(low)}`);
        log('contrast', {theme, state, checked: texts.length, lowest: Math.min(...texts.map(text => text.ratio))});
      }
      log('panel-geometry', {theme, state, width, height, panel: g.panel, row: g.row.top, lines: g.lines});
      await panel.keyboard.press('Escape');
      assert(await closedWithFocusOn('menu-toggle'), `Escape closes and restores focus (${where})`);
    }
    await panel.evaluate(() => { stop(); void start(); });
    await panel.waitForFunction(() => call && !document.getElementById('voice-box').hidden);
  }
  }
  await panel.evaluate(() => document.getElementById('theme-modern').click());
  await panel.setViewportSize({width: 390, height: 844});
  await panel.evaluate(() => { stop(); void start(); });
  await panel.waitForFunction(() => call && !document.getElementById('end').disabled);
  await panel.focus('#menu-toggle'); await panel.keyboard.press('Enter');
  assert(await inPanel(), 'keyboard opening moves focus into the panel');
  await panel.click('#menu-close');
  assert(await closedWithFocusOn('menu-toggle'), 'Close closes and restores focus');
  await panel.click('#menu-toggle'); await panel.click('#menu-toggle');
  assert(await closedWithFocusOn('menu-toggle'), 'the toggle closes an open panel');
  await panel.click('#menu-toggle'); await panel.mouse.click(20, 20);
  assert(await closedWithFocusOn(null), 'an outside pointer dismisses');
  await panel.click('#menu-toggle');
  await panel.focus('#end'); await panel.keyboard.press('Tab');
  assert(await closedWithFocusOn(null), 'Tab from the last control leaves the panel and closes it');
  await panel.evaluate(() => { call.phase = 'ready'; render(); });
  await panel.click('#menu-toggle');
  await panel.focus('#end'); await panel.keyboard.press('Tab');
  assert(await closedWithFocusOn('mute'), 'Tab from the last control moves to the microphone and closes the panel');
  await panel.click('#menu-toggle');
  const other = await panel.evaluate(() => [...document.querySelectorAll('#voice option')].map(o => o.value).find(v => v !== voiceState.current));
  await panel.focus('#voice'); await panel.selectOption('#voice', other);
  await panel.locator('#reconnect').waitFor({state: 'visible', timeout: 10000}).catch(() => {});
  assert(await panel.evaluate(() => !document.getElementById('call-menu').hidden) && await panel.locator('#reconnect').isVisible(),
    'choosing a voice keeps the panel open and shows Reconnect');
  await panel.keyboard.press('Escape');
  await panel.click('#menu-toggle');
  await panel.focus('#menu-close'); await panel.keyboard.press('Shift+Tab');
  assert(await panel.evaluate(() => !document.getElementById('call-menu').hidden && document.activeElement?.id === 'menu-toggle'),
    'Shift+Tab from the first control returns to the toggle, which still controls the open panel');
  await panel.keyboard.press('Enter');
  assert(await closedWithFocusOn('menu-toggle'), 'the toggle then closes the panel');
  log('panel-keyboard');
  await panelContext.close();

  const themed = await browser.newContext({viewport: {width: 390, height: 844}, hasTouch: true});
  await themed.addInitScript(() => {
    navigator.mediaDevices.getUserMedia = () => new Promise(() => {});
    document.addEventListener('DOMContentLoaded', () => { window.__themeAtReady = document.documentElement.dataset.theme; });
  });
  const look = page => page.evaluate(() => ({theme: document.documentElement.dataset.theme, ready: window.__themeAtReady,
    meta: document.querySelector('meta[name="theme-color"]').content, checked: document.querySelector('input[name="theme"]:checked')?.value,
    background: getComputedStyle(document.body).backgroundColor, image: getComputedStyle(document.body).backgroundImage}));
  const page2 = await open(themed);
  let seen = await look(page2);
  assert(seen.theme === 'modern' && seen.ready === 'modern' && seen.meta === '#30302e' && seen.checked === 'modern'
    && seen.background === 'rgb(48, 48, 46)', `Modern is the default: ${JSON.stringify(seen)}`);
  await page2.click('#menu-toggle'); await page2.getByText('Retro 95').click();
  seen = await look(page2);
  assert(seen.theme === 'typesafe' && seen.meta === '#b9dff8' && seen.checked === 'typesafe' && seen.image.includes('gradient'), `Retro 95 applies at once: ${JSON.stringify(seen)}`);
  await page2.screenshot({path: `${out}/theme-typesafe-menu.png`});
  await page2.reload();
  seen = await look(page2);
  assert(seen.theme === 'typesafe' && seen.ready === 'typesafe' && seen.checked === 'typesafe' && seen.meta === '#b9dff8', `Retro 95 persists across reload, applied before DOMContentLoaded: ${JSON.stringify(seen)}`);
  await page2.click('#menu-toggle'); await page2.getByText('Modern').click(); await page2.reload();
  seen = await look(page2);
  assert(seen.theme === 'modern' && seen.ready === 'modern' && seen.checked === 'modern', `Modern persists across reload: ${JSON.stringify(seen)}`);
  await themed.close();
  const blocked = await browser.newContext({viewport: {width: 390, height: 844}, hasTouch: true});
  await blocked.addInitScript(() => {
    navigator.mediaDevices.getUserMedia = () => new Promise(() => {});
    Object.defineProperty(window, 'localStorage', {get() { throw new DOMException('Synthetic storage block', 'SecurityError'); }});
  });
  const page3 = await open(blocked);
  await page3.click('#menu-toggle'); await page3.getByText('Retro 95').click();
  seen = await look(page3);
  assert(seen.theme === 'typesafe' && seen.checked === 'typesafe', 'without storage the choice still applies to the page');
  await blocked.close();
  log('theme-persistence');

  assert(errors.length === 0, `no uncaught browser errors: ${errors.join('; ')}`);
  log('summary', {failures: 0});
  console.log('TALK_UI_BROWSER_OK');
} finally {
  fixture.kill('SIGTERM');
  await browser.close();
}
