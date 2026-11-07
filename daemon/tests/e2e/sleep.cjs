#!/usr/bin/env node
// The sleep timer on the key page: the clock beside Standby, 15 min, the chip,
// cancelling with Undo, a key on the radio cancelling it, and Standby's tap
// staying instant. Runs the page against the fake radio (haslerd --fake) in Chrome.
//
//   cd daemon && node tests/e2e/sleep.cjs
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

(async () => {
  const radio = await startRadio();
  const send = (method, url, body) => fetch(radio.base + url, {method, headers: {'Content-Type': 'application/json'}, body: body && JSON.stringify(body)});
  const sleepDoc = async () => (await (await fetch(radio.base + '/api/status')).json()).sleep;
  // The fake's power knob reads off at boot: let that settle, then wake it (no timer in standby).
  await sleep(800);
  await send('POST', '/api/standby', {on: false});
  const browser = await launch();
  const errors = [];
  try {
    for (const [name, viewport] of [['phone', {width: 390, height: 844}], ['desktop', {width: 1280, height: 900}]]){
      const ctx = await browser.newContext({viewport});
      const page = await ctx.newPage();
      page.on('pageerror', e => errors.push(name + ': ' + e.message));
      page.on('console', m => { if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(name + ': ' + m.text()); });
      await page.goto(radio.base + '/');
      await until(() => page.isVisible('#sleepBtn'), 5000, 'the sleep timer button');

      await check(name + ': set 15 min -> the chip shows', async () => {
        await page.click('#sleepBtn');
        await until(() => page.isVisible('#sleepDlg'), 2000, 'the sleep dialog');
        assert(await page.evaluate(() => document.activeElement && document.activeElement.dataset.min === '15'), 'focus not on 15 min');
        await page.click('#sleepDlg [data-min="15"]');
        await until(() => page.isVisible('#sleepChip'), 3000, 'the chip');
        const txt = (await page.textContent('#sleepChip')).trim();
        assert(/Sleep · 1[45] min/.test(txt), 'chip says ' + JSON.stringify(txt));
        const s = await sleepDoc();
        assert(s && s.minutes === 15 && s.ends_in_s > 840 && !s.fading, 'radio says ' + JSON.stringify(s));
        assert(await page.getAttribute('#sleepBtn', 'aria-pressed') === 'true', 'clock not pressed');
      });

      await check(name + ': a tap on the chip cancels, Undo sets it back', async () => {
        await page.click('#sleepChip');
        await until(async () => !(await page.isVisible('#sleepChip')), 2000, 'the chip to go');
        assert(await sleepDoc() === null, 'still set on the radio');
        await page.click('.toast-undo');
        await until(() => page.isVisible('#sleepChip'), 3000, 'the chip back');
        const s = await sleepDoc();
        assert(s && s.ends_in_s > 840, 'undo set ' + JSON.stringify(s));
      });

      await check(name + ': keyboard: open, Escape, then turn it off', async () => {
        await page.focus('#sleepBtn');
        await page.keyboard.press('Enter');
        await until(() => page.isVisible('#sleepDlg'), 2000, 'the dialog');
        await page.keyboard.press('Escape');
        await until(async () => !(await page.isVisible('#sleepDlg')), 2000, 'the dialog to close');
        assert(await page.evaluate(() => document.activeElement && document.activeElement.id === 'sleepBtn'), 'focus not back on the clock');
        await page.keyboard.press('Enter');
        await page.click('#sleepOff');
        await until(async () => !(await page.isVisible('#sleepChip')), 3000, 'the chip to go');
        assert(await sleepDoc() === null, 'still set');
      });

      await check(name + ': a key on the radio cancels it', async () => {
        assert((await send('POST', '/api/sleep', {minutes: 30})).ok, 'set failed');
        await until(() => page.isVisible('#sleepChip'), 4000, 'the chip from the poll');
        await send('POST', '/api/press', {key: name === 'phone' ? 'k2' : 'k3'});   // a key not playing: k2 again would be standby
        await until(async () => !(await page.isVisible('#sleepChip')), 4000, 'the chip to go');
      });

      await check(name + ': Standby is still one tap, and hides the clock', async () => {
        await page.click('#standbyBtn');
        await until(async () => (await (await fetch(radio.base + '/api/status')).json()).standby, 2000, 'standby');
        assert(!(await page.isVisible('#sleepDlg')), 'a dialog opened');
        await until(async () => !(await page.isVisible('#sleepBtn')), 3000, 'the clock to hide');
        await page.click('#standbyBtn');
        await until(() => page.isVisible('#sleepBtn'), 4000, 'the clock back');
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
