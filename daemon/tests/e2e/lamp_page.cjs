#!/usr/bin/env node
// Settings -> Pilot lamp for the owner: what the lamp means with Show me, brightness
// with Undo, key replies, the night choices (the place only for By the sun, the Save
// bar only for a new place), Identify on This radio, and the front page's
// "Lamp: asleep" line. Runs the page against the fake radio (haslerd --fake, with
// HASLER_FAKE_LAMP=1: a lamp engine that draws to nowhere) in Chrome.
//
//   cd daemon && node tests/e2e/lamp_page.cjs
//
// Needs Node and Playwright (`npm i playwright`, or PLAYWRIGHT=/path/to/node_modules/playwright),
// and a browser: Playwright's own Chromium, or installed Chrome (PW_CHANNEL=chrome).
// HASLER_PY picks the Python that runs the fake radio (default .venv/bin/python).
// SHOTS=/some/dir also saves phone and desktop screenshots there.
'use strict';
const {spawn} = require('child_process');
const fs = require('fs'), os = require('os'), path = require('path'), net = require('net');
const {chromium} = require(process.env.PLAYWRIGHT || 'playwright');

const DAEMON = path.resolve(__dirname, '..', '..');
const PY = process.env.HASLER_PY || path.join(DAEMON, '.venv', 'bin', 'python');
const SHOTS = process.env.SHOTS || '';
const sleep = ms => new Promise(r => setTimeout(r, ms));
let failed = 0, passed = 0;

function freePort(){
  return new Promise((res, rej) => { const s = net.createServer(); s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => res(p)); }); s.on('error', rej); });
}
async function startRadio(){
  const port = await freePort(), tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'hasler-e2e-'));
  fs.mkdirSync(path.join(tmp, 'etc')); fs.mkdirSync(path.join(tmp, 'state'));
  fs.writeFileSync(path.join(tmp, 'etc', 'box.yaml'), `schema: 1\nweb:\n  host: 127.0.0.1\n  port: ${port}\n`);
  const proc = spawn(PY, ['-m', 'haslerd.main', '--fake', '--etc', path.join(tmp, 'etc'), '--state', path.join(tmp, 'state'), '--log-level', 'WARNING'],
    {cwd: DAEMON, stdio: ['ignore', 'ignore', 'pipe'], env: Object.assign({}, process.env, {HASLER_FAKE_LAMP: '1'})});
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

(async () => {
  const radio = await startRadio();
  const get = async url => (await fetch(radio.base + url)).json();
  const send = (method, url, body) => fetch(radio.base + url, {method, headers: {'Content-Type': 'application/json'}, body: body && JSON.stringify(body)});
  const shot = async (page, name) => { if (SHOTS){ fs.mkdirSync(SHOTS, {recursive: true}); await page.screenshot({path: path.join(SHOTS, name + '.png'), fullPage: true}); } };
  await sleep(800);     // the fake's power knob reads off at boot: the radio starts asleep
  const browser = await launch();
  const errors = [];
  try {
    await check('api: status says what the lamp means', async () => {
      const s = await get('/api/status');
      assert(s.lamp === 'asleep', 'status.lamp = ' + JSON.stringify(s.lamp));
    });
    await check('api: preview takes a look or a reply, refuses nonsense', async () => {
      let r = await send('POST', '/api/lamp/preview', {look: 'phone'});
      let j = await r.json();
      assert(r.ok && j.showing && j.look === 'phone' && j.expires_s === 8, 'look: ' + JSON.stringify(j));
      r = await send('POST', '/api/lamp/preview', {reply: 'no'}); j = await r.json();
      assert(r.ok && j.reply === 'no', 'reply: ' + JSON.stringify(j));
      r = await send('POST', '/api/lamp/preview', {look: 'disco'});
      assert(r.status === 400, 'disco answered ' + r.status);
      r = await send('POST', '/api/lamp/preview', {look: null}); j = await r.json();
      assert(r.ok && !j.showing, 'end: ' + JSON.stringify(j));
      r = await fetch(radio.base + '/api/lamp/identify', {method: 'POST', headers: {'Content-Type': 'text/plain'}, body: '{}'});
      assert(r.status === 403, 'identify without JSON answered ' + r.status);
    });

    for (const [name, viewport] of [['phone', {width: 390, height: 844}], ['desktop', {width: 1280, height: 900}]]){
      const ctx = await browser.newContext({viewport});
      const page = await ctx.newPage();
      const posts = [];
      page.on('request', rq => { if (rq.method() !== 'GET' && /\/api\/lamp\//.test(rq.url())) posts.push([new URL(rq.url()).pathname, rq.postData()]); });
      page.on('pageerror', e => errors.push(name + ': ' + e.message));
      page.on('console', m => { if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(name + ': ' + m.text()); });

      await check(name + ': the front page says the lamp in words', async () => {
        await send('POST', '/api/standby', {on: true});
        await page.goto(radio.base + '/');
        await until(() => page.isVisible('#lampLine'), 5000, 'the lamp line');
        const t = (await page.textContent('#lampLine')).trim();
        assert(t === 'Lamp: asleep', 'line says ' + JSON.stringify(t));
        await shot(page, name + '-front-asleep');
        await send('POST', '/api/standby', {on: false});
        await until(async () => !/asleep/.test(await page.textContent('#lampLine')), 6000, 'the line to wake');
        const awake = (await page.textContent('#lampLine')).trim();
        assert(/^Lamp: (ready|playing|starting)$/.test(awake), 'awake line says ' + JSON.stringify(awake));
      });

      await check(name + ': the meanings list, each with Show me', async () => {
        await page.goto(radio.base + '/#settings/lamp');
        await until(() => page.isVisible('#lampMeans'), 5000, 'the meanings');
        const rows = await page.$$eval('#lampMeans [data-mean]', els => els.map(e => e.dataset.mean));
        assert(rows.join() === 'playing,ready,asleep,phone,busy,heard,no,asking,setup', 'rows: ' + rows.join());
        const titles = await page.$$eval('#lampMeans .li-title', els => els.map(e => e.textContent));
        assert(titles.includes('Busy — don’t unplug') && titles.includes('A phone is playing'), 'titles: ' + titles.join(' | '));
        assert(await page.$$eval('#lampMeans button', b => b.length) === 9, 'not 9 Show me buttons');
        await shot(page, name + '-lamp-page');
      });

      await check(name + ': Show me posts the right preview', async () => {
        posts.length = 0;
        await page.click('#lampShow-asleep');
        await until(() => posts.length, 3000, 'the preview post');
        assert(posts[0][0] === '/api/lamp/preview' && JSON.parse(posts[0][1]).look === 'asleep', 'posted ' + JSON.stringify(posts[0]));
        await until(async () => (await page.textContent('#lampShow-asleep')) === 'Showing…', 2000, 'Showing…');
        assert(await page.getAttribute('#lampShow-asleep', 'aria-pressed') === 'true', 'not pressed');
        await page.click('#lampShow-heard');
        await until(() => posts.length >= 2, 3000, 'the reply post');
        assert(JSON.parse(posts[1][1]).reply === 'heard', 'posted ' + posts[1][1]);
      });

      await check(name + ': brightness applies at once, persists, Undo puts it back', async () => {
        await page.click('#lampBright [data-v="low"]');
        await until(async () => (await get('/api/settings')).lamp.brightness === 'low', 3000, 'low on the radio');
        await until(async () => (await page.getAttribute('#lampBright [data-v="low"]', 'aria-checked')) === 'true', 2000, 'Low checked');
        await page.click('.toast-undo');
        await until(async () => (await get('/api/settings')).lamp.brightness === 'normal', 3000, 'Undo to normal');
        await until(async () => (await page.getAttribute('#lampBright [data-v="normal"]', 'aria-checked')) === 'true', 2000, 'Normal checked');
        // Survives a reload.
        await page.click('#lampBright [data-v="bright"]');
        await until(async () => (await get('/api/settings')).lamp.brightness === 'bright', 3000, 'bright');
        await page.reload();
        await until(() => page.isVisible('#lampBright'), 5000, 'the page again');
        assert(await page.getAttribute('#lampBright [data-v="bright"]', 'aria-checked') === 'true', 'Bright not kept');
        await send('PUT', '/api/settings', {lamp: {brightness: 'normal'}});
      });

      await check(name + ': key replies switch', async () => {
        await page.reload();
        await until(() => page.isVisible('#lampKeys'), 5000, 'the switch');
        assert(await page.isChecked('#lampKeys'), 'key replies start off');
        await page.click('label[for="lampKeys"]');
        await until(async () => (await get('/api/settings')).lamp.key_replies === false, 3000, 'off on the radio');
        await send('PUT', '/api/settings', {lamp: {key_replies: true}});
      });

      await check(name + ': the place shows only for By the sun, with no doubled heading', async () => {
        await page.reload();
        await until(() => page.isVisible('#lampName'), 5000, 'the place');
        assert(!(await page.$('#sec-place')), 'a separate "Where the radio stands" section');
        const heads = await page.$$eval('#panel h3', hs => hs.map(x => x.textContent.trim().toLowerCase()));
        assert(heads.join() === 'what the lamp means,brightness,at night', 'headings: ' + heads.join());
        await page.click('#sec-night .seg button:has-text("Never")');
        await until(async () => !(await page.isVisible('#lampName')), 3000, 'the place to hide for Never');
        await page.click('.toast-undo');
        await until(() => page.isVisible('#lampName'), 3000, 'the place back');
      });

      await check(name + ': the Save bar is for a new place only', async () => {
        assert(await page.isHidden('#saveBar'), 'bar before any change');
        const was = await page.inputValue('#lampName'), city = name === 'phone' ? 'Bern' : 'Basel';
        await page.fill('#lampName', city);
        await until(() => page.isVisible('#saveBar'), 2000, 'the Save bar');
        await page.fill('#lampName', was);
        await until(() => page.isHidden('#saveBar'), 2000, 'the bar to go when the place is back as it was');
        await page.fill('#lampName', city);
        await page.click('#sbSave');
        await until(async () => ((await get('/api/settings')).location || {}).name === city, 3000, city + ' saved');
        await until(() => page.isHidden('#saveBar'), 2000, 'the bar to go after Save');
      });

      await check(name + ': lamp colours line links to Maker tools only when they are on', async () => {
        assert(/set by the makers/.test(await page.textContent('#lampMakers')), 'no makers line');
        assert(!(await page.$('#lampMakersLink')), 'a link with Maker tools off');
        await page.evaluate(() => localStorage.setItem('hasler.maker', '1'));
        await page.reload();
        await until(() => page.isVisible('#lampMakersLink'), 5000, 'the link with Maker tools on');
        await page.evaluate(() => localStorage.removeItem('hasler.maker'));
      });

      await check(name + ': Identify this radio posts identify', async () => {
        posts.length = 0;
        await page.goto(radio.base + '/#settings/radio');
        await until(() => page.isVisible('#identSelf'), 5000, 'Identify');
        await page.click('#identSelf');
        await until(() => posts.some(p => p[0] === '/api/lamp/identify'), 3000, 'the identify post');
        await until(async () => /Look for the lamp/.test(await page.textContent('#toast')), 3000, 'the toast');
        await shot(page, name + '-this-radio');
      });
      await ctx.close();
    }
    await check('no console errors', async () => assert(!errors.length, errors.join('\n     ')));
  } finally {
    await browser.close();
    radio.proc.kill();
    fs.rmSync(radio.tmp, {recursive: true, force: true});
  }
  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
