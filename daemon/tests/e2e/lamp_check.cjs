#!/usr/bin/env node
// The guided Lamp check (Settings -> Maker tools -> Lamp colours): walks the
// whole flow with scripted picks - floors, balance, amber, standby glow,
// blue, green, the verification sweep - with Back, Stop and resume (across a
// reload), a keyboard answer, then Save, and checks the calibration the radio
// saved into the overlay's io.lamp (and that other io.lamp keys survived), the
// raw duty previews the page sent, and Back to standard. Runs a fake radio
// (haslerd --fake) in Chrome.
//
//   cd daemon && node tests/e2e/lamp_check.cjs
//
// Needs Node and Playwright (`npm i playwright`, or PLAYWRIGHT=/path/to/node_modules/playwright),
// and a browser: Playwright's own Chromium, or installed Chrome (PW_CHANNEL=chrome).
// HASLER_PY picks the Python that runs the fake radio (default .venv/bin/python).
// SHOTS=/some/dir saves phone and desktop screenshots of the steps there.
'use strict';
const {spawn} = require('child_process');
const fs = require('fs'), os = require('os'), path = require('path'), net = require('net');
const {chromium} = require(process.env.PLAYWRIGHT || 'playwright');

const DAEMON = path.resolve(__dirname, '..', '..');
const PY = process.env.HASLER_PY || path.join(DAEMON, '.venv', 'bin', 'python');
const SHOTS = process.env.SHOTS || '';
const sleep = ms => new Promise(r => setTimeout(r, ms));
let failed = 0, passed = 0;

// The check starts from the radio's standard (GET /api/lamp/calibration .standard: the
// variant descriptor's own io.lamp calibration - the bench LED's check, shipped with every
// Hasler HM - over the engine's constants). What the scripted picks below must save is
// that standard with the picks applied (see the comments at each pick).
const r4 = x => Math.round(x * 1e4) / 1e4;
function expected(std){
  const day0 = std.palette.amber.day;
  const day = r4(day0[1] + 0.015);                                   // amber by day: a, (stop/resume), b, b, a from the standard's yellow
  const night = r4(day - 0.03);                                      // at night: b, then Can't tell
  const gain = Object.assign({}, std.level_gain, {blue: 1.6818});    // blue: capped at 80 %, 2^0.75 at 35 %
  if (std.palette.green) gain.green = r4(0.57 * 1.2);                // green: "the amber" once from the page's 0.57, then About the same
  return {
    min_duty: {red: 0.015, green: 0.005625, blue: 0.054},            // floors: 1 % x 1.5, 0.25 % x 1.5 (raised x 1.5 in the sweep), 4.5 % x 1.2
    balance: {red: 1, green: 1.25, blue: 0.6667},                    // the warm-bulb staircase: 0.4375 / 0.35, 0.04 / 0.06
    palette: Object.assign({}, std.palette, {amber: {day: [day0[0], day, day0[2]], night: [day0[0], night, day0[2]]}, blue: [0.05, 0, 1]}),
    level_gain: gain,
    levels: {ember_day: r4(Math.min(0.6, std.levels.ember_day * 1.25)), ember_night: r4(Math.max(0.03, std.levels.ember_night / 1.25))},
  };
}
const OVERLAY = 'schema: 1\nio:\n  lamp:\n    frequency_hz: 600\n    palette:\n      white: [1.0, 0.3, 0.05]\n';

function freePort(){
  return new Promise((res, rej) => { const s = net.createServer(); s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => res(p)); }); s.on('error', rej); });
}
async function startRadio(){
  const port = await freePort(), tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'hasler-e2e-'));
  fs.mkdirSync(path.join(tmp, 'etc', 'variant.d'), {recursive: true}); fs.mkdirSync(path.join(tmp, 'state'));
  fs.writeFileSync(path.join(tmp, 'etc', 'box.yaml'), `schema: 1\nvariant: hasler-hm\nweb:\n  host: 127.0.0.1\n  port: ${port}\n`);
  fs.writeFileSync(path.join(tmp, 'etc', 'variant.d', 'hasler-hm.yaml'), OVERLAY);
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
function near(a, b, path = ''){
  if (Array.isArray(b)) return b.forEach((v, i) => near(a[i], v, path + '[' + i + ']'));
  if (b && typeof b === 'object'){
    assert(a && typeof a === 'object', path + ' missing');
    assert(JSON.stringify(Object.keys(a).sort()) === JSON.stringify(Object.keys(b).sort()), path + ' keys ' + Object.keys(a));
    return Object.keys(b).forEach(k => near(a[k], b[k], path + '.' + k));
  }
  assert(typeof a === 'number' && Math.abs(a - b) < 1e-6, path + ': ' + a + ' != ' + b);
}

(async () => {
  const radio = await startRadio();
  const browser = await launch();
  const errors = [], previews = [];
  const phone = await browser.newContext({viewport: {width: 390, height: 844}, deviceScaleFactor: 2});
  await phone.addInitScript(() => { try { localStorage.setItem('hasler.maker', '1'); } catch (_) {} });
  const shot = async (page, name) => { if (SHOTS){ fs.mkdirSync(SHOTS, {recursive: true}); await page.screenshot({path: path.join(SHOTS, name + '.png'), fullPage: true}); } };
  const open = async (ctx = phone) => {
    const page = await ctx.newPage();
    page.on('console', m => { if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(m.text()); });
    page.on('pageerror', e => errors.push(e.message));
    page.on('request', r => { if (r.url().endsWith('/api/lamp/preview') && r.method() === 'POST'){ try { previews.push(JSON.parse(r.postData() || '{}')); } catch (_) {} } });
    await page.goto(radio.base + '/#settings/maker');
    return page;
  };
  const q = page => page.textContent('#lchkQ');
  const step = page => page.textContent('#lchkStep');
  const pick = async (page, k, times = 1) => { for (let i = 0; i < times; i++) await page.click('#lchkAns-' + k); };
  // The document the check starts from, and what it must end with.
  let page, doc, std, EXPECT, N;
  try {
    await check('the radio ships the bench LED’s check as its standard: nothing saved, a green in the palette', async () => {
      doc = await getJson(radio.base, '/api/lamp/calibration');
      std = doc.standard;
      assert(JSON.stringify(doc.saved) === '{}', 'saved at the start: ' + JSON.stringify(doc.saved));
      assert(std && std.min_duty && std.balance && std.palette && std.level_gain && std.levels, 'standard: ' + JSON.stringify(std));
      assert(std.min_duty.red > 0 && std.balance.green !== 1 && std.palette.amber.day[1] !== std.palette.amber.night[1], 'a calibrated standard: ' + JSON.stringify(std));
      assert(doc.colours.includes('green') && Array.isArray(std.palette.green) && typeof std.level_gain.green === 'number', 'no green in the standard: ' + JSON.stringify(std.palette));
      assert(doc.led, 'the descriptor names no LED part');
      EXPECT = expected(std); N = 7;
    });

    await check('maker tools: the lamp check sits above the sliders, folded into Advanced', async () => {
      page = await open();
      await page.waitForSelector('#lchkStart', {timeout: 8000});
      const txt = await page.textContent('#lchk');
      assert(txt.includes('dim room') && txt.includes('listening position') && txt.includes('20 minutes'), 'intro: ' + txt);
      assert(txt.includes('No lamp on this radio'), 'the fake radio has no lamp: ' + txt);
      const state = await page.textContent('#lchkState');
      assert(state.includes('Standard settings') && state.includes('LED ' + doc.led), 'state: ' + state);
      assert(!(await page.$('#lchkStd')), 'Back to standard shows with nothing saved');
      assert(await page.$('#lcAdv:not([open])'), 'Advanced is not folded');
      assert(await page.isHidden('#lcAdv .lc-list'), 'sliders show while folded');
      await page.click('#lcAdv summary');
      assert(await page.isVisible('#lcAdv .lc-list'), 'Advanced does not open');
      await page.click('#lcAdv summary');
      await shot(page, 'lampcheck-phone-intro');
    });

    await check('step 1, floors: blinking duty steps on the lamp, Back undoes a pick', async () => {
      await page.click('#lchkStart');
      assert((await step(page)).includes('Step 1 of ' + N), await step(page));
      assert((await q(page)).includes('Red alone') && (await q(page)).includes('2 %'), await q(page));
      await until(async () => previews.some(b => b.duty && b.duty[0] === 0.02 && !b.duty[1] && !b.duty[2]), 3000, 'red 2 % preview');
      await until(async () => previews.some(b => b.duty && b.duty.every(v => v === 0)), 4000, 'the blink off');
      await shot(page, 'lampcheck-phone-floors');
      await pick(page, 'steady');
      assert((await q(page)).includes('1 %'), 'after steady: ' + await q(page));
      await page.click('#lchkBack');
      assert((await q(page)).includes('2 %'), 'Back: ' + await q(page));
      await pick(page, 'steady', 2);                 // 2 %, 1 % steady ...
      await pick(page, 'shimmer');                   // ... 0.5 % shimmers: red floor 1.5 x 1 % = 1.5 %
      assert((await q(page)).includes('Green alone'), await q(page));
      await pick(page, 'steady', 4);                 // green steady to 0.25 %: 0.375 %
      assert((await q(page)).includes('Blue alone'), await q(page));
      await pick(page, 'gone');                      // blue gone at 2 % ...
      assert((await q(page)).includes('3 %'), 'the search goes up: ' + await q(page));
      await pick(page, 'shimmer');                   // ... shimmers at 3 % ...
      assert((await q(page)).includes('4.5 %'), await q(page));
      await pick(page, 'steady');                    // ... steady at 4.5 %: x 1.2 = 5.4 %
    });

    await check('step 2, balance: A/B staircase, named in plain words', async () => {
      assert((await step(page)).includes('Step 2 of ' + N), await step(page));
      assert((await q(page)).includes('warm bulb') && (await step(page)).includes('Balance'), await q(page));
      await sleep(1700);                             // let the lamp swap to B once
      const lit = await page.$eval('#lchkCard1', el => el.classList.contains('on'));
      assert(lit, 'B is not shown as on the lamp after 1.5 s');
      await shot(page, 'lampcheck-phone-white');
      await pick(page, 'b'); await pick(page, 'a'); await pick(page, 'a'); await pick(page, 'b');   // green 0.4375 -> x1.25
      assert((await q(page)).includes('plain white'), await q(page));
      await pick(page, 'same');                      // blue 0.04 -> x0.6667
    });

    await check('step 3, amber: starts from the standard’s amber; Stop, reload, carry on where it stopped', async () => {
      assert((await step(page)).includes('Step 3 of ' + N) && (await q(page)).includes('your amber'), await q(page));
      const y0 = std.palette.amber.day[1];
      // The first sample pair sits 0.04 either side of the standard's yellow.
      await until(async () => previews.some(b => b.draft && b.colour === 'amber' && b.level === 1 && Math.abs(b.draft.palette.amber.day[1] - r4(y0 + 0.04)) < 1e-9), 4000, 'A at the standard + 0.04');
      await pick(page, 'a');                         // yellower: + 0.04
      await page.click('#lchkStop');
      const resume = await page.textContent('#lchkResume');
      assert(resume.includes('step 3 of ' + N), 'resume: ' + resume);
      await page.reload();
      await page.waitForSelector('#lchkResume', {timeout: 8000});
      await page.click('#lchkResume');
      assert((await q(page)).includes('your amber'), 'after resume: ' + await q(page));
      await pick(page, 'b'); await pick(page, 'b'); await pick(page, 'a');   // - 0.02, - 0.01, + 0.005: the standard + 0.015
      assert((await q(page)).includes('At night'), await q(page));
      await pick(page, 'b'); await pick(page, 'same');                        // night: day - 0.03
    });

    await check('step 4, standby glow: starts from the standard’s levels; brighter/dimmer, one answer from the keyboard', async () => {
      assert((await step(page)).includes('Step 4 of ' + N) && (await q(page)).includes('by day'), await q(page));
      assert((await page.textContent('#lchk')).includes(Math.round(std.levels.ember_day * 100) + ' % brightness'), 'the standard’s level first: ' + await page.textContent('#lchk'));
      await page.focus('#lchkAns-up');
      await page.keyboard.press('1');                // Too dim: x 1.25
      const want = Math.round(Math.min(0.6, std.levels.ember_day * 1.25) * 100) + ' % brightness';
      assert((await page.textContent('#lchk')).includes(want), 'level after the key: ' + want);
      await pick(page, 'ok');
      assert((await q(page)).includes('at night'), await q(page));
      await pick(page, 'down'); await pick(page, 'ok');                       // / 1.25
    });

    await check('step 5, blue: brightness match against amber, then the tint', async () => {
      assert((await step(page)).includes('Step 5 of ' + N) && (await q(page)).includes('brighter'), await q(page));
      await shot(page, 'lampcheck-phone-blue');
      await pick(page, 'amber', 2);                  // 80 %: blue loses even at full power -> capped
      await pick(page, 'amber'); await pick(page, 'blue'); await pick(page, 'amber'); await pick(page, 'same');  // 35 %: 2^0.75
      assert((await q(page)).includes('nicer blue'), await q(page));
      await pick(page, 'b'); await pick(page, 'b');  // a touch of red, then more: r 0.05
    });

    await check('step 6, green: the magic-eye flash matched against the amber', async () => {
      assert((await step(page)).includes('Step 6 of ' + N) && (await step(page)).includes('Green'), await step(page));
      assert((await q(page)).includes('the green'), await q(page));
      await until(async () => previews.some(b => b.draft && b.colour === 'green' && b.level === 0.55 && Math.abs(b.draft.level_gain.green - 0.57) < 1e-9), 4000, 'a green sample at 0.55, gain 0.57');
      await shot(page, 'lampcheck-phone-green');
      await pick(page, 'amber');                     // the green is dimmer: 0.57 x 1.2 = 0.684
      await pick(page, 'same');
    });

    await check('step 7, every look by day and night: a jump raises a floor and re-runs it', async () => {
      assert((await step(page)).includes('Step 7 of ' + N) && (await step(page)).includes('Every look'), await step(page));
      assert((await q(page)).includes('Playing') && (await q(page)).includes('by day'), await q(page));
      await shot(page, 'lampcheck-phone-sweep');
      await pick(page, 'jump');                      // the radio blames amber's faintest lit channel: green 0.375 % x 1.5
      await until(async () => (await page.textContent('#lchkLive')).includes('Green'), 4000, 'a word about the raised floor');
      assert((await q(page)).includes('Playing'), 'the look is not re-run');
      const m = /1 of (\d+)/.exec(await page.textContent('#lchk')), n = m ? +m[1] : 0;
      assert(n >= 20, 'only ' + n + ' looks to check');
      for (let i = 0; i < n; i++) await pick(page, 'fine');
      await page.waitForSelector('#lchkSum');
    });

    await check('summary: what was chosen, swatches only a guide, nothing saved yet', async () => {
      const txt = await page.textContent('#lchk');
      assert(txt.includes('only a guide') && txt.includes('Save'), txt);
      assert(txt.includes('B 5.4 %') && !txt.includes('No steady faint'), 'blue found by searching up: ' + txt);
      assert(txt.includes('couldn’t quite match'), 'no word about the blue cap');
      assert(txt.includes('Green') && txt.includes('×' + EXPECT.level_gain.green), 'no green row: ' + txt);
      assert(txt.includes('Raised during the check: green ×1'), 'no word about the raised floor: ' + txt);
      assert(txt.includes('Back to standard'), 'no Back to standard');
      const saved = await getJson(radio.base, '/api/lamp/calibration');
      assert(JSON.stringify(saved.saved) === '{}', 'saved before Save: ' + JSON.stringify(saved.saved));
      await shot(page, 'lampcheck-phone-summary');
    });

    await check('Save writes exactly the contract into io.lamp and keeps the rest', async () => {
      await page.click('#lchkSave');
      await until(async () => (await page.$('#lchkStart')) && !(await page.$('#lchkSum')), 5000, 'back to the intro');
      const after = await getJson(radio.base, '/api/lamp/calibration');
      const s = after.saved;
      assert(JSON.stringify(Object.keys(s).sort()) === JSON.stringify(['balance', 'calibrated', 'level_gain', 'levels', 'min_duty', 'palette']), 'keys ' + Object.keys(s));
      const {calibrated, ...cal} = s;
      near(cal, EXPECT);
      assert(calibrated.version === 1 && /^\d{4}-\d\d-\d\dT/.test(calibrated.at) && 'cpu_temp_c' in calibrated, JSON.stringify(calibrated));
      assert(calibrated.led === doc.led && after.led_changed === false, 'the LED part stamped: ' + JSON.stringify(calibrated));
      near(after.standard, std);                     // the standard is the descriptor's, untouched by a save
      const text = fs.readFileSync(path.join(radio.tmp, 'etc', 'variant.d', 'hasler-hm.yaml'), 'utf8');
      assert(text.includes('frequency_hz: 600') && /white: \[1\.0, 0\.3, 0\.05\]/.test(text), 'other keys lost:\n' + text);
      assert((await page.textContent('#lchkState')).includes('Checked by eye'), await page.textContent('#lchkState'));
      await shot(page, 'lampcheck-phone-saved');
    });

    await check('the page only sent choices: raw duties for floors and balance, the draft for the engine to draw', async () => {
      const duties = previews.filter(b => b.duty);
      assert(duties.length > 10, 'only ' + duties.length + ' duty previews');
      assert(duties.every(b => b.duty.length === 3 && b.duty.every(v => typeof v === 'number' && v >= 0 && v <= 1)), 'a bad duty');
      const drafts = previews.filter(b => b.draft);
      const keys = ['balance', 'level_gain', 'levels', 'min_duty', 'palette'];
      assert(drafts.length > 20, 'only ' + drafts.length + ' draft previews');
      assert(drafts.every(b => JSON.stringify(Object.keys(b.draft).sort()) === JSON.stringify(keys)), 'a draft with odd keys');
      assert(drafts.every(b => (b.colour && typeof b.level === 'number' && b.look === undefined) || (b.look && typeof b.night === 'boolean')), 'a bad draft preview');
      // Every draft carries the standard's other colours (a draft is the whole calibration, so the engine draws it as it would be).
      assert(drafts.every(b => Array.isArray(b.draft.palette.green) && typeof b.draft.level_gain.green === 'number'), 'a draft without the standard’s green');
      const looks = new Set(drafts.filter(b => b.look).map(b => b.look));
      for (const id of ['standby', 'playing', 'busy', 'hotspot', 'asking', 'kick', 'refuse', 'swell', 'hue_blue', 'no_internet', 'fault', 'ok'])
        assert(looks.has(id), 'the sweep never showed ' + id + ': ' + [...looks]);
      assert(drafts.some(b => b.colour === 'amber' && b.level === 1) && drafts.some(b => b.colour === 'blue') && drafts.some(b => b.colour === 'green'), 'no colour samples');
      assert(previews.some(b => b.rgb === null), 'the preview was never ended');
    });

    await check('the radio draws a draft preview itself (no model on the page)', async () => {
      const draft = previews.filter(b => b.draft).pop().draft;
      const post = async body => (await fetch(radio.base + '/api/lamp/preview', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})).json();
      const amber = await post({draft, colour: 'amber', level: 1});
      assert(amber.ok && amber.duty.length === 3 && amber.duty[0] > amber.duty[1] && amber.duty[2] === 0, JSON.stringify(amber));
      const look = await post({draft, look: 'busy', night: true});
      assert(look.ok && ['red', 'green', 'blue'].includes(look.jump), JSON.stringify(look));
      await post({rgb: null});
    });

    await check('Back to standard takes a second tap, returns to the shipped standard and keeps the rest of the overlay', async () => {
      await page.click('#lchkStd');
      assert((await page.textContent('#lchkStd')).includes('Tap again'), await page.textContent('#lchkStd'));
      await page.click('#lchkStd');
      await until(async () => JSON.stringify((await getJson(radio.base, '/api/lamp/calibration')).saved) === '{}', 5000, 'calibration gone');
      const after = await getJson(radio.base, '/api/lamp/calibration');
      near(after.standard, std);                     // back to the descriptor's values, not the engine's bare constants
      assert(after.led === doc.led && after.led_changed === false, JSON.stringify(after));
      const text = fs.readFileSync(path.join(radio.tmp, 'etc', 'variant.d', 'hasler-hm.yaml'), 'utf8');
      assert(text.includes('frequency_hz: 600') && text.includes('white'), 'the rest of the overlay lost:\n' + text);
      for (const k of ['min_duty', 'balance', 'level_gain', 'levels', 'calibrated', 'amber', 'ember', 'blue', 'green'])
        assert(!text.includes(k), 'the overlay still has ' + k + ':\n' + text);
      await until(async () => (await page.textContent('#lchkState')).includes('Standard'), 5000, 'standard state');
      assert(!(await page.$('#lchkStd')), 'Back to standard still offered with nothing saved');
    });

    await check('a lamp with a magic-eye green gets a green brightness step (only then)', async () => {
      // Without a green in the palette (an older lamp), the step is left out.
      const plain = await browser.newContext({viewport: {width: 390, height: 844}});
      await plain.addInitScript(() => { try { localStorage.setItem('hasler.maker', '1'); } catch (_) {} });
      await plain.route('**/api/lamp/calibration', async route => {
        if (route.request().method() !== 'GET') return route.continue();
        const res = await route.fetch(), d = await res.json();
        d.colours = d.colours.filter(c => c !== 'green');
        delete d.standard.palette.green; delete d.standard.level_gain.green;
        await route.fulfill({response: res, json: d});
      });
      const p = await plain.newPage();
      p.on('pageerror', e => errors.push(e.message));
      await p.goto(radio.base + '/#settings/maker');
      await p.waitForSelector('#lchkStart', {timeout: 8000});
      await p.click('#lchkStart');
      assert((await p.textContent('#lchkStep')).includes('of 6'), 'a Green step without a green: ' + await p.textContent('#lchkStep'));
      await p.click('#lchkStop');
      await plain.close();
      // With one: the step, reached directly, starts from the page's 0.57 and halves its step each answer.
      const ctx = await browser.newContext({viewport: {width: 390, height: 844}});
      await ctx.addInitScript(() => { try { localStorage.setItem('hasler.maker', '1'); } catch (_) {} });
      const sent = [];
      const g = await ctx.newPage();
      g.on('pageerror', e => errors.push(e.message));
      g.on('request', r => { if (r.url().endsWith('/api/lamp/preview') && r.method() === 'POST'){ try { sent.push(JSON.parse(r.postData() || '{}')); } catch (_) {} } });
      await g.goto(radio.base + '/#settings/maker');
      await g.waitForSelector('#lchkStart', {timeout: 8000});
      await g.click('#lchkStart');
      assert((await g.textContent('#lchkStep')).includes('of 7'), 'no Green step: ' + await g.textContent('#lchkStep'));
      await g.click('#lchkStop');
      // Jump to the green step: a check saved just before it.
      await g.evaluate(() => {
        const st = JSON.parse(localStorage.getItem('hasler.lampcheck'));
        st.task = 12; st.ts = null; st.running = false;   // green-55: the 13th task when green is there
        localStorage.setItem('hasler.lampcheck', JSON.stringify(st));
      });
      await g.reload();
      await g.waitForSelector('#lchkResume', {timeout: 8000});
      await g.click('#lchkResume');
      assert((await g.textContent('#lchkStep')).includes('Green'), await g.textContent('#lchkStep'));
      assert((await g.textContent('#lchkQ')).includes('the green'), await g.textContent('#lchkQ'));
      await until(async () => sent.some(b => b.draft && b.colour === 'green' && b.level === 0.55 && Math.abs(b.draft.level_gain.green - 0.57) < 1e-9), 4000, 'a green sample at 0.55, gain 0.57');
      await g.click('#lchkAns-green');               // too bright: 0.57 / 1.2
      await g.click('#lchkAns-same');
      assert((await g.textContent('#lchkStep')).includes('Every look'), await g.textContent('#lchkStep'));
      const st = await g.evaluate(() => JSON.parse(localStorage.getItem('hasler.lampcheck')));
      assert(Math.abs(st.cal.level_gain.green - 0.475) < 1e-4, 'green gain ' + st.cal.level_gain.green);
      await g.click('#lchkStop');
      await ctx.close();
    });

    if (SHOTS) await check('desktop screenshots', async () => {
      const desk = await browser.newContext({viewport: {width: 1280, height: 900}});
      await desk.addInitScript(() => { try { localStorage.setItem('hasler.maker', '1'); } catch (_) {} });
      const d = await open(desk);
      await d.waitForSelector('#lchkStart');
      await d.click('#lchkStart');
      await d.click('#lchkAns-steady');
      await shot(d, 'lampcheck-desktop-floors');
      await d.click('#lchkStop');
      await desk.close();
    });

    await check('no console errors', async () => { assert(!errors.length, errors.join('\n')); });
  } finally {
    await browser.close();
    radio.proc.kill();
    fs.rmSync(radio.tmp, {recursive: true, force: true});
  }
  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
