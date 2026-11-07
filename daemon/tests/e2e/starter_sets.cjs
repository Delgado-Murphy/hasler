#!/usr/bin/env node
// Starter sets on the key page: the "Start from a set…" sheet at the bottom of
// Edit keys, choosing a set (every key changes, the radio remembers it, Reset
// goes back to it), on a Hasler HM and on an Excelsior (seven keys, Tape), and
// a radio whose box.yaml names a set, and one that names a set 1.6 folded into
// another (pop -> espana). Runs fake radios (haslerd --fake) in
// Chrome; box.yaml's web.allow_unconfirmed stands in for the key press.
//
//   cd daemon && node tests/e2e/starter_sets.cjs
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
const SETS = ['little', 'family', 'teens', 'indie', 'eclectic', 'classical', 'jazz', 'oldies', 'espana', 'world'];
const GROUPS = ['Little ones', 'Kids', 'Teens & twenties', 'Grown-ups'];
const sleep = ms => new Promise(r => setTimeout(r, ms));
let failed = 0, passed = 0;

function freePort(){
  return new Promise((res, rej) => { const s = net.createServer(); s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => res(p)); }); s.on('error', rej); });
}
// variant: hasler-hm | excelsior; starter: box.yaml's set, if any.
async function startRadio({variant, starter}){
  const port = await freePort(), tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'hasler-e2e-'));
  fs.mkdirSync(path.join(tmp, 'etc')); fs.mkdirSync(path.join(tmp, 'state'));
  // state_file: a state dir, so settings.json (the remembered set) exists as on a radio.
  fs.writeFileSync(path.join(tmp, 'etc', 'box.yaml'), `schema: 1\nvariant: ${variant}\n${starter ? 'starter: ' + starter + '\n' : ''}keys_file: ${path.join(tmp, 'state', 'keys.yaml')}\nstate_file: ${path.join(tmp, 'state', 'state.json')}\nweb:\n  host: 127.0.0.1\n  port: ${port}\n  allow_unconfirmed: true\n`);
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
const getJson = async (base, p) => (await fetch(base + p)).json();
const send = (base, method, url, body) => fetch(base + url, {method, headers: {'Content-Type': 'application/json', Origin: base}, body: JSON.stringify(body)});
const presetName = (cfg, n) => (cfg.presets.find(p => p.n === n) || {}).name;

(async () => {
  const hm = await startRadio({variant: 'hasler-hm'});
  const ex = await startRadio({variant: 'excelsior'});
  const boxed = await startRadio({variant: 'hasler-hm', starter: 'family'});
  const folded = await startRadio({variant: 'hasler-hm', starter: 'pop'});
  const browser = await launch();
  const errors = [];
  const phone = await browser.newContext({viewport: {width: 390, height: 844}, deviceScaleFactor: 2});
  const open = async (radio, hash) => {
    const page = await phone.newPage();
    page.on('console', m => { if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(hash + ': ' + m.text()); });
    page.on('pageerror', e => errors.push(hash + ': ' + e.message));
    await page.goto(radio.base + '/' + hash);
    return page;
  };
  // Edit keys → Start from a set…: the sheet, with every set and the one on the keys marked.
  const openSheet = async page => {
    await page.waitForSelector('#startSet');
    await page.click('#startSet');
    await page.waitForSelector('#setsDlg[open] .ss-set');
    return page.$$eval('#setsDlg .ss-set', els => els.map(el => ({
      id: el.dataset.set, on: el.classList.contains('on'), title: el.querySelector('h3').textContent,
      now: (el.querySelector('.ss-now') || {}).textContent || '', keys: [...el.querySelectorAll('.ss-keys li')].map(li => li.querySelector('.ss-n').textContent + ' ' + li.querySelector('.ss-name').textContent),
      glyphs: [...el.querySelectorAll('.ss-g')].map(g => g.getAttribute('aria-label')), button: el.querySelector('.btn').textContent, primary: el.querySelector('.btn').classList.contains('primary')})));
  };
  try {
    await check('the config lists the ten sets, grouped, the eclectic one on the keys', async () => {
      const c = await getJson(hm.base, '/api/config');
      assert(c.starter === 'eclectic' && c.starter_from === 'default' && c.factory === true, 'starter ' + JSON.stringify([c.starter, c.starter_from, c.factory]));
      assert(JSON.stringify(c.starters.map(s => s.id)) === JSON.stringify(SETS), 'sets ' + c.starters.map(s => s.id));
      assert(c.starters.every(s => s.keys.length === 6 && s.title && s.blurb && s.group), 'six keys per set on the HM');
      assert(JSON.stringify([...new Set(c.starters.map(s => s.group))]) === JSON.stringify(GROUPS), 'groups ' + c.starters.map(s => s.group));
      assert(presetName(c, 1) === 'FIP' && presetName(c, 3) === 'Coffee Shop Blend', 'eclectic keys ' + c.presets.map(p => p.name));
      assert(c.presets[0].station === 'fr-fip' && c.presets[0].uri.startsWith('https://'), 'station id on key 1: ' + JSON.stringify(c.presets[0]));
    });

    await check('HM: the sheet from Edit keys, choose Jazz & soul, the keys change', async () => {
      const page = await open(hm, '#edit');
      await page.waitForSelector('.keylist li');
      const row = await page.textContent('#sec-reset');
      assert(row.includes('Reset keys to defaults') && row.includes('Start from a set…') && row.includes('(Eclectic)'), 'bottom row: ' + row);
      const sets = await openSheet(page);
      assert(JSON.stringify(sets.map(s => s.id)) === JSON.stringify(SETS), 'sheet sets ' + sets.map(s => s.id));
      const heads = await page.$$eval('#setsDlg .ss-group', els => els.map(e => e.textContent));
      assert(JSON.stringify(heads) === JSON.stringify(GROUPS), 'group headings ' + heads);
      const cur = sets.find(s => s.on);
      assert(cur && cur.id === 'eclectic' && cur.now === 'On the keys now' && !cur.primary, 'current set ' + JSON.stringify(cur));
      assert(sets.filter(s => !s.on).every(s => s.primary && s.button === 'Use this set'), 'the other sets have a primary button');
      const jazz = sets.find(s => s.id === 'jazz');
      assert(jazz.title === 'Jazz & soul' && jazz.keys.length === 6 && jazz.keys[0] === '1 Radio Swiss Jazz' && jazz.keys[5] === '6 Dinner Party Jazz', 'jazz keys ' + jazz.keys);
      assert(jazz.glyphs[0] === 'Radio' && jazz.glyphs[2] === 'YouTube Music', 'source glyphs ' + jazz.glyphs);
      await page.click('#setsDlg [data-choose="jazz"]');
      await until(async () => !(await page.$('#setsDlg')), 5000, 'the sheet to close');
      await until(async () => (await page.textContent('#toast')).includes('Jazz & soul is on the keys'), 5000, 'the toast');
      const c = await getJson(hm.base, '/api/config');
      assert(c.starter === 'jazz' && c.starter_from === 'page' && c.factory === true, 'after: ' + JSON.stringify([c.starter, c.starter_from, c.factory]));
      assert(presetName(c, 1) === 'Radio Swiss Jazz' && presetName(c, 2) === 'TSF Jazz' && presetName(c, 6) === 'Dinner Party Jazz', 'jazz on the keys: ' + c.presets.map(p => p.name));
      const keysYaml = fs.readFileSync(path.join(hm.tmp, 'state', 'keys.yaml'), 'utf8');
      assert(keysYaml.includes('defaults_rev:') && keysYaml.includes('shipped:') && keysYaml.includes('station: ch-radio-swiss-jazz'), 'keys.yaml:\n' + keysYaml);
      assert(JSON.parse(fs.readFileSync(path.join(hm.tmp, 'state', 'settings.json'), 'utf8')).starter === 'jazz', 'settings.json remembers the set');
      // The page shows the new keys, and the sheet now marks Jazz & soul.
      const listed = await page.$$eval('.keylist .kl-line.tap .kl-v', s => s.map(x => x.textContent));
      assert(listed[0].startsWith('Radio Swiss Jazz') && listed[1].startsWith('TSF Jazz'), 'key list: ' + listed.join(' | '));
      const again = await openSheet(page);
      assert(again.find(s => s.on).id === 'jazz' && again.find(s => s.id === 'eclectic').primary, 'jazz marked now: ' + JSON.stringify(again.map(s => [s.id, s.on])));
      await page.keyboard.press('Escape');
      await page.close();
    });

    await check('HM: a changed key is not factory; Reset goes back to the chosen set', async () => {
      const c0 = await getJson(hm.base, '/api/config');
      const presets = c0.presets.map(p => p.n === 1 ? Object.assign({}, p, {name: 'My station', uri: 'http://mine.example/live', station: undefined}) : p);
      const r = await send(hm.base, 'PUT', '/api/config', {rev: c0.rev, presets, keys: c0.keys.map(k => ({id: k.id, tap: k.tap, hold: k.hold}))});
      assert(r.ok, 'PUT /api/config ' + r.status + ' ' + await r.text());
      let c = await getJson(hm.base, '/api/config');
      assert(c.factory === false && c.starter === 'jazz' && presetName(c, 1) === 'My station', 'after the edit: ' + JSON.stringify([c.factory, c.starter, presetName(c, 1)]));
      const page = await open(hm, '#edit/reset');
      await page.waitForSelector('#resetKeys');
      const sets = await openSheet(page);
      assert(sets.find(s => s.on).now === 'Your set' && sets.find(s => s.on).button === 'Back to this set', 'a changed set reads "Your set": ' + JSON.stringify(sets.find(s => s.on)));
      await page.keyboard.press('Escape');
      await until(async () => !(await page.$('#setsDlg')), 5000, 'the sheet to close');
      await page.click('#resetKeys');
      await until(async () => (await page.textContent('#toast')).includes('Built-in keys restored'), 8000, 'the reset toast');
      c = await getJson(hm.base, '/api/config');
      assert(c.factory === true && c.starter === 'jazz' && presetName(c, 1) === 'Radio Swiss Jazz', 'after reset: ' + JSON.stringify([c.factory, c.starter, presetName(c, 1)]));
      assert(!fs.existsSync(path.join(hm.tmp, 'state', 'keys.yaml')), 'keys.yaml gone after reset');
      await page.close();
    });

    await check('Excelsior: seven keys per set, Tape takes the seventh; Classical & calm', async () => {
      const c0 = await getJson(ex.base, '/api/config');
      assert(c0.starters.every(s => s.keys.length === 7), 'seven keys per set');
      assert(presetName(c0, 7) === 'KEXP', 'Tape: ' + presetName(c0, 7));
      const page = await open(ex, '#edit');
      await page.waitForSelector('.keylist li');
      const sets = await openSheet(page);
      const classical = sets.find(s => s.id === 'classical');
      assert(classical.keys.length === 7 && classical.keys[6] === 'Tape Gregorian chant', 'the Tape key in the sheet: ' + classical.keys);
      await page.click('#setsDlg [data-choose="classical"]');
      await until(async () => !(await page.$('#setsDlg')), 5000, 'the sheet to close');
      const c = await until(async () => { const d = await getJson(ex.base, '/api/config'); return d.starter === 'classical' ? d : null; }, 5000, 'classical on the keys');
      assert(presetName(c, 1) === 'Radio Swiss Classic' && presetName(c, 7) === 'Gregorian chant' && c.factory, 'classical keys: ' + c.presets.map(p => p.name));
      const tape = c.keys.find(k => k.id === 'tape');
      assert(tape.tap.n === 7, 'Tape still plays 7');
      await page.goto(ex.base + '/#');
      await page.waitForSelector('#bank button[data-key="tape"]');
      await until(async () => (await page.textContent('#bank button[data-key="tape"] .key-l')).includes('Gregorian'), 5000, 'the Tape key cap');
      await page.close();
    });

    await check('box.yaml starter: family on the keys, the page says so', async () => {
      const c = await getJson(boxed.base, '/api/config');
      assert(c.starter === 'family' && c.starter_from === 'box' && c.factory === true, JSON.stringify([c.starter, c.starter_from, c.factory]));
      assert(presetName(c, 1) === 'Songs From Animated Movies' && presetName(c, 3) === 'Disney en castellano', 'family keys ' + c.presets.map(p => p.name));
      const page = await open(boxed, '#edit');
      await page.waitForSelector('#sec-reset');
      assert((await page.textContent('#sec-reset')).includes('(Kids & family)'), 'the reset row names the set');
      const sets = await openSheet(page);
      assert(sets.find(s => s.on).id === 'family', 'family marked');
      await page.keyboard.press('Escape');
      await page.close();
    });

    await check('box.yaml naming a folded set (pop): España on the keys, marked in the sheet', async () => {
      const c = await getJson(folded.base, '/api/config');
      assert(c.starter === 'espana' && c.starter_from === 'box' && c.factory === true, JSON.stringify([c.starter, c.starter_from, c.factory]));
      assert(presetName(c, 1) === 'Pop español' && presetName(c, 2) === 'Flamenco Radio', 'espana keys ' + c.presets.map(p => p.name));
      const page = await open(folded, '#edit');
      await page.waitForSelector('#sec-reset');
      assert((await page.textContent('#sec-reset')).includes('(España)'), 'the reset row names the set');
      const sets = await openSheet(page);
      assert(sets.find(s => s.on).id === 'espana', 'espana marked');
      await page.keyboard.press('Escape');
      await page.close();
    });

    await check('the sleep timer is a verb a hold can have', async () => {
      const page = await open(hm, '#edit/k3/hold');
      await page.waitForSelector('#chg-hold');
      await page.click('#chg-hold');
      await page.waitForSelector('.picker');
      const choice = await page.$('.picker [role=radio]:has-text("Sleep timer")');
      assert(choice, 'no Sleep timer in the hold picker');
      await choice.click();
      await page.waitForSelector('#min-hold');
      assert((await page.inputValue('#min-hold')) === '30', 'default minutes');
      await page.fill('#min-hold', '45');
      await page.click('#saveBtn');
      await until(async () => (await page.textContent('.save-msg')).includes('saved to the radio'), 8000, 'saved');
      const c = await getJson(hm.base, '/api/config');
      assert(JSON.stringify(c.keys.find(k => k.id === 'k3').hold) === JSON.stringify({do: 'sleep', minutes: 45}), 'k3.hold ' + JSON.stringify(c.keys.find(k => k.id === 'k3').hold));
      assert(c.factory === false, 'a sleep hold is the owner\'s');
      await page.close();
    });

    assert(!errors.length, 'page errors:\n' + errors.join('\n'));
    if (errors.length) failed++;
  } finally {
    await browser.close();
    for (const r of [hm, ex, boxed, folded]){ r.proc.kill(); fs.rmSync(r.tmp, {recursive: true, force: true}); }
  }
  console.log(`${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
