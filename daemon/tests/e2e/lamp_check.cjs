#!/usr/bin/env node
// The guided Lamp check (Settings -> Maker tools -> Lamp colours): walks the
// whole flow with scripted picks - floors, warm white, amber, standby glow,
// blue, the verification sweep - with Back, Stop and resume (across a reload),
// a keyboard answer, then Save, and checks the calibration the radio saved
// into the overlay's io.lamp (and that other io.lamp keys survived), the raw
// duty previews the page sent, and Back to standard. Runs a fake radio
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

// What the scripted picks below must save (see the comments at each pick).
const EXPECT = {
  min_duty: {red: 0.015, green: 0.005625, blue: 0.03},
  balance: {red: 1, green: 1.25, blue: 0.6667},
  palette: {amber: {day: [1, 0.165, 0], night: [1, 0.135, 0]}, ember: [1, 0.1, 0], blue: [0.05, 0, 1]},
  level_gain: {amber: 1, ember: 1, blue: 1.6818},
  levels: {ember_day: 0.275, ember_night: 0.12},
};
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
  let page;
  try {
    await check('maker tools: the lamp check sits above the sliders, folded into Advanced', async () => {
      page = await open();
      await page.waitForSelector('#lchkStart', {timeout: 8000});
      const txt = await page.textContent('#lchk');
      assert(txt.includes('dim room') && txt.includes('listening position') && txt.includes('20 minutes'), 'intro: ' + txt);
      assert(txt.includes('No lamp on this radio'), 'the fake radio has no lamp: ' + txt);
      assert(await page.$('#lcAdv:not([open])'), 'Advanced is not folded');
      assert(await page.isHidden('#lcAdv .lc-list'), 'sliders show while folded');
      await page.click('#lcAdv summary');
      assert(await page.isVisible('#lcAdv .lc-list'), 'Advanced does not open');
      await page.click('#lcAdv summary');
      await shot(page, 'lampcheck-phone-intro');
    });

    await check('step 1, floors: blinking duty steps on the lamp, Back undoes a pick', async () => {
      await page.click('#lchkStart');
      assert((await step(page)).includes('Step 1 of 6'), await step(page));
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
      await pick(page, 'gone');                      // blue gone at 2 %: floor set high, 3 %
    });

    await check('step 2, warm white: A/B staircase', async () => {
      assert((await step(page)).includes('Step 2 of 6'), await step(page));
      assert((await q(page)).includes('warm white'), await q(page));
      await sleep(1700);                             // let the lamp swap to B once
      const lit = await page.$eval('#lchkCard1', el => el.classList.contains('on'));
      assert(lit, 'B is not shown as on the lamp after 1.5 s');
      await shot(page, 'lampcheck-phone-white');
      await pick(page, 'b'); await pick(page, 'a'); await pick(page, 'a'); await pick(page, 'b');   // green 0.4375 -> x1.25
      assert((await q(page)).includes('whiter'), await q(page));
      await pick(page, 'same');                      // blue 0.04 -> x0.6667
    });

    await check('step 3, amber: Stop, reload, carry on where it stopped', async () => {
      assert((await step(page)).includes('Step 3 of 6') && (await q(page)).includes('your amber'), await q(page));
      await pick(page, 'a');                         // 0.19 (yellower)
      await page.click('#lchkStop');
      const resume = await page.textContent('#lchkResume');
      assert(resume.includes('step 3 of 6'), 'resume: ' + resume);
      await page.reload();
      await page.waitForSelector('#lchkResume', {timeout: 8000});
      await page.click('#lchkResume');
      assert((await q(page)).includes('your amber'), 'after resume: ' + await q(page));
      await pick(page, 'b'); await pick(page, 'b'); await pick(page, 'a');   // 0.17, 0.16, 0.165
      assert((await q(page)).includes('At night'), await q(page));
      await pick(page, 'b'); await pick(page, 'same');                        // night 0.135
    });

    await check('step 4, standby glow: brighter/dimmer, one answer from the keyboard', async () => {
      assert((await step(page)).includes('Step 4 of 6') && (await q(page)).includes('by day'), await q(page));
      await page.focus('#lchkAns-up');
      await page.keyboard.press('1');                // Too dim: 0.22 x 1.25 = 0.275
      assert((await page.textContent('#lchk')).includes('28 % brightness'), 'level after the key');
      await pick(page, 'ok');
      assert((await q(page)).includes('at night'), await q(page));
      await pick(page, 'down'); await pick(page, 'ok');                       // 0.15 / 1.25 = 0.12
    });

    await check('step 5, blue: brightness match against amber, then the tint', async () => {
      assert((await step(page)).includes('Step 5 of 6') && (await q(page)).includes('brighter'), await q(page));
      await shot(page, 'lampcheck-phone-blue');
      await pick(page, 'amber', 2);                  // 80 %: blue loses even at full power -> capped
      await pick(page, 'amber'); await pick(page, 'blue'); await pick(page, 'amber'); await pick(page, 'same');  // 35 %: 2^0.75
      assert((await q(page)).includes('nicer blue'), await q(page));
      await pick(page, 'b'); await pick(page, 'b');  // a touch of red, then more: r 0.05
    });

    await check('step 6, every look by day and night: a jump raises a floor and re-runs it', async () => {
      assert((await step(page)).includes('Step 6 of 6'), await step(page));
      assert((await q(page)).includes('Playing') && (await q(page)).includes('by day'), await q(page));
      await shot(page, 'lampcheck-phone-sweep');
      await pick(page, 'jump');                      // amber's faintest lit channel: green 0.375 % x 1.5
      assert((await page.textContent('#lchkLive')).includes('Green'), 'no word about the raised floor');
      assert((await q(page)).includes('Playing'), 'the look is not re-run');
      const m = /1 of (\d+)/.exec(await page.textContent('#lchk')), n = m ? +m[1] : 0;
      assert(n >= 20, 'only ' + n + ' looks to check');
      for (let i = 0; i < n; i++) await pick(page, 'fine');
      await page.waitForSelector('#lchkSum');
    });

    await check('summary: what was chosen, swatches only a guide, nothing saved yet', async () => {
      const txt = await page.textContent('#lchk');
      assert(txt.includes('only a guide') && txt.includes('Save'), txt);
      assert(txt.includes('No steady faint blue'), 'no warning about blue: ' + txt);
      assert(txt.includes('couldn’t quite match'), 'no word about the blue cap');
      assert(txt.includes('Back to standard'), 'no Back to standard');
      const saved = await getJson(radio.base, '/api/lamp/calibration');
      assert(JSON.stringify(saved.saved) === '{}', 'saved before Save: ' + JSON.stringify(saved.saved));
      await shot(page, 'lampcheck-phone-summary');
    });

    await check('Save writes exactly the contract into io.lamp and keeps the rest', async () => {
      await page.click('#lchkSave');
      await until(async () => (await page.$('#lchkStart')) && !(await page.$('#lchkSum')), 5000, 'back to the intro');
      const doc = await getJson(radio.base, '/api/lamp/calibration');
      const s = doc.saved;
      assert(JSON.stringify(Object.keys(s).sort()) === JSON.stringify(['balance', 'calibrated', 'level_gain', 'levels', 'min_duty', 'palette']), 'keys ' + Object.keys(s));
      const {calibrated, ...cal} = s;
      near(cal, EXPECT);
      assert(calibrated.version === 1 && /^\d{4}-\d\d-\d\dT/.test(calibrated.at) && 'cpu_temp_c' in calibrated, JSON.stringify(calibrated));
      const text = fs.readFileSync(path.join(radio.tmp, 'etc', 'variant.d', 'hasler-hm.yaml'), 'utf8');
      assert(text.includes('frequency_hz: 600') && /white: \[1\.0, 0\.3, 0\.05\]/.test(text), 'other keys lost:\n' + text);
      assert((await page.textContent('#lchkState')).includes('Checked by eye'), await page.textContent('#lchkState'));
      await shot(page, 'lampcheck-phone-saved');
    });

    await check('the page only sent raw duties 0..1 while checking', async () => {
      const duties = previews.filter(b => b.duty);
      assert(duties.length > 10, 'only ' + duties.length + ' duty previews');
      assert(duties.every(b => b.duty.length === 3 && b.duty.every(v => typeof v === 'number' && v >= 0 && v <= 1)), 'a bad duty');
      assert(previews.some(b => b.rgb === null), 'the preview was never ended');
    });

    await check('Back to standard takes a second tap and keeps the rest of the overlay', async () => {
      await page.click('#lchkStd');
      assert((await page.textContent('#lchkStd')).includes('Tap again'), await page.textContent('#lchkStd'));
      await page.click('#lchkStd');
      await until(async () => JSON.stringify((await getJson(radio.base, '/api/lamp/calibration')).saved) === '{}', 5000, 'calibration gone');
      const text = fs.readFileSync(path.join(radio.tmp, 'etc', 'variant.d', 'hasler-hm.yaml'), 'utf8');
      assert(text.includes('frequency_hz: 600') && text.includes('white') && !text.includes('min_duty'), text);
      await until(async () => (await page.textContent('#lchkState')).includes('Standard'), 5000, 'standard state');
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
