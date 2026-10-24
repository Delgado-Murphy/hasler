#!/usr/bin/env node
// The key page on a flaky phone connection: lost requests, lost answers, a
// radio out of reach for a while. Runs the page against the fake radio
// (haslerd --fake) in Chrome and breaks the network with Playwright routes.
//
//   cd daemon && node tests/e2e/net_resilience.cjs
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
const NET_ERR = 'That didn’t reach the radio. Try again in a moment.';
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
const dialHead = page => page.textContent('#dialHead');

(async () => {
  const radio = await startRadio();
  const browser = await launch();
  const errors = [], http = new Set();
  const ctx = await browser.newContext({viewport: {width: 390, height: 844}});
  const fresh = async hash => {
    const page = await ctx.newPage();
    // HTTP errors are the fake radio's own answers (no updates, no Home Assistant…) or the
    // aborts above; what counts is script trouble.
    page.on('console', m => { if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(hash + ': ' + m.text()); });
    page.on('response', r => { if (r.status() >= 400) http.add(r.status() + ' ' + r.request().method() + ' ' + new URL(r.url()).pathname); });
    page.on('pageerror', e => errors.push(hash + ': ' + e.message));
    await page.goto(radio.base + '/' + hash);
    return page;
  };
  try {
    // A record of everything the dial said, so a one-poll flash can't slip by.
    const watchDial = page => page.evaluate(() => {
      window.__dial = [];
      const note = () => window.__dial.push(document.getElementById('dialHead').textContent + '|' + document.getElementById('glass').className + '|' + document.getElementById('dialMark').textContent);
      new MutationObserver(note).observe(document.getElementById('glass'), {subtree: true, childList: true, characterData: true, attributes: true});
    });

    await check('one lost /api/status: no "Can’t reach", no dimming', async () => {
      const page = await fresh('#');
      await until(async () => (await dialHead(page)) !== 'Tuning…', 5000, 'first status');
      await watchDial(page);
      let n = 0;
      await page.route('**/api/status', r => (n++ === 0 ? r.abort() : r.continue()));
      await sleep(3500);
      const seen = await page.evaluate(() => window.__dial);
      assert(n >= 2, 'status was polled ' + n + ' times');
      assert(!seen.some(s => s.includes('Can’t reach') || / offline/.test(s)), 'dial went offline: ' + seen.filter(s => /Can’t|offline/.test(s))[0]);
      assert(seen.some(s => s.includes('Reconnecting…')), 'no "Reconnecting…" while a poll was missing');
      assert(!(await page.textContent('#dialMark')).includes('Reconnecting'), 'still says Reconnecting after the next answer');
      await page.close();
    });

    await check('radio out of reach 7.5 s: says so, recovers', async () => {
      const page = await fresh('#');
      await until(async () => (await dialHead(page)) !== 'Tuning…', 5000, 'first status');
      await page.route('**/api/status', r => r.abort());
      const t0 = Date.now();
      await until(async () => (await dialHead(page)) === 'Can’t reach the radio', 12000, '"Can’t reach the radio"');
      const offAt = Date.now() - t0;
      assert(offAt >= 6000, 'offline after only ' + offAt + ' ms');
      assert(await page.evaluate(() => document.getElementById('glass').classList.contains('offline')), 'glass not dimmed');
      assert((await page.textContent('#songTitle')).includes('home Wi-Fi'), 'song line: ' + await page.textContent('#songTitle'));
      await sleep(Math.max(0, 7500 - offAt));
      await page.unroute('**/api/status');
      const t1 = Date.now();
      await until(async () => (await dialHead(page)) !== 'Can’t reach the radio', 5000, 'recovery');
      const back = Date.now() - t1;
      console.log('     offline after ' + offAt + ' ms; back ' + back + ' ms after the radio answered again');
      assert(back <= 3500, 'recovery took ' + back + ' ms');
      await page.close();
    });

    // Rename key 1's preset in the editor and save it.
    const renameAndSave = async (page, name) => {
      await page.click('#renameBtn');
      await page.fill('.choice-txt input', name);
      await page.keyboard.press('Enter');
      await page.click('#saveBtn');
    };
    const keyName = async id => {
      const c = await (await fetch(radio.base + '/api/config')).json();
      const k = c.keys.find(x => x.id === id);
      return {p: c.presets.find(p => p.n === k.tap.n), all: c.presets};
    };

    await check('save: first PUT lost, the retry lands once, says saved', async () => {
      const page = await fresh('#edit/k1');
      await page.waitForSelector('#renameBtn');
      let puts = 0, reached = 0;
      await page.route('**/api/config', r => {
        if (r.request().method() !== 'PUT') return r.continue();
        if (puts++ === 0) return r.abort();
        reached++; return r.continue();
      });
      await renameAndSave(page, 'Retry FM');
      await until(async () => (await page.textContent('.save-msg')).includes('saved to the radio'), 8000, 'saved message');
      assert(puts === 2 && reached === 1, puts + ' PUTs sent, ' + reached + ' reached the radio');
      assert(!(await page.textContent('.save-msg')).includes('Couldn’t'), 'error shown');
      assert((await keyName('k1')).p.name === 'Retry FM', 'radio has ' + (await keyName('k1')).p.name);
      await page.close();
    });

    await check('save: answer lost but the radio saved: recognised as saved', async () => {
      const page = await fresh('#edit/k1');
      await page.waitForSelector('#renameBtn');
      let puts = 0;
      await page.route('**/api/config', async r => {
        if (r.request().method() !== 'PUT') return r.continue();
        if (puts++ === 0) await r.fetch();   // it reaches the radio; the answer doesn't come back
        return r.abort();
      });
      const before = (await keyName('k1')).all.length;
      await renameAndSave(page, 'Lost Reply FM');
      await until(async () => /saved to the radio|Couldn’t save/.test(await page.textContent('.save-msg')), 12000, 'an outcome');
      const msg = await page.textContent('.save-msg');
      assert(msg.includes('saved to the radio'), 'said: ' + msg);
      assert(await page.textContent('#saveBtn') === 'Saved ✓', 'button: ' + await page.textContent('#saveBtn'));
      const k = await keyName('k1');
      assert(k.p.name === 'Lost Reply FM', 'radio has ' + k.p.name);
      assert(k.all.length === before && k.all.filter(p => p.name === 'Lost Reply FM').length === 1, 'presets ' + before + ' -> ' + k.all.length);
      await page.close();
    });

    await check('save: no answer at all: says so, changes kept, Try again', async () => {
      const page = await fresh('#edit/k1');
      await page.waitForSelector('#renameBtn');
      await page.route('**/api/config', r => (r.request().method() === 'PUT' ? r.abort() : r.continue()));
      await renameAndSave(page, 'Never FM');
      await until(async () => (await page.textContent('.save-msg')).includes('Couldn’t save'), 12000, 'the failure');
      assert(await page.textContent('#saveBtn') === 'Try again', 'button: ' + await page.textContent('#saveBtn'));
      assert((await page.textContent('.choice-name')) === 'Never FM', 'draft lost');
      assert((await keyName('k1')).p.name !== 'Never FM', 'radio has it');
      await page.unroute('**/api/config');
      await page.click('#saveBtn');
      await until(async () => (await page.textContent('.save-msg')).includes('saved to the radio'), 8000, 'saved on Try again');
      await page.close();
    });

    await check('a key press that gets lost is sent once, and says so', async () => {
      const page = await fresh('#');
      await page.waitForSelector('#bank button');
      let n = 0;
      await page.route('**/api/press', r => { n++; return r.abort(); });
      await page.click('#bank button >> nth=1');
      await until(async () => (await page.textContent('#toast')).includes(NET_ERR), 4000, 'error toast');
      await sleep(2500);
      assert(n === 1, n + ' press requests');
      await page.close();
    });

    await check('the phone back online: polls at once', async () => {
      const page = await fresh('#settings');     // on a phone, Settings polls every 4 s
      await sleep(1500);
      const times = [];
      await page.route('**/api/status', r => { times.push(Date.now()); return r.continue(); });
      await until(async () => times.length, 6000, 'a scheduled poll');
      await sleep(200);
      const t = Date.now(), had = times.length;
      await page.evaluate(() => window.dispatchEvent(new Event('online')));
      await until(async () => times.length > had, 1000, 'a poll after "online"');
      assert(times[had] - t < 500, 'poll came ' + (times[had] - t) + ' ms later');
      await page.close();
    });

    await check('a call carrying a confirm token is never sent twice', async () => {
      const page = await ctx.newPage();
      page.on('pageerror', e => errors.push('updates: ' + e.message));
      await page.route('**/api/software', r => r.request().method() === 'GET'
        ? r.fulfill({json: {ok: true, installed: true, version: '1.1.1', channel: 'beta', update_mode: 'ask', available: null, history: []}}) : r.continue());
      await page.route('**/api/confirm/request', r => r.fulfill({json: {ok: true, token: 'tok', state: 'confirmed'}}));
      let n = 0, body = null;
      await page.route('**/api/software/settings', r => { n++; body = r.request().postDataJSON(); return r.abort(); });
      await page.goto(radio.base + '/#settings/radio/updates');
      const btn = page.getByRole('radio', {name: /Stable/});
      await btn.click();
      await until(async () => n > 0, 4000, 'the channel switch');
      await sleep(3000);
      assert(body && body.confirm === 'tok', 'body ' + JSON.stringify(body));
      assert(n === 1, n + ' requests');
      await page.close();
    });

    await check('no console errors on the main views', async () => {
      const before = errors.length;
      for (const hash of ['#', '#browse', '#browse/ytm', '#settings', '#settings/sound', '#settings/radio', '#settings/radio/updates', '#edit/k1']){
        const page = await fresh(hash); await sleep(1500); await page.close();
      }
      assert(errors.length === before, errors.slice(before).join('\n     '));
    });
  } finally {
    await browser.close();
    radio.proc.kill();
    fs.rmSync(radio.tmp, {recursive: true, force: true});
  }
  if (errors.length) console.log('console errors during the run:\n  ' + errors.join('\n  '));
  if (http.size) console.log('HTTP errors from the fake radio (expected):\n  ' + [...http].join('\n  '));
  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
