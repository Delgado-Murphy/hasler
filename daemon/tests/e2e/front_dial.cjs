#!/usr/bin/env node
// The front page tells the truth about where the music comes from. The dial's
// needle has a phone stop at the left end and a page stop at the right: a play
// from the page slides it to the page stop, a phone taking the speaker to the
// phone stop; when the phone stops the needle stays put, un-bolded. The bank
// draws "down" (a finger, a latched key) apart from "lit" (what plays): on an
// Excelsior key 3 stays down, un-lit, while the page or a phone plays, and PU
// lights only when PU chose the source. Runs fake radios (haslerd --fake) in
// Chrome; a phone is simulated with POST /api/fake/phone (web/fake_api.py).
//
//   cd daemon && node tests/e2e/front_dial.cjs
//
// Needs Node and Playwright (`npm i playwright`, or PLAYWRIGHT=/path/to/node_modules/playwright),
// and a browser: Playwright's own Chromium, or installed Chrome (PW_CHANNEL=chrome).
// HASLER_PY picks the Python that runs the fake radio (default .venv/bin/python).
'use strict';
const {spawn} = require('child_process');
const fs = require('fs'), os = require('os'), path = require('path'), net = require('net');
const {chromium} = require(process.env.PLAYWRIGHT || 'playwright');

const DAEMON = path.resolve(__dirname, '..', '..');
const PY = process.env.HASLER_PY || path.join(DAEMON, '.venv', 'bin', 'python');
const sleep = ms => new Promise(r => setTimeout(r, ms));
let failed = 0, passed = 0;

function freePort(){
  return new Promise((res, rej) => { const s = net.createServer(); s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => res(p)); }); s.on('error', rej); });
}
async function startRadio({variant, closed}){
  const port = await freePort(), tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'hasler-e2e-'));
  fs.mkdirSync(path.join(tmp, 'etc', 'variant.d'), {recursive: true}); fs.mkdirSync(path.join(tmp, 'state'));
  fs.writeFileSync(path.join(tmp, 'etc', 'box.yaml'), `schema: 1\nvariant: ${variant}\nkeys_file: ${path.join(tmp, "state", "keys.yaml")}\nweb:\n  host: 127.0.0.1\n  port: ${port}\n`);
  const env = Object.assign({}, process.env, closed ? {HASLER_FAKE_CLOSED: closed} : {});
  const proc = spawn(PY, ['-m', 'haslerd.main', '--fake', '--etc', path.join(tmp, 'etc'), '--state', path.join(tmp, 'state'), '--log-level', 'WARNING'],
    {cwd: DAEMON, stdio: ['ignore', 'ignore', 'pipe'], env});
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
const getJson = async (base, p) => (await fetch(base + p)).json();
const post = (base, p, body) => fetch(base + p, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
const play = (base, name) => post(base, '/api/play', {preset: {name, source: 'radio', uri: 'http://stream.example/' + name.toLowerCase().replace(/\W+/g, '-')}});
const phone = (base, source, device, title, artist) => post(base, '/api/fake/phone', {source, device, title, artist});
// The dial as the page draws it: where the needle points (the strip's data-at), which stop is
// bold, the head and the song line; and the bank's keys with their down / lit classes.
const dial = page => page.evaluate(() => {
  const strip = document.getElementById('stripEl'), on = strip.querySelector('.st.on');
  const stop = id => { const el = strip.querySelector('.st[data-id="' + id + '"]'); return el ? el.querySelector('.st-nm').textContent : null; };
  return {at: strip.dataset.at, bold: on ? on.dataset.id : null, head: document.getElementById('dialHead').textContent,
    name: document.getElementById('dialName').textContent, song: document.getElementById('songTitle').textContent,
    sub: document.getElementById('songSub').textContent, x: strip.style.transform,
    phoneStop: stop('_phone'), pageStop: stop('_page')};
});
const bank = page => page.$$eval('#bank button', bs => Object.fromEntries(bs.map(b => [b.dataset.key,
  {down: b.classList.contains('down'), lit: b.classList.contains('lit'), pressed: b.getAttribute('aria-pressed'), label: b.getAttribute('aria-label')}])));
const lit = keys => Object.keys(keys).filter(k => keys[k].lit);
const down = keys => Object.keys(keys).filter(k => keys[k].down);

(async () => {
  const radios = {};
  radios.hm = await startRadio({variant: 'hasler-hm'});
  radios.ex = await startRadio({variant: 'excelsior', closed: 'd10'});    // key 3 latched
  const browser = await launch();
  const errors = [];
  const ctx = await browser.newContext({viewport: {width: 390, height: 844}, deviceScaleFactor: 2});
  const open = async (radio, hash = '#') => {
    const page = await ctx.newPage();
    page.on('console', m => { if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(m.text()); });
    page.on('pageerror', e => errors.push(e.message));
    await page.goto(radio.base + '/' + hash);
    return page;
  };
  try {
    const hm = radios.hm, ex = radios.ex;

    await check('HM: the dial has a phone stop and a page stop around the keys', async () => {
      const page = await open(hm);
      await until(async () => (await page.$$('#bank button')).length === 7, 5000, 'seven keys');
      const ids = await until(async () => { const v = await page.$$eval('#stripEl .st', es => es.map(e => e.dataset.id)); return v.length ? v : null; }, 5000, 'the strip');
      assert(ids.join(' ') === '_phone k1 k2 k3 k4 k5 k6 _page', 'stops: ' + ids.join(' '));
      const d = await dial(page);
      assert(d.phoneStop === 'Phone' && d.pageStop === 'From the page', 'end stops: ' + JSON.stringify(d));
      assert(d.bold === null && (d.head === 'Standby' || d.head === 'Ready'), 'quiet: ' + JSON.stringify(d));   // a fresh fake radio sleeps
      await page.close();
    });

    await check('HM: a key lights (not down) and the needle is on it, bold', async () => {
      const page = await open(hm);
      await page.waitForSelector('#bank button[data-key="k2"]');
      await page.click('#bank button[data-key="k2"]');
      await until(async () => { const s = await getJson(hm.base, '/api/status'); return s.played_from === 'key' && s.current_key === 'k2'; }, 5000, 'key 2 playing');
      await sleep(400);     // the tap's flash is over
      const d = await dial(page), b = await bank(page);
      assert(d.at === 'k2' && d.bold === 'k2' && d.head === 'Key 2 · Radio', 'dial: ' + JSON.stringify(d));
      assert(lit(b).join() === 'k2' && down(b).length === 0, 'bank: ' + JSON.stringify(b));
      assert(b.k2.pressed === 'true' && b.k2.label.includes(', playing'), 'aria: ' + JSON.stringify(b.k2));
      await page.close();
    });

    await check('HM: a play from the page moves the needle to the page stop; no key lit', async () => {
      const page = await open(hm);
      await page.waitForSelector('#bank button[data-key="k2"]');
      await until(async () => (await dial(page)).at === 'k2', 5000, 'the needle on key 2');
      assert((await play(hm.base, 'Radio Pop')).ok, 'play refused');
      await until(async () => (await dial(page)).bold === '_page', 5000, 'the page stop bold');
      const d = await dial(page), b = await bank(page);
      assert(d.at === '_page' && d.pageStop === 'Radio Pop' && d.head === 'From the page · Radio' && d.name === 'Radio Pop', 'dial: ' + JSON.stringify(d));
      assert(lit(b).length === 0 && down(b).length === 0, 'bank: ' + JSON.stringify(b));
      await page.close();
    });

    await check('HM: a phone taking the speaker moves the needle to the phone stop; PU stays dark', async () => {
      const page = await open(hm);
      await page.waitForSelector('#bank button[data-key="pu"]');
      await until(async () => (await dial(page)).at === '_page', 5000, 'the needle on the page stop');
      assert((await phone(hm.base, 'bluetooth', 'Pixel 11', 'Blue in Green', 'Miles Davis')).ok, 'phone refused');
      await until(async () => (await dial(page)).bold === '_phone', 5000, 'the phone stop bold');
      const d = await dial(page), b = await bank(page);
      assert(d.at === '_phone' && d.phoneStop === 'Pixel 11' && d.head === 'Phone · Bluetooth' && d.name === 'Pixel 11', 'dial: ' + JSON.stringify(d));
      assert(d.song === 'Blue in Green' && d.sub === 'Miles Davis · Pixel 11', 'song: ' + JSON.stringify(d));
      assert(lit(b).length === 0 && down(b).length === 0, 'bank: ' + JSON.stringify(b));
      await page.close();
    });

    await check('HM: when the phone stops the needle stays on the phone stop, un-bolded', async () => {
      const page = await open(hm);
      await page.waitForSelector('#bank button[data-key="pu"]');
      await until(async () => (await dial(page)).bold === '_phone', 5000, 'the phone stop bold');
      const before = (await dial(page)).x;
      assert((await phone(hm.base, null)).ok, 'release refused');
      await until(async () => (await getJson(hm.base, '/api/status')).played_from === null, 5000, 'quiet');
      await sleep(1200);
      const d = await dial(page);
      assert(d.at === '_phone' && d.bold === null && d.x === before && d.head === 'Ready', 'dial: ' + JSON.stringify(d));
      assert(d.phoneStop === 'Phone', 'the phone stop keeps the old name: ' + d.phoneStop);
      await page.close();
    });

    await check('HM: PU lights only when PU chose the source', async () => {
      const page = await open(hm);
      await page.waitForSelector('#bank button[data-key="pu"]');
      await page.click('#bank button[data-key="pu"]');
      await until(async () => (await getJson(hm.base, '/api/status')).pu_source === 'bluetooth', 5000, 'PU on Bluetooth');
      // Like moOde: the flag stays off until a phone connects.
      assert((await phone(hm.base, null)).ok, 'release refused');
      await until(async () => (await getJson(hm.base, '/api/status')).waiting === true, 5000, 'waiting');
      await until(async () => (await bank(page)).pu.lit, 5000, 'PU lit');
      let d = await dial(page), b = await bank(page);
      assert(d.at === '_phone' && d.bold === null && d.head === 'PU · Bluetooth' && d.song === 'Waiting for a phone', 'waiting: ' + JSON.stringify(d));
      assert(lit(b).join() === 'pu' && !b.pu.down, 'bank: ' + JSON.stringify(b));
      assert((await phone(hm.base, 'bluetooth', 'Pixel 11', 'So What', 'Miles Davis')).ok, 'phone refused');
      await until(async () => (await dial(page)).bold === '_phone', 5000, 'the phone stop bold');
      d = await dial(page); b = await bank(page);
      assert(d.head === 'PU · Bluetooth' && d.song === 'So What' && lit(b).join() === 'pu', 'PU plays the phone: ' + JSON.stringify({d, b}));
      await phone(hm.base, null);
      await page.close();
    });

    await check('Excelsior: key 3 stays down, un-lit, while the page or a phone plays', async () => {
      const page = await open(ex);
      await until(async () => (await page.$$('#bank button')).length === 9, 5000, 'nine keys');
      await until(async () => { const b = await bank(page); return b.k3 && b.k3.down && b.k3.lit; }, 5000, 'key 3 down and lit');
      let d = await dial(page), b = await bank(page);
      assert(d.at === 'k3' && d.bold === 'k3' && b.k3.pressed === 'true' && b.k3.label.includes(', playing'), 'latched: ' + JSON.stringify({d, k3: b.k3}));
      assert((await play(ex.base, 'Radio Pop')).ok, 'play refused');
      await until(async () => (await dial(page)).bold === '_page', 5000, 'the page stop bold');
      d = await dial(page); b = await bank(page);
      assert(d.at === '_page' && d.head === 'From the page · Radio', 'dial: ' + JSON.stringify(d));
      assert(b.k3.down && !b.k3.lit && lit(b).length === 0 && down(b).join() === 'k3', 'bank: ' + JSON.stringify(b));
      assert(b.k3.pressed === 'true' && b.k3.label.includes(', down') && !b.k3.label.includes('playing'), 'aria: ' + JSON.stringify(b.k3));
      assert((await phone(ex.base, 'bluetooth', 'Pixel 11', 'Song', 'Band')).ok, 'phone refused');
      await until(async () => (await dial(page)).bold === '_phone', 5000, 'the phone stop bold');
      d = await dial(page); b = await bank(page);
      assert(d.head === 'Phone · Bluetooth' && b.k3.down && !b.k3.lit && !b.pu.lit && !b.pu.down, 'phone: ' + JSON.stringify({d, b}));
      await phone(ex.base, null);
      await page.close();
    });

    // A YouTube Music song on the dial: two small buttons beside it (Lyrics, More), none for
    // live radio or a phone. The fake radio can't play YouTube Music, so the page's /api/status
    // is answered with the song added (route), and the lyrics and account calls likewise.
    await check('HM: a YouTube Music song gets Lyrics and More beside it; radio and a phone don’t', async () => {
      await play(hm.base, 'Radio Uno');
      let song = null, posted = null;
      const page = await ctx.newPage();
      page.on('pageerror', e => errors.push(e.message));
      await page.route('**/api/status', async route => {
        const resp = await route.fetch(), j = await resp.json();
        if (song){ j.standby = false; j.playing = Object.assign({}, j.playing || {}, {source: 'ytm', name: 'Rosalía radio'}); j.track = song; }
        route.fulfill({response: resp, json: j});
      });
      await page.route('**/api/ytm/account', route => route.fulfill({json: {available: true, signed_in: false}}));
      await page.route('**/api/ytm/lyrics/**', route => route.fulfill({json: {lyrics: 'First line\nSecond line', source: 'Source: Test'}}));
      await page.route('**/api/ytm/radio', route => { posted = route.request().postDataJSON(); route.fulfill({json: {ok: true, preset: {name: 'La Perla radio'}}}); });
      await page.goto(hm.base + '/#');
      await until(async () => (await dial(page)).name === 'Radio Uno', 5000, 'the radio on the dial');
      const hidden = () => page.evaluate(() => document.getElementById('songActs').hidden);
      assert(await hidden(), 'live radio shows the song buttons');
      const keysBefore = await page.$eval('#bank', b => b.getBoundingClientRect().top);
      song = {title: 'La Perla', artist: 'ROSALÍA', vid: 'w7pjt9ZH3NM'};
      await until(async () => !(await hidden()), 5000, 'the song buttons');
      const box = await page.evaluate(() => ['ytnLyr', 'ytnMore'].map(id => { const b = document.getElementById(id), r = b.getBoundingClientRect();
        return {w: r.width, h: r.height, label: b.getAttribute('aria-label'), exp: b.getAttribute('aria-expanded')}; }));
      assert(box[0].label === 'Lyrics for La Perla' && box[0].exp === 'false' && box[1].label === 'More for La Perla', 'labels: ' + JSON.stringify(box));
      assert(box.every(b => b.w >= 44 && b.h >= 44), 'hit targets: ' + JSON.stringify(box));
      const keysAfter = await page.$eval('#bank', b => b.getBoundingClientRect().top);
      assert(Math.abs(keysAfter - keysBefore) < 1, `the keys moved when the song came: ${keysBefore} → ${keysAfter}`);
      assert(!(await page.$('#ytnRadio')) && !(await page.$('#ytnKey')), 'the old big buttons are still there');
      // Lyrics opens under the dial and closes again; focus stays on the button.
      await page.click('#ytnLyr');
      await page.waitForSelector('#ytnLyrics .ytn-text');
      assert((await page.$eval('#ytnLyrics', e => e.textContent)).includes('Second line'), 'the lyrics');
      assert(await page.evaluate(() => document.activeElement.id === 'ytnLyr' && document.activeElement.getAttribute('aria-expanded') === 'true'), 'focus / expanded');
      await page.click('#ytnLyr');
      assert(await page.evaluate(() => document.getElementById('ytmNow').hidden), 'the lyrics didn’t close');
      // More: the song's sheet with Start a radio and Put on a key.
      await page.click('#ytnMore');
      await page.waitForSelector('dialog.yt-sheet[open]');
      const acts = await page.$$eval('dialog.yt-sheet .sts-acts button', bs => bs.map(b => b.textContent));
      assert(acts.join('|') === 'Start a radio from this song|Put on a key', 'sheet: ' + acts.join('|'));
      await page.click('#ytsPlay');
      await until(() => posted, 3000, 'the radio request');
      assert(posted.seed === 'song:w7pjt9ZH3NM' && posted.name === 'La Perla radio', 'radio: ' + JSON.stringify(posted));
      await page.keyboard.press('Escape');
      await until(() => page.evaluate(() => document.activeElement.id === 'ytnMore'), 3000, 'focus back on More');
      song = null;
      assert((await phone(hm.base, 'bluetooth', 'Pixel 11', 'So What', 'Miles Davis')).ok, 'phone refused');
      await until(async () => (await dial(page)).bold === '_phone', 5000, 'the phone stop bold');
      assert(await hidden(), 'a phone shows the song buttons');
      await phone(hm.base, null);
      await page.close();
    });

    await check('no console errors', async () => {
      assert(!errors.length, errors.join('\n     '));
    });
  } finally {
    await browser.close();
    for (const r of Object.values(radios)){ r.proc.kill(); fs.rmSync(r.tmp, {recursive: true, force: true}); }
  }
  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
