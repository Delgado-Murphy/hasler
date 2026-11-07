#!/usr/bin/env node
// Key page extras against the fake radio (haslerd --fake) in Chrome: the
// home-screen manifest, the owner's own key-cap label, the printable key card.
//
//   cd daemon && node tests/e2e/extras.cjs
//
// Same needs as net_resilience.cjs: Node, Playwright (PLAYWRIGHT=/path/to/node_modules/playwright),
// a browser (PW_CHANNEL=chrome for installed Chrome), HASLER_PY for the fake radio's Python.
// SHOTS=/some/dir also saves screenshots of the key caps and both card sizes there.
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
const shot = async (target, name) => { if (SHOTS){ fs.mkdirSync(SHOTS, {recursive: true}); await target.screenshot({path: path.join(SHOTS, name)}); } };

(async () => {
  const radio = await startRadio();
  const browser = await launch();
  const errors = [];
  const ctx = await browser.newContext({viewport: {width: 390, height: 844}, deviceScaleFactor: 2});
  const fresh = async hash => {
    const page = await ctx.newPage();
    page.on('console', m => { if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(hash + ': ' + m.text()); });
    page.on('pageerror', e => errors.push(hash + ': ' + e.message));
    await page.goto(radio.base + '/' + hash);
    return page;
  };
  const config = async () => (await fetch(radio.base + '/api/config')).json();
  try {
    await check('manifest: linked from the page, installable, its icons load', async () => {
      const page = await fresh('#');
      const href = await page.getAttribute('link[rel="manifest"]', 'href');
      assert(href === '/manifest.webmanifest', 'manifest link ' + href);
      const touch = await page.getAttribute('link[rel="apple-touch-icon"]', 'href');
      const m = await page.evaluate(async h => (await fetch(h)).json(), href);
      assert(/^Hasler/.test(m.name) && m.display === 'standalone' && m.start_url === '/', JSON.stringify(m));
      for (const src of [...m.icons.map(i => i.src), touch]){
        const ok = await page.evaluate(src => new Promise(res => { const i = new Image(); i.onload = () => res(i.naturalWidth); i.onerror = () => res(0); i.src = src; }), src);
        assert(ok > 0, 'icon did not load: ' + src);
      }
      await page.close();
    });

    await check('short label: set under Rename, saved, shown on the key cap', async () => {
      const page = await fresh('#edit/k1');
      await page.waitForSelector('#renameBtn');
      await page.click('#renameBtn');
      await page.waitForSelector('#shortInp');
      // moving from the name to the label keeps the rename open
      await page.focus('.choice-txt input');
      await page.click('#shortInp');
      assert(await page.$('#shortInp'), 'rename closed when moving to the label field');
      await page.fill('#shortInp', 'My Classic');
      await page.keyboard.press('Enter');
      await until(async () => /key: My Classic/.test(await page.textContent('.choice-type')), 3000, 'label in the summary');
      await page.click('#saveBtn');
      await until(async () => (await page.textContent('.save-msg')).includes('saved to the radio'), 8000, 'saved message');
      const c = await config(), k1 = c.keys.find(k => k.id === 'k1');
      const p = c.presets.find(x => x.n === k1.tap.n);
      assert(p.short === 'My Classic', 'radio has short ' + JSON.stringify(p.short));
      await page.goto(radio.base + '/#');
      await until(async () => (await page.textContent('.key[data-key="k1"] .key-l')) === 'My Classic', 5000, 'the key cap');
      const k2 = await page.textContent('.key[data-key="k2"] .key-l');
      assert(k2 && k2 !== 'My Classic', 'other keys keep their shortened names: ' + k2);
      await shot(await page.$('#bank'), 'key-caps.png');
      // too long: the field stops at the limit
      await page.goto(radio.base + '/#edit/k1');
      await page.waitForSelector('#renameBtn');
      await page.click('#renameBtn');
      await page.fill('#shortInp', 'Much too long a label');
      assert((await page.inputValue('#shortInp')).length === (c.short_max || 12), 'maxlength not applied');
      await page.keyboard.press('Escape');
      await page.close();
    });

    await check('key card: from Edit keys, lists every key, prints without the app around it', async () => {
      const page = await fresh('#edit');
      await page.waitForSelector('#printLink');
      await page.click('#printLink');
      await page.waitForSelector('#keyCard');
      const c = await config();
      const ids = await page.$$eval('#keyCard .kc-key', els => els.map(e => e.dataset.key));
      assert(JSON.stringify(ids) === JSON.stringify(c.keys.map(k => k.id)), 'card keys ' + ids.join(','));
      const k1 = await page.textContent('#keyCard .kc-key[data-key="k1"] .kc-tap');
      const p1 = c.presets.find(x => x.n === c.keys[0].tap.n);
      assert(k1 === p1.name, 'key 1 says ' + k1);
      const holds = await page.$$eval('#keyCard .kc-hold', els => els.map(e => e.textContent));
      assert(holds.some(t => /Volume up/.test(t)) && holds.some(t => /Pair a phone/.test(t)), 'holds: ' + holds.join(' | '));
      assert((await page.textContent('#keyCard')).includes('Delgado'), 'no maker’s mark');
      const off = await page.$('#keyCard .kc-power');
      assert(off && /tone knob/.test(await off.textContent()), 'no switching-off line');
      await shot(await page.$('#keyCard'), 'card-a6.png');
      await page.click('[data-size="strip"]');
      await page.waitForSelector('#keyCard.kc-strip');
      assert((await page.$$('#keyCard .kc-key')).length === c.keys.length, 'strip keys');
      await shot(await page.$('#keyCard'), 'card-strip.png');
      // print media: only the card
      await page.emulateMedia({media: 'print'});
      const shown = await page.evaluate(() => ['remote', 'toast'].map(id => getComputedStyle(document.getElementById(id)).display)
        .concat(getComputedStyle(document.querySelector('.print-tools')).display));
      assert(shown.every(d => d === 'none'), 'app chrome when printing: ' + shown.join(','));
      assert(await page.evaluate(() => getComputedStyle(document.getElementById('keyCard')).display !== 'none'), 'card hidden when printing');
      if (SHOTS) await page.pdf({path: path.join(SHOTS, 'card-strip.pdf'), landscape: true, format: 'A4'}).catch(() => {});
      await page.emulateMedia({media: 'screen'});
      await page.click('[data-size="a6"]');
      if (SHOTS){ await page.emulateMedia({media: 'print'}); await page.pdf({path: path.join(SHOTS, 'card-a6.pdf'), width: '105mm', height: '148mm'}).catch(() => {}); }
      // window.print() is what the button calls
      await page.emulateMedia({media: 'screen'});
      await page.evaluate(() => { window.__printed = 0; window.print = () => { window.__printed++; }; });
      await page.click('#printBtn');
      assert(await page.evaluate(() => window.__printed) === 1, 'print button');
      await page.close();
    });

    await check('sound: the ceiling slider stops at the maker’s cap and says so', async () => {
      const page = await fresh('#settings/sound');
      await page.waitForSelector('#limit');
      await until(async () => (await page.getAttribute('#limit', 'max')) === '90', 5000, 'the slider max');
      assert((await page.textContent('#limitHint')).includes('the maker set the top at 90'), await page.textContent('#limitHint'));
      const s = await (await fetch(radio.base + '/api/settings')).json();
      assert(JSON.stringify(s.volume_limit_range) === '[30,90]' && s.volume_limit_set === false, JSON.stringify([s.volume_limit_range, s.volume_limit_set]));
      const r = await fetch(radio.base + '/api/settings', {method: 'PUT', headers: {'Content-Type': 'application/json', Host: 'localhost'}, body: JSON.stringify({volume_limit: 95})});
      assert(r.status === 400 && /maker set the top at 90/.test((await r.json()).error), 'PUT 95 was not refused');
      await page.close();
    });

    await check('this radio: How the radio is, in words, and Restart asks for a key press', async () => {
      const page = await fresh('#settings/radio');
      await page.waitForSelector('#sec-how');
      await until(async () => /Wi-Fi/.test(await page.textContent('#howBox')) && /Running for/.test(await page.textContent('#howBox')), 8000, 'the card’s rows');
      const txt = await page.textContent('#howBox');
      assert(/Internet/.test(txt) && /Memory card/.test(txt) && /Last switched off/.test(txt) && /Player/.test(txt), txt);
      const d = await (await fetch(radio.base + '/api/diagnostics')).json();
      assert(d.last_shutdown && d.last_shutdown.kind === 'unknown' && Array.isArray(d.faults) && 'online' in d, JSON.stringify(d.last_shutdown));
      await page.click('#restartBtn');
      await page.waitForSelector('#dlg[open]');
      assert(/Restart the radio\?/.test(await page.textContent('#dlgTitle')), await page.textContent('#dlgTitle'));
      assert(/Press any key on the radio/.test(await page.textContent('#dlg')), 'no press prompt');
      await page.click('#dlg button:has-text("Cancel")');
      await until(async () => !(await page.$('#dlg[open]')), 3000, 'the sheet to close');
      const st = await (await fetch(radio.base + '/api/status')).json();
      assert(Array.isArray(st.fault_reasons) && 'online' in st && 'fault_cleared_at' in st, 'status fields');
      await shot(await page.$('#sec-how'), 'how-the-radio-is.png');
      await page.close();
    });

    await check('key card: the back button returns to Edit keys; no console errors', async () => {
      const before = errors.length;
      const page = await fresh('#print');
      await page.waitForSelector('#keyCard');
      await page.click('#screenTop button');
      await until(async () => page.evaluate(() => document.getElementById('app').dataset.view === 'edit'), 3000, 'edit view');
      await sleep(500);
      await page.close();
      assert(errors.length === before, errors.slice(before).join('\n     '));
    });
  } finally {
    await browser.close();
    radio.proc.kill();
    fs.rmSync(radio.tmp, {recursive: true, force: true});
  }
  if (errors.length) console.log('console errors during the run:\n  ' + errors.join('\n  '));
  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
