#!/usr/bin/env node
// The key page on an Excelsior: its keys drawn from the radio's config, key 0,
// the latched key, SP, the speaker selector, treble and bass; the key editor
// for latching keys (no hold, no combinations) and for a modified unit whose
// keys spring back (variant.d overlay `presets: {latching: false}`); and an HM
// beside them, to show its page is as it was. Runs fake radios (haslerd --fake)
// in Chrome.
//
//   cd daemon && node tests/e2e/excelsior.cjs
//
// Needs Node and Playwright (`npm i playwright`, or PLAYWRIGHT=/path/to/node_modules/playwright),
// and a browser: Playwright's own Chromium, or installed Chrome (PW_CHANNEL=chrome).
// HASLER_PY picks the Python that runs the fake radio (default .venv/bin/python).
// SHOTS=/some/dir also saves phone and desktop screenshots of both cabinets there.
'use strict';
const {spawn} = require('child_process');
const fs = require('fs'), os = require('os'), path = require('path'), net = require('net');
const {chromium} = require(process.env.PLAYWRIGHT || 'playwright');

const DAEMON = path.resolve(__dirname, '..', '..');
const PY = process.env.HASLER_PY || path.join(DAEMON, '.venv', 'bin', 'python');
const SHOTS = process.env.SHOTS || '';
const BANK = ['k1', 'k2', 'k3', 'k4', 'k5', 'k6', 'pu', 'tape'];
// Words that belong to the HM (or to keys that can be held) and must not show on a latching Excelsior.
const HM_ONLY = ['Hasler HM', 'Bern 1964', 'hold for its second job', 'Source key', 'source key', 'PU tap', 'Every pair of keys'];
const sleep = ms => new Promise(r => setTimeout(r, ms));
let failed = 0, passed = 0;

function freePort(){
  return new Promise((res, rej) => { const s = net.createServer(); s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => res(p)); }); s.on('error', rej); });
}
// variant: excelsior | hasler-hm; overlay: the variant.d file's text; closed: contacts closed at start.
async function startRadio({variant, overlay, closed}){
  const port = await freePort(), tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'hasler-e2e-'));
  fs.mkdirSync(path.join(tmp, 'etc', 'variant.d'), {recursive: true}); fs.mkdirSync(path.join(tmp, 'state'));
  fs.writeFileSync(path.join(tmp, 'etc', 'box.yaml'), `schema: 1\nvariant: ${variant}\nkeys_file: ${path.join(tmp, "state", "keys.yaml")}\nweb:\n  host: 127.0.0.1\n  port: ${port}\n`);
  if (overlay) fs.writeFileSync(path.join(tmp, 'etc', 'variant.d', variant + '.yaml'), overlay);
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
const bankKeys = page => page.$$eval('#bank button', bs => bs.map(b => ({id: b.dataset.key, cls: b.className, n: b.querySelector('.key-n').textContent})));
const visibleText = page => page.evaluate(() => document.body.innerText);

(async () => {
  const radios = {};
  radios.ex = await startRadio({variant: 'excelsior', closed: 'd10,d2,d3'});           // key 3 latched, SP down, speakers "both"
  radios.mod = await startRadio({variant: 'excelsior', overlay: 'schema: 1\ncontrols:\n  presets: {latching: false}\n'});
  radios.hm = await startRadio({variant: 'hasler-hm'});
  const browser = await launch();
  const errors = [];
  const phone = await browser.newContext({viewport: {width: 390, height: 844}, deviceScaleFactor: 2});
  const desk = await browser.newContext({viewport: {width: 1280, height: 900}});
  const open = async (radio, hash, ctx = phone) => {
    const page = await ctx.newPage();
    page.on('console', m => { if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(hash + ': ' + m.text()); });
    page.on('pageerror', e => errors.push(hash + ': ' + e.message));
    await page.goto(radio.base + '/' + hash);
    return page;
  };
  const shot = async (page, name) => { if (SHOTS){ fs.mkdirSync(SHOTS, {recursive: true}); await page.screenshot({path: path.join(SHOTS, name + '.png'), fullPage: true}); } };
  try {
    const ex = radios.ex, mod = radios.mod, hm = radios.hm;

    await check('excelsior: the config has the bank keys, key 0 fixed, no combinations', async () => {
      const c = await getJson(ex.base, '/api/config');
      assert(JSON.stringify(c.keys.map(k => k.id)) === JSON.stringify(BANK), 'keys ' + c.keys.map(k => k.id));
      assert(c.keys.every(k => k.latching && k.hold === null), 'not all latching without hold');
      assert(c.fixed_keys.length === 1 && c.fixed_keys[0].id === 'k0', 'fixed ' + JSON.stringify(c.fixed_keys));
      assert(c.combo_keys.length === 0 && c.title === 'Excelsior', 'combo keys / title');
    });

    await check('excelsior front page: key 0, eight keys, 3 latched, SP, speakers, treble and bass', async () => {
      const page = await open(ex, '#');
      await until(async () => (await page.$$('#bank button')).length === 9, 5000, 'nine keys');
      const keys = await bankKeys(page);
      assert(keys[0].id === 'k0' && /\brel\b/.test(keys[0].cls) && keys[0].n === '0', 'first key ' + JSON.stringify(keys[0]));
      assert(JSON.stringify(keys.slice(1).map(k => k.id)) === JSON.stringify(BANK), 'keys ' + keys.map(k => k.id));
      assert(keys.slice(1).map(k => k.n).join(' ') === '1 2 3 4 5 6 PU Tape', 'captions ' + keys.map(k => k.n));
      await until(async () => (await bankKeys(page)).find(k => k.id === 'k3').cls.includes('down'), 5000, 'key 3 drawn down');
      assert(!(await bankKeys(page)).find(k => k.id === 'k2').cls.includes('down'), 'key 2 down too');
      const line = await until(async () => { const el = await page.$('#cabLine'); return el && el.textContent(); }, 5000, 'the SP / speaker line');
      assert(line.includes('SP · On') && line.includes('Speakers · Both'), 'line: ' + line);
      const knobs = await page.$$eval('#cab .tone', rows => rows.map(r => r.dataset.knob));
      assert(knobs.join(',') === 'treble,bass', 'knob rows ' + knobs);
      assert(await page.isHidden('#tone'), 'the HM tone row shows');
      const hint = await page.textContent('#hintLine');
      assert(hint === 'Tap a key to play · 0 for standby', 'hint: ' + hint);
      await shot(page, 'excelsior-phone-front');
      await page.close();
    });

    await check('excelsior: tapping a key plays it; key 0 puts the radio on standby', async () => {
      const page = await open(ex, '#');
      await page.waitForSelector('#bank button[data-key="k5"]');
      await page.click('#bank button[data-key="k5"]');
      await until(async () => { const s = await getJson(ex.base, '/api/status'); return s.playing && s.playing.n === 5; }, 5000, 'preset 5 playing');
      await page.click('#bank button[data-key="k0"]');
      await until(async () => (await getJson(ex.base, '/api/status')).standby, 5000, 'standby');
      await page.close();
    });

    await check('excelsior key list: select wording, no hold, no combinations', async () => {
      const page = await open(ex, '#edit');
      await page.waitForSelector('.keylist li');
      assert((await page.$$('.keylist > li')).length === 8, (await page.$$('.keylist > li')).length + ' keys listed');
      const words = await page.$$eval('.keylist .kl-line.tap .kl-k', s => s.map(x => x.textContent));
      assert(words.length === 8 && words.every(w => w === 'Select'), 'tap rows: ' + words.join(' | '));
      assert(!(await page.$('.keylist .kl-line.hold')), 'a hold row');
      assert(!(await page.$('#sec-combos')), 'combinations offered');
      assert((await page.textContent('.field .hint')).includes('no hold'), 'hint: ' + await page.textContent('.field .hint'));
      await page.close();
    });

    await check('excelsior: a key is assignable and the save round-trips', async () => {
      const page = await open(ex, '#edit/tape');
      await page.waitForSelector('#renameBtn');
      assert((await page.textContent('.sheet-head .mono')) === 'Selected, plays', 'head: ' + await page.textContent('.sheet-head .mono'));
      assert(await page.$('#latchNote'), 'no latching note');
      assert(!(await page.$('#chg-hold')), 'a hold row');
      assert(!(await page.$('#slot-hold')) && !(await page.$('.slot-seg')), 'a Tap | Hold switch on a latching key');
      await page.goto(ex.base + '/#edit/k2/hold');
      await page.waitForSelector('#renameBtn');
      assert(!(await page.$('#slot-hold')) && !(await page.$('#holdName')), '#edit/k2/hold opened a hold');
      await page.goto(ex.base + '/#edit/tape');
      await page.waitForSelector('#renameBtn');
      await shot(page, 'excelsior-phone-edit-tape');
      await page.click('#renameBtn');
      await page.fill('.choice-txt input', 'Reel FM');
      await page.keyboard.press('Enter');
      await page.click('#saveBtn');
      await until(async () => (await page.textContent('.save-msg')).includes('saved to the radio'), 8000, 'saved');
      const c = await getJson(ex.base, '/api/config');
      const tape = c.keys.find(k => k.id === 'tape');
      assert(c.presets.find(p => p.n === tape.tap.n).name === 'Reel FM', 'radio has ' + JSON.stringify(tape));
      const keysYaml = fs.readFileSync(path.join(ex.tmp, 'state', 'keys.yaml'), 'utf8');
      assert(keysYaml.includes('tape.tap') && !keysYaml.includes('tape.hold'), 'keys.yaml:\n' + keysYaml);
      // And it plays from the phone.
      await page.goto(ex.base + '/#');
      await page.waitForSelector('#bank button[data-key="tape"]');
      await until(async () => (await page.textContent('#bank button[data-key="tape"] .key-l')).length > 0, 5000, 'tape caption');
      await page.click('#bank button[data-key="tape"]');
      await until(async () => { const s = await getJson(ex.base, '/api/status'); return s.playing && s.playing.name === 'Reel FM'; }, 5000, 'Reel FM playing');
      await page.close();
    });

    await check('excelsior key card lists its keys and key 0', async () => {
      const page = await open(ex, '#print');
      await page.waitForSelector('.kc-key');
      const ids = await page.$$eval('.kc-key', ks => ks.map(k => k.dataset.key));
      assert(JSON.stringify(ids) === JSON.stringify(BANK), 'card keys ' + ids);
      const text = await visibleText(page);
      assert(text.includes('press key 0'), 'no key 0 on the card');
      assert(!HM_ONLY.some(w => text.includes(w)), 'HM words on the card: ' + HM_ONLY.filter(w => text.includes(w)) + '\n' + text);
      await shot(page, 'excelsior-phone-card');
      await page.close();
    });

    await check('excelsior: no HM-only words anywhere', async () => {
      for (const hash of ['#', '#edit', '#edit/k1', '#edit/pu', '#settings', '#settings/keys', '#settings/sound']){
        const page = await open(ex, hash); await sleep(1200);
        const text = await visibleText(page);
        const bad = HM_ONLY.filter(w => text.includes(w));
        assert(!bad.length, hash + ' says ' + bad.join(', '));
        if (hash === '#settings'){
          assert(text.includes('Excelsior · restored 2026'), 'no model line on ' + hash);
          await shot(page, 'excelsior-phone-settings');
        }
        await page.close();
      }
    });

    await check('excelsior desktop', async () => {
      const page = await open(ex, '#', desk);
      await until(async () => (await page.$$('#bank button')).length === 9, 5000, 'nine keys');
      await page.waitForSelector('#cabLine');
      await sleep(500);
      await shot(page, 'excelsior-desktop-front');
      await page.goto(ex.base + '/#edit/k3'); await page.waitForSelector('#renameBtn'); await sleep(300);
      await shot(page, 'excelsior-desktop-edit-k3');
      await page.close();
    });

    await check('modified excelsior (keys spring back): key 0 is a key, holds and combinations', async () => {
      const c = await getJson(mod.base, '/api/config');
      assert(JSON.stringify(c.keys.map(k => k.id)) === JSON.stringify(['k0', ...BANK]), 'keys ' + c.keys.map(k => k.id));
      assert(!c.keys.some(k => k.latching) && c.fixed_keys.length === 0 && c.combo_keys.length === 9, 'config');
      const page = await open(mod, '#');
      await until(async () => (await page.$$('#bank button')).length === 9, 5000, 'nine keys');
      const keys = await bankKeys(page);
      assert(keys[0].id === 'k0' && !/\brel\b/.test(keys[0].cls), 'key 0 ' + JSON.stringify(keys[0]));
      assert((await page.textContent('#hintLine')) === 'Tap a key to play · hold for its second job', 'hint');
      await shot(page, 'excelsior-modified-phone-front');
      await page.goto(mod.base + '/#edit');
      await page.waitForSelector('#sec-combos');
      assert((await page.$$('.keylist .kl-line.hold')).length === 9, 'hold rows');
      const words = await page.$$eval('.keylist .kl-line.tap .kl-k', s => s.map(x => x.textContent));
      assert(words.every(w => w === 'Tap'), 'tap rows ' + words.join(' | '));
      await page.goto(mod.base + '/#edit/k2');
      await page.waitForSelector('#slot-hold');
      assert(await page.$('#slot-tap'), 'no Tap tab');
      await page.click('#slot-hold');
      await page.waitForSelector('#chg-hold');
      await page.goto(mod.base + '/#edit/k2');
      await page.waitForSelector('#slot-tap');
      assert((await page.textContent('.sheet-head .mono')) === 'Tap plays', 'head');
      await page.close();
    });

    await check('HM: as it was', async () => {
      const page = await open(hm, '#');
      await until(async () => (await page.$$('#bank button')).length === 7, 5000, 'seven keys');
      const keys = await bankKeys(page);
      assert(keys.map(k => k.n).join(' ') === '1 2 3 4 5 6 PU', 'keys ' + keys.map(k => k.n));
      assert(!keys.some(k => /\brel\b/.test(k.cls)), 'a release key');
      assert(await page.isHidden('#cab'), 'the cabinet line shows');
      assert((await page.textContent('#hintLine')) === 'Tap a key to play · hold for its second job', 'hint');
      assert(!(await page.$eval('#bank', b => b.classList.contains('wide-bank'))), 'packed bank');
      await shot(page, 'hm-phone-front');
      await page.goto(hm.base + '/#settings'); await sleep(1200);
      assert((await visibleText(page)).includes('Hasler HM · Bern 1964 · restored 2026'), 'model line');
      await page.goto(hm.base + '/#edit');
      await page.waitForSelector('#sec-combos');
      await shot(page, 'hm-phone-edit');
      await page.goto(hm.base + '/#edit/k1'); await page.waitForSelector('#slot-hold');
      assert((await page.textContent('.sheet-head .mono')) === 'Tap plays', 'head');
      await page.close();
      const d = await open(hm, '#', desk);
      await until(async () => (await d.$$('#bank button')).length === 7, 5000, 'seven keys');
      await sleep(500);
      await shot(d, 'hm-desktop-front');
      await d.close();
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
