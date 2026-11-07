#!/usr/bin/env node
// Shortcuts for your phone: the Settings → Phones card per platform (iPhone, Android,
// desktop, by user agent), the radio's /do/<action> pages (GET changes nothing; the
// page's own POST does it, once), and the Add to Home Screen hint (phones, browser,
// once, dismissed for good).
// Runs the page against the fake radio (haslerd --fake) in Chrome.
//
//   cd daemon && node tests/e2e/phone_shortcuts.cjs
//
// Needs Node and Playwright (`npm i playwright`, or PLAYWRIGHT=/path/to/node_modules/playwright),
// and a browser: Playwright's own Chromium, or installed Chrome (PW_CHANNEL=chrome).
// HASLER_PY picks the Python that runs the fake radio (default .venv/bin/python).
// SHOTS=/some/dir also saves screenshots there.
'use strict';
const {spawn} = require('child_process');
const fs = require('fs'), os = require('os'), path = require('path'), net = require('net');
const {chromium} = require(process.env.PLAYWRIGHT || 'playwright');

const DAEMON = path.resolve(__dirname, '..', '..');
const PY = process.env.HASLER_PY || path.join(DAEMON, '.venv', 'bin', 'python');
const SHOTS = process.env.SHOTS || '';
const sleep = ms => new Promise(r => setTimeout(r, ms));
let failed = 0, passed = 0;

const IOS_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1';
const ANDROID_UA = 'Mozilla/5.0 (Linux; Android 15; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36';
const SITE = 'https://delgado-murphy.github.io/shortcuts/';

function freePort(){
  return new Promise((res, rej) => { const s = net.createServer(); s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => res(p)); }); s.on('error', rej); });
}
async function startRadio(){
  const port = await freePort(), tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'hasler-e2e-'));
  fs.mkdirSync(path.join(tmp, 'etc')); fs.mkdirSync(path.join(tmp, 'state'));
  fs.writeFileSync(path.join(tmp, 'etc', 'box.yaml'), `schema: 1\nweb:\n  host: 127.0.0.1\n  port: ${port}\n`);
  const proc = spawn(PY, ['-m', 'haslerd.main', '--fake', '--etc', path.join(tmp, 'etc'), '--state', path.join(tmp, 'state'), '--log-level', 'WARNING'],
    {cwd: DAEMON, stdio: ['ignore', 'ignore', 'pipe']});
  let err = ''; proc.stderr.on('data', d => { err += d; });
  const base = `http://127.0.0.1:${port}`;
  for (let i = 0; i < 100; i++){
    try { const r = await fetch(base + '/api/status'); if (r.ok) return {proc, base, tmp}; } catch (_) {}
    if (proc.exitCode != null) break;
    await sleep(100);
  }
  proc.kill(); throw new Error('fake radio did not start:\n' + err);
}
async function launch(){
  const want = process.env.PW_CHANNEL;
  if (!want){ try { return await chromium.launch(); } catch (_) {} }
  return chromium.launch({channel: want || 'chrome'});
}
async function check(name, fn){
  try { await fn(); passed++; console.log('ok   ' + name); }
  catch (e) { failed++; console.log('FAIL ' + name + '\n     ' + (e && e.message || e)); }
}
function assert(c, msg){ if (!c) throw new Error(msg); }
async function until(fn, ms, what){
  const end = Date.now() + ms;
  for (;;){ const v = await fn(); if (v) return v; if (Date.now() > end) throw new Error('timed out waiting for ' + what); await sleep(50); }
}
const shot = async (page, name, full) => { if (SHOTS){ fs.mkdirSync(SHOTS, {recursive: true}); await page.screenshot({path: path.join(SHOTS, name + '.png'), fullPage: !!full}); } };

(async () => {
  const radio = await startRadio();
  const browser = await launch();
  const errors = [];
  const phoneCtx = ua => browser.newContext({viewport: {width: 390, height: 844}, userAgent: ua, isMobile: true, hasTouch: true, deviceScaleFactor: 2});
  const watch = (page, tag) => {
    page.on('console', m => { if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(tag + ': ' + m.text()); });
    page.on('pageerror', e => errors.push(tag + ': ' + e.message));
    return page;
  };
  const volume = async () => (await (await fetch(radio.base + '/api/status')).json()).volume;
  const openCard = async ctx => {
    const page = watch(await ctx.newPage(), 'card');
    await page.goto(radio.base + '/#settings/phones');
    await page.waitForSelector('#sec-shortcuts .sc-card');
    return page;
  };
  try {
    await check('iPhone: the hosted shortcuts with Add, where to put them, home Wi-Fi', async () => {
      const ctx = await phoneCtx(IOS_UA);
      const page = await openCard(ctx);
      assert(await page.getAttribute('#sec-shortcuts .sc-card', 'data-kind') === 'ios', 'not detected as iPhone');
      assert(await page.$('#scAndroid') === null, 'Android links on an iPhone');
      const hrefs = await page.$$eval('#scIos a', as => as.map(a => a.getAttribute('href')));
      assert(hrefs.length === 11, 'links: ' + hrefs.length);
      assert(hrefs.every(u => u.startsWith(SITE) && u.endsWith('.shortcut')), 'hrefs: ' + hrefs.join(' '));
      assert(hrefs.includes(SITE + 'Hasler%20louder.shortcut'), 'no louder: ' + hrefs[0]);
      assert(hrefs.includes(SITE + 'Hasler%20play-pause.shortcut'), 'no play/pause');
      const where = await page.textContent('#scIosWhere');
      for (const w of ['Back Tap', 'Action Button', 'Control Center', 'Lock Screen', 'Siri']) assert(where.includes(w), 'missing ' + w);
      assert(!(await page.getAttribute('#scIosWhere', 'open') != null), 'steps not folded');
      assert((await page.textContent('#scWifi')).includes('home Wi-Fi'), 'no Wi-Fi note');
      await page.locator('#sec-shortcuts').scrollIntoViewIfNeeded();
      await shot(page, 'card-iphone');
      await page.click('#scIosWhere summary');
      await page.locator('#scIosWhere').scrollIntoViewIfNeeded();
      await shot(page, 'card-iphone-where');
      await ctx.close();
    });

    await check('Android: the radio’s own action links, Add to Home screen steps, the honest note', async () => {
      const ctx = await phoneCtx(ANDROID_UA);
      const page = await openCard(ctx);
      assert(await page.getAttribute('#sec-shortcuts .sc-card', 'data-kind') === 'android', 'not detected as Android');
      assert(await page.$('#scIos') === null, 'iPhone files on Android');
      const hrefs = await page.$$eval('#scAndroid a', as => as.map(a => a.getAttribute('href')));
      assert(hrefs.length === 11 && hrefs.every(u => u.startsWith('/do/')), 'hrefs: ' + hrefs.join(' '));
      assert(hrefs.includes('/do/key/6') && hrefs.includes('/do/next'), 'hrefs: ' + hrefs.join(' '));
      assert((await page.textContent('#scAndroidHow')).includes('Add to Home screen'), 'no steps');
      assert(/Quick Settings tile.*app/.test(await page.textContent('#scAndroidLimits')), 'no note on tiles and apps');
      await page.locator('#sec-shortcuts').scrollIntoViewIfNeeded();
      await shot(page, 'card-android');
      await ctx.close();
    });

    await check('desktop: open it on your phone; both lists folded', async () => {
      const ctx = await browser.newContext({viewport: {width: 1280, height: 900}});
      const page = await openCard(ctx);
      assert(await page.getAttribute('#sec-shortcuts .sc-card', 'data-kind') === 'desktop', 'not desktop');
      assert((await page.textContent('#scDesktop')).includes('on your phone'), 'no phone line');
      assert(await page.$('#scDeskIos #scIos') && await page.$('#scDeskAndroid #scAndroid'), 'lists missing');
      assert(!(await page.isVisible('#scIos')), 'iPhone list not folded');
      await page.locator('#sec-shortcuts').scrollIntoViewIfNeeded();
      await shot(page, 'card-desktop');
      await ctx.close();
    });

    await check('/do/louder: a prefetch changes nothing; opening it turns it up once; reload doesn’t', async () => {
      const v0 = await volume();
      const r = await fetch(radio.base + '/do/louder');           // a link preview / prefetch
      assert(r.ok && (await r.text()).includes('<title>Hasler louder</title>'), 'no page');
      await sleep(200);
      assert(await volume() === v0, 'GET changed the volume');
      const ctx = await phoneCtx(ANDROID_UA);
      const page = watch(await ctx.newPage(), 'do');
      const posts = [];
      page.on('request', q => { if (q.method() === 'POST') posts.push(q.url() + ' ' + q.postData()); });
      await page.goto(radio.base + '/do/louder');
      await until(async () => (await page.textContent('#word')) === 'Louder' && posts.length === 1, 4000, 'the POST');
      assert(posts[0].endsWith('/api/volume {"delta":5}'), 'posted ' + posts[0]);
      await until(async () => await volume() === Math.min(100, v0 + 5), 3000, 'volume +5 (' + v0 + ')');
      await shot(page, 'do-louder');
      // Back / restored tab / reload: no second POST, an Again button instead.
      await page.goto(radio.base + '/do/louder#done');
      await page.reload();
      await page.waitForSelector('#again:not([hidden])');
      await sleep(300);
      assert(posts.length === 1, 'reload posted again: ' + posts.length);
      await page.click('#again');
      await until(async () => posts.length === 2, 3000, 'Again');
      await ctx.close();
    });

    await check('Android: Open from the card does it and stays, saying how to add it', async () => {
      const ctx = await phoneCtx(ANDROID_UA);
      const page = await openCard(ctx);
      await page.click('#scAndroid a[href="/do/quieter"]');
      await page.waitForURL(/\/do\/quieter/);
      await until(async () => (await page.textContent('#word')) === 'Quieter', 4000, 'Quieter');
      await sleep(1500);                                  // past the home-screen icon's close/back
      assert(/\/do\/quieter/.test(page.url()), 'left the page: ' + page.url());
      assert((await page.textContent('#note')).includes('Add to Home screen'), 'note: ' + await page.textContent('#note'));
      await shot(page, 'do-quieter-from-card');
      await ctx.close();
    });

    await check('/do/key/3: the page presses key 3', async () => {
      const ctx = await phoneCtx(ANDROID_UA);
      const page = watch(await ctx.newPage(), 'do');
      let body = null;
      page.on('request', q => { if (q.method() === 'POST') body = q.postDataJSON(); });
      await page.goto(radio.base + '/do/key/3');
      await until(async () => body, 4000, 'the POST');
      assert(body.key === 'k3' && body.event === 'tap', JSON.stringify(body));
      assert(await page.title() === 'Hasler key 3', 'title ' + await page.title());
      await ctx.close();
    });

    await check('Add to Home Screen hint: iPhone, once, platform words', async () => {
      const ctx = await phoneCtx(IOS_UA);
      const page = watch(await ctx.newPage(), 'a2hs');
      await page.goto(radio.base + '/');
      await page.waitForSelector('#a2hs:not([hidden])');
      assert((await page.textContent('#a2hs')).includes('Share → Add to Home Screen'), await page.textContent('#a2hs'));
      await shot(page, 'a2hs-iphone');
      await page.reload();
      await page.waitForSelector('#bank');
      await sleep(300);
      assert(await page.isHidden('#a2hs'), 'shown a second time');
      await ctx.close();
    });

    await check('Add to Home Screen hint: Android words; × dismisses it for good', async () => {
      const ctx = await phoneCtx(ANDROID_UA);
      const page = watch(await ctx.newPage(), 'a2hs');
      await page.goto(radio.base + '/');
      await page.waitForSelector('#a2hs:not([hidden])');
      assert((await page.textContent('#a2hs')).includes('⋮ → Add to Home screen'), await page.textContent('#a2hs'));
      await page.click('#a2hsX');
      assert(await page.isHidden('#a2hs'), 'still shown');
      assert(await page.evaluate(() => localStorage.getItem('hasler.a2hs')) === 'dismissed', 'not remembered');
      await page.reload(); await page.waitForSelector('#bank'); await sleep(300);
      assert(await page.isHidden('#a2hs'), 'back after reload');
      await ctx.close();
    });

    await check('Add to Home Screen hint: never on desktop, never from the home screen, never without storage', async () => {
      const desk = await browser.newContext({viewport: {width: 1280, height: 900}});
      let page = watch(await desk.newPage(), 'a2hs');
      await page.goto(radio.base + '/'); await page.waitForSelector('#bank'); await sleep(300);
      assert(await page.isHidden('#a2hs'), 'shown on desktop');
      await desk.close();
      const home = await phoneCtx(IOS_UA);
      await home.addInitScript(() => { Object.defineProperty(navigator, 'standalone', {get: () => true}); });
      page = watch(await home.newPage(), 'a2hs');
      await page.goto(radio.base + '/'); await page.waitForSelector('#bank'); await sleep(300);
      assert(await page.isHidden('#a2hs'), 'shown in standalone');
      await home.close();
      const nostore = await phoneCtx(ANDROID_UA);
      await nostore.addInitScript(() => { Object.defineProperty(window, 'localStorage', {get(){ throw new Error('blocked'); }}); });
      page = await nostore.newPage();
      page.on('pageerror', e => errors.push('nostore: ' + e.message));
      await page.goto(radio.base + '/'); await page.waitForSelector('#bank'); await sleep(300);
      assert(await page.isHidden('#a2hs'), 'shown without storage');
      await nostore.close();
    });

    await check('no console errors', async () => { assert(!errors.length, errors.join('\n     ')); });
  } finally {
    await browser.close();
    radio.proc.kill();
    fs.rmSync(radio.tmp, {recursive: true, force: true});
  }
  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
