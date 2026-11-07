#!/usr/bin/env node
// Synced lyrics on the front page: the line being sung lights up and stays in the middle of
// the box, follows the song between polls, re-syncs after a jump (seek, skip, a key pressed
// again) and holds on pause; scrolling by hand stops the following for a few seconds with a
// "Back to the song" chip; plain lyrics say Plain; enhanced LRC lights word by word; Earlier /
// Later step 0.25 s (held: repeating) with the value shown, Sync takes the line the owner taps
// as the one sung now, and the timing is saved on the radio. The fake radio can't play YouTube Music, so the page's
// /api/status is answered with a song and a position from a clock this test runs, and
// /api/ytm/lyrics with timed lines.
//
//   cd daemon && node tests/e2e/lyrics_sync.cjs
//
// Needs Node and Playwright (`npm i playwright`, or PLAYWRIGHT=/path/to/node_modules/playwright),
// and a browser: Playwright's own Chromium, or installed Chrome (PW_CHANNEL=chrome).
// HASLER_PY picks the Python that runs the fake radio (default .venv/bin/python).
// LYRICS_SHOTS=<dir> also saves phone and desktop screenshots, synced and plain.
'use strict';
const {spawn} = require('child_process');
const fs = require('fs'), os = require('os'), path = require('path'), net = require('net');
const {chromium} = require(process.env.PLAYWRIGHT || 'playwright');

const DAEMON = path.resolve(__dirname, '..', '..');
const PY = process.env.HASLER_PY || path.join(DAEMON, '.venv', 'bin', 'python');
const SHOTS = process.env.LYRICS_SHOTS || '';
const sleep = ms => new Promise(r => setTimeout(r, ms));
let failed = 0, passed = 0;

function freePort(){
  return new Promise((res, rej) => { const s = net.createServer(); s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => res(p)); }); s.on('error', rej); });
}
async function startRadio({variant}){
  const port = await freePort(), tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'hasler-e2e-'));
  fs.mkdirSync(path.join(tmp, 'etc', 'variant.d'), {recursive: true}); fs.mkdirSync(path.join(tmp, 'state'));
  fs.writeFileSync(path.join(tmp, 'etc', 'box.yaml'), `schema: 1\nvariant: ${variant}\nkeys_file: ${path.join(tmp, "state", "keys.yaml")}\nweb:\n  host: 127.0.0.1\n  port: ${port}\n`);
  const proc = spawn(PY, ['-m', 'haslerd.main', '--fake', '--etc', path.join(tmp, 'etc'), '--state', path.join(tmp, 'state'), '--log-level', 'WARNING'],
    {cwd: DAEMON, stdio: ['ignore', 'ignore', 'pipe'], env: process.env});
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
  for (;;){ const v = await fn(); if (v) return v; if (Date.now() > end) throw new Error('timed out waiting for ' + what); await sleep(40); }
}
const getJson = async (base, p) => (await fetch(base + p)).json();
const post = (base, p, body) => fetch(base + p, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
const play = (base, name) => post(base, '/api/play', {preset: {name, source: 'radio', uri: 'http://stream.example/' + name.toLowerCase().replace(/\W+/g, '-')}});

// Twelve lines 1.5 s apart from 1 s in; a long line wraps on a phone.
const LINES = Array.from({length: 12}, (_, i) => ({t: 1 + 1.5 * i, end: null, text: i === 6 ? 'Line 7, the long one that wraps across the box on a phone' : 'Line ' + (i + 1)}));
const SYNCED = {available: true, lyrics: LINES.map(l => l.text).join('\n'), source: 'Source: LyricFind', synced: true, lines: LINES, timing: 'YouTube Music'};
const PLAIN = {available: true, lyrics: 'Hola, ladrón de paz\nque me robas la calma\ny no devuelves nada', source: 'Source: Musixmatch', synced: false, lines: null};
const WORDS = {available: true, lyrics: 'Hola mundo azul', source: 'LRCLIB', synced: true,
  lines: [{t: 1, end: null, text: 'Hola mundo azul', words: [{t: 1, w: 'Hola '}, {t: 2, w: 'mundo '}, {t: 3, w: 'azul'}]},
          {t: 5, end: null, text: 'Otra línea', words: [{t: 5, w: 'Otra '}, {t: 6, w: 'línea'}]}]};

(async () => {
  const radio = await startRadio({variant: 'hasler-hm'});
  const browser = await launch();
  const errors = [];
  // The song's clock: where it was at `since` (ms), playing or paused.
  const song = {vid: 'w7pjt9ZH3NM', title: 'La Perla', el: 0, since: Date.now(), state: 'play', offset: 0};
  const at = () => song.el + (song.state === 'play' ? (Date.now() - song.since) / 1000 : 0);
  const seek = (s, state = song.state) => { song.el = s; song.since = Date.now(); song.state = state; };
  let lyrics = SYNCED;
  const wire = async page => {
    page.on('console', m => { if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(m.text()); });
    page.on('pageerror', e => errors.push(e.message));
    await page.route('**/api/status', async route => {
      const resp = await route.fetch(), j = await resp.json();
      j.standby = false; j.source = 'ytm';
      j.playing = Object.assign({}, j.playing || {}, {source: 'ytm', name: 'Rosalía radio'});
      j.track = {title: song.title, artist: 'ROSALÍA', album: 'LUX', vid: song.vid, thumb: '', art: ''};
      // song.offset null: the radio's own saved offset (what /api/status carries on a real radio).
      let off = song.offset;
      if (off === null){ const s = await getJson(radio.base, '/api/settings'); off = s.ytm.lyrics_offset_ms; }
      j.position = {elapsed_s: +at().toFixed(3), duration_s: 200, state: song.state, at: Date.now() / 1000, offset_ms: off};
      route.fulfill({response: resp, json: j});
    });
    await page.route('**/api/ytm/account', route => route.fulfill({json: {available: true, signed_in: false}}));
    await page.route('**/api/ytm/lyrics/**', route => route.fulfill({json: lyrics}));
  };
  const openLyrics = async (ctx) => {
    const page = await ctx.newPage();
    await wire(page);
    await page.goto(radio.base + '/#');
    await page.waitForSelector('#ytnLyr');
    await page.click('#ytnLyr');
    return page;
  };
  const now = page => page.evaluate(() => { const el = document.querySelector('#ytnSync .lyl.now'); return el ? el.textContent : null; });
  const centred = page => page.evaluate(() => {
    const box = document.getElementById('ytnSync'), el = box.querySelector('.lyl.now');
    if (!el) return null;
    const b = box.getBoundingClientRect(), r = el.getBoundingClientRect();
    return Math.abs((r.top + r.height / 2) - (b.top + b.height / 2));
  });
  try {
    await play(radio.base, 'Radio Uno');
    const phone = await browser.newContext({viewport: {width: 390, height: 844}, deviceScaleFactor: 2});

    await check('the line being sung lights up, the sung ones dim, and it sits in the middle', async () => {
      seek(4.6, 'play');                          // Line 3 (4.0 s) is being sung
      const page = await openLyrics(phone);
      await page.waitForSelector('#ytnSync .lyl.now');
      assert(/^Line [34]$/.test(await now(page)), 'now: ' + await now(page));
      const tag = await page.$eval('.lyr-tag', e => [e.textContent, e.classList.contains('on')]);
      assert(tag[0] === 'Synced' && tag[1], 'tag: ' + tag);
      const src = await page.$eval('.lyr-foot .ytn-src', e => e.textContent);
      assert(src === 'Source: LyricFind · timing from YouTube Music', 'footer: ' + src);
      const past = await page.$$eval('#ytnSync .lyl.past', es => es.map(e => e.textContent));
      assert(past.slice(0, 2).join('|') === 'Line 1|Line 2', 'past: ' + past.join('|'));
      assert(await page.$eval('#ytnSync .lyl.now', e => e.getAttribute('aria-current')) === 'true', 'aria-current');
      // It follows between polls: Line 5 comes at 7.0 s.
      await until(async () => (await now(page)) === 'Line 5', 4000, 'Line 5');
      await until(async () => { const d = await centred(page); return d !== null && d < 24; }, 2000, 'the line centred');
      assert(await page.$$eval('#ytmNow [aria-live]', es => es.length) === 0, 'a live region in the lyrics');
      await page.close();
    });

    await check('a jump (seek / skip) re-syncs at the next poll, both ways; a pause holds the line', async () => {
      seek(2.0, 'play');
      const page = await openLyrics(phone);
      await until(async () => (await now(page)) === 'Line 1', 3000, 'Line 1');
      seek(16.4);                                 // Line 11 (16.0 s)
      await until(async () => (await now(page)) === 'Line 11', 2500, 'Line 11 after the jump');
      await until(async () => { const d = await centred(page); return d !== null && d < 24; }, 2000, 'Line 11 centred');
      seek(5.6);                                  // back to Line 4 (5.5 s)
      await until(async () => (await now(page)) === 'Line 4', 2500, 'Line 4 after the jump back');
      seek(7.3, 'pause');                         // Line 5, paused
      await until(async () => (await now(page)) === 'Line 5', 2500, 'Line 5 on pause');
      await sleep(2200);
      assert((await now(page)) === 'Line 5', 'moved on while paused: ' + await now(page));
      seek(7.3, 'play');
      await until(async () => (await now(page)) === 'Line 6', 3500, 'Line 6 after play');
      await page.close();
    });

    await check('scrolling by hand stops the following, with a "Back to the song" chip', async () => {
      seek(1.2, 'play');
      const page = await openLyrics(phone);
      await page.waitForSelector('#ytnSync .lyl.now');
      assert(await page.$eval('#lyrBack', e => e.hidden), 'the chip shows before any scroll');
      const box = await page.$eval('#ytnSync', e => { const r = e.getBoundingClientRect(); return {x: r.x + r.width / 2, y: r.y + r.height / 2}; });
      await page.mouse.move(box.x, box.y);
      await page.mouse.wheel(0, 600);
      await until(() => page.$eval('#lyrBack', e => !e.hidden), 1500, 'the chip');
      const held = await page.$eval('#ytnSync', e => e.scrollTop);
      const was = await now(page);
      await until(async () => (await now(page)) !== was, 3000, 'the line to move on');
      assert(Math.abs(await page.$eval('#ytnSync', e => e.scrollTop) - held) < 2, 'the box scrolled away from the owner');
      await page.click('#lyrBack');
      assert(await page.$eval('#lyrBack', e => e.hidden), 'the chip stayed');
      await until(async () => { const d = await centred(page); return d !== null && d < 24; }, 2000, 'back on the line');
      // By itself after a few seconds too.
      await page.mouse.wheel(0, -300);
      await until(() => page.$eval('#lyrBack', e => !e.hidden), 1500, 'the chip again');
      await until(() => page.$eval('#lyrBack', e => e.hidden), 7000, 'the chip to go by itself');
      if (SHOTS){ seek(9.0, 'play'); await sleep(1600); await page.screenshot({path: path.join(SHOTS, 'phone-synced.png')}); }
      await page.close();
    });

    await check('plain lyrics say Plain and don\'t follow', async () => {
      lyrics = PLAIN;
      song.vid = 'p3w_8a5w6Tk'; song.title = 'Otra canción';
      const page = await openLyrics(phone);
      await page.waitForSelector('#ytnLyrics .ytn-text');
      assert(!(await page.$('#ytnSync')), 'a synced box for plain lyrics');
      const tag = await page.$eval('.lyr-tag', e => [e.textContent, e.classList.contains('on')]);
      assert(tag[0] === 'Plain' && !tag[1], 'tag: ' + tag);
      assert((await page.$eval('.lyr-foot .ytn-src', e => e.textContent)) === 'Source: Musixmatch', 'source');
      assert(!(await page.$('.lyr-nudge')), 'timing buttons for plain lyrics');
      if (SHOTS) await page.screenshot({path: path.join(SHOTS, 'phone-plain.png')});
      await page.close();
    });

    await check('enhanced LRC lights word by word', async () => {
      lyrics = WORDS;
      song.vid = 'lv-r_Tr6Tw8'; song.title = 'Azul';
      seek(2.4, 'play');
      const page = await openLyrics(phone);
      await page.waitForSelector('#ytnSync .lyl.now .w.cur');
      const w = await page.$$eval('#ytnSync .lyl.now .w', es => es.map(e => (e.classList.contains('sung') ? 's' : e.classList.contains('cur') ? 'c' : '-')).join(''));
      assert(w === 'sc-' || w === 'ssc', 'words: ' + w);
      const p = await page.$eval('#ytnSync .lyl.now .w.cur', e => parseFloat(e.style.getPropertyValue('--p')));
      assert(p > 0 && p <= 100, '--p: ' + p);
      await page.close();
    });

    await check('a new song starts its lyrics from the top; Later saves the timing on the radio', async () => {
      lyrics = SYNCED;
      song.vid = 'w7pjt9ZH3NM'; song.title = 'La Perla';
      seek(10.2, 'play');
      const page = await openLyrics(phone);
      await page.waitForSelector('#ytnSync .lyl.now');
      song.vid = 'abcdefghijk'; song.title = 'Next song'; seek(0.2, 'pause');   // held before its first line
      await until(async () => (await page.evaluate(() => { const b = document.getElementById('ytnSync');
        return b && document.getElementById('ytnLyr').getAttribute('aria-label') === 'Lyrics for Next song' && !b.querySelector('.lyl.now') && b.scrollTop === 0; })),
        3000, 'the next song at the top');
      seek(0.2, 'play');
      await until(async () => (await now(page)) === 'Line 1', 3000, 'its first line');
      await page.click('.lyr-nudge button:last-child');
      await until(async () => { const s = await getJson(radio.base, '/api/settings'); return s.ytm && s.ytm.lyrics_offset_ms === 250; }, 3000, 'the offset saved');
      await page.close();
    });

    await check('timing: steps of 0.25 s with the value shown, a held press repeats, Sync sets the tapped line, kept after a reload', async () => {
      lyrics = SYNCED;
      song.vid = 'w7pjt9ZH3NM'; song.title = 'La Perla'; song.offset = null;   // the radio's own offset from here on
      await fetch(radio.base + '/api/settings', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ytm: {lyrics_offset_ms: 0}})});
      seek(4.6, 'play');
      let page = await openLyrics(phone);
      await page.waitForSelector('#ytnSync .lyl.now');
      const off = () => page.$eval('#lyrOff', e => e.textContent);
      const ms = async () => { const t = await off(); return Math.round(parseFloat(t.replace('−', '-').replace('+', '')) * 1000); };
      await until(async () => (await off()) === '0.00 s', 2000, 'the value 0.00 s');
      await page.click('.lyr-nudge button[aria-label="Lyrics later"]');
      assert((await off()) === '+0.25 s', 'after Later: ' + await off());
      await page.click('.lyr-nudge button[aria-label="Lyrics earlier"]');
      await page.click('.lyr-nudge button[aria-label="Lyrics earlier"]');
      assert((await off()) === '−0.25 s', 'after Earlier twice: ' + await off());
      await until(async () => (await getJson(radio.base, '/api/settings')).ytm.lyrics_offset_ms === -250, 3000, '-250 saved');
      // Held: keeps stepping until let go.
      const later = await page.$eval('.lyr-nudge button[aria-label="Lyrics later"]', e => { const r = e.getBoundingClientRect(); return {x: r.x + r.width / 2, y: r.y + r.height / 2}; });
      await page.mouse.move(later.x, later.y); await page.mouse.down(); await sleep(1150); await page.mouse.up();
      const held = await ms();
      assert(held >= 750 && held % 250 === 0, 'held Later: ' + held);
      await sleep(400);
      assert((await ms()) === held, 'kept stepping after let go');
      // Sync, paused for a steady clock: the song is at 9.1 s (Line 6) on MPD's clock; the owner hears Line 4 (5.5 s).
      seek(9.1, 'pause');
      await page.waitForSelector('#ytnSync .lyl.now');
      await sleep(1300);                                         // the pause reaches the page
      await page.click('#lyrSync');
      assert(await page.$eval('#lyrSync', e => e.getAttribute('aria-pressed')) === 'true', 'Sync not pressed');
      assert(await page.$eval('#lyrHint', e => !e.hidden), 'no hint');
      await page.click('#ytnSync .lyl:nth-child(4)');
      assert((await now(page)) === 'Line 4', 'after the tap: ' + await now(page) + ' at ' + await off());
      assert(await page.$eval('#lyrSync', e => e.getAttribute('aria-pressed')) === 'false', 'Sync still waiting');
      assert(await page.$eval('#lyrHint', e => e.hidden), 'hint still showing');
      const synced = await ms();
      assert(Math.abs(synced - (9.1 - 5.5 - 0.15) * 1000) <= 60, 'synced offset: ' + synced);
      await until(async () => (await getJson(radio.base, '/api/settings')).ytm.lyrics_offset_ms === synced, 3000, 'the synced offset saved');
      await sleep(1300);
      assert((await now(page)) === 'Line 4', 'a poll took the line back: ' + await now(page));
      await page.close();
      // Another load (or another phone): the radio's offset, the same line.
      page = await openLyrics(phone);
      await page.waitForSelector('#ytnSync .lyl.now');
      await until(async () => (await ms()) === synced, 3000, 'the offset after a reload');
      await until(async () => (await now(page)) === 'Line 4', 2000, 'Line 4 after a reload');
      // Playing: a tap makes the tapped line the one lit at once.
      seek(8.0, 'play');                          // Line 3 lights; the owner hears Line 5 (7.0 s);
      await until(async () => (await now(page)) !== 'Line 4', 3000, 'the song to move on');
      await page.click('#lyrSync');
      await page.click('#ytnSync .lyl:nth-child(5)');
      assert((await now(page)) === 'Line 5', 'playing, after the tap: ' + await now(page));
      await page.close();
      song.offset = 0;
    });

    await check('reduced motion: no smooth scroll, no colour fades', async () => {
      song.offset = 0;
      const calm = await browser.newContext({viewport: {width: 390, height: 844}, reducedMotion: 'reduce'});
      seek(4.6, 'play');
      const page = await openLyrics(calm);
      await page.waitForSelector('#ytnSync .lyl.now');
      const tr = await page.$eval('#ytnSync .lyl', e => getComputedStyle(e).transitionDuration);
      assert(tr.split(',').every(v => parseFloat(v) < 0.01), 'transition: ' + tr);
      seek(16.4);
      await until(async () => (await now(page)) === 'Line 11', 2500, 'Line 11');
      const d = await centred(page);              // the same frame: no glide
      assert(d !== null && d < 24, 'not centred at once: ' + d);
      await calm.close();
    });

    if (SHOTS){
      await check('desktop screenshots', async () => {
        const desk = await browser.newContext({viewport: {width: 1280, height: 860}, deviceScaleFactor: 1});
        lyrics = SYNCED; song.vid = 'w7pjt9ZH3NM'; song.title = 'La Perla'; seek(9.0, 'play');
        let page = await openLyrics(desk);
        await page.waitForSelector('#ytnSync .lyl.now'); await sleep(1500);
        await page.screenshot({path: path.join(SHOTS, 'desktop-synced.png')});
        await page.close();
        lyrics = PLAIN; song.vid = 'p3w_8a5w6Tk';
        page = await openLyrics(desk);
        await page.waitForSelector('#ytnLyrics .ytn-text');
        await page.screenshot({path: path.join(SHOTS, 'desktop-plain.png')});
        await desk.close();
      });
    }

    await check('no console errors', async () => {
      assert(!errors.length, errors.join('\n     '));
    });
  } finally {
    await browser.close();
    radio.proc.kill(); fs.rmSync(radio.tmp, {recursive: true, force: true});
  }
  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
