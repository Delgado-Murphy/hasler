#!/usr/bin/env node
// Tap and Hold on the key page: the Tap | Hold switch, its address (#edit/k4/hold),
// both halves saved together, the key list and key bank showing the hold, a hold
// from the keyboard, and Reassign from Home Assistant landing on the search.
// Runs the page against the fake radio (haslerd --fake) in Chrome.
//
//   cd daemon && node tests/e2e/tap_hold.cjs
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
const shot = async (page, name) => { if (SHOTS){ fs.mkdirSync(SHOTS, {recursive: true}); await page.screenshot({path: path.join(SHOTS, name + '.png'), fullPage: false}); } };

// Home Assistant as the stubs tell it: connected, but k4's hold points at an automation it doesn't have.
const HA_MISSING = {configured: true, connected: true, url: 'http://ha.local:8123', location: 'Home', version: '2026.10', publish: false, error: null,
  missing: [{key: 'k4', event: 'hold', entity: 'automation.wake_up', do: 'ha_automation', reason: 'not found'}], previous_instance: null, moved: null, lost: null};
const HA_ENTS = {areas: [{id: 'lr', name: 'Living room'}, {id: 'bed', name: 'Bedroom'}],
  entities: [
    {entity_id: 'automation.morning', name: 'Morning lights', domain: 'automation', area: 'bed', state: 'on'},
    {entity_id: 'automation.evening', name: 'Evening calm', domain: 'automation', area: 'lr', state: 'on'},
    {entity_id: 'scene.reading', name: 'Reading', domain: 'scene', area: 'lr', state: 'scening'}]};

(async () => {
  const radio = await startRadio();
  const browser = await launch();
  const errors = [], dialogs = [];
  const phone = await browser.newContext({viewport: {width: 390, height: 844}});
  const fresh = async (hash, ctx) => {
    const page = await (ctx || phone).newPage();
    page.on('console', m => { if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(hash + ': ' + m.text()); });
    page.on('pageerror', e => errors.push(hash + ': ' + e.message));
    page.on('dialog', d => { dialogs.push(d.message()); d.dismiss(); });
    await page.goto(radio.base + '/' + hash);
    return page;
  };
  const activeId = page => page.evaluate(() => document.activeElement && document.activeElement.id);
  try {
    await check('#edit/k4/hold: the Hold tab, no station browser, the hold within the first screen', async () => {
      const page = await fresh('#edit/k4/hold');
      await page.waitForSelector('#slot-hold[aria-checked="true"]');
      assert(await page.getAttribute('#slot-tap', 'aria-checked') === 'false', 'Tap is checked too');
      assert(await page.$('.br') === null, 'a station browser is in the page');
      assert(await page.$('#testBtn') === null, '"Test on the radio" is shown on Hold');
      assert((await page.textContent('.sheet-head .mono')) === 'Hold does', 'head says ' + await page.textContent('.sheet-head .mono'));
      const box = await page.locator('#chg-hold').boundingBox();
      assert(box && box.y + box.height <= 844, 'Change is below the first screen: ' + JSON.stringify(box));
      assert((await page.textContent('.slot-seg #slot-tap small')).length > 0, 'Tap half has no summary');
      await until(async () => (await activeId(page)) === 'slot-hold', 3000, 'focus on the Hold tab');
      await shot(page, 'phone-hold-k4');
      await page.close();
    });

    await check('another key keeps Hold, and the address follows', async () => {
      const page = await fresh('#edit/k4/hold');
      await page.waitForSelector('#slot-hold[aria-checked="true"]');
      const n = await page.evaluate(() => history.length);
      await page.click('.strip-keys .skey >> nth=4');   // key 5
      await page.waitForSelector('#keyTitle');
      assert(await page.getAttribute('#slot-hold', 'aria-checked') === 'true', 'Hold not kept');
      assert(await page.evaluate(() => location.hash) === '#edit/k5/hold', 'hash ' + await page.evaluate(() => location.hash));
      assert((await page.textContent('#holdName')).includes('Volume down'), 'hold card: ' + await page.textContent('#holdName'));
      await page.click('#slot-tap');
      assert(await page.evaluate(() => location.hash) === '#edit/k5', 'hash after Tap ' + await page.evaluate(() => location.hash));
      assert(await page.evaluate(() => history.length) === n, 'the switch added history');
      await page.close();
    });

    await check('change the hold, switch to Tap, change it, Save: both in one PUT', async () => {
      const page = await fresh('#edit/k4/hold');
      await page.waitForSelector('#chg-hold');
      let body = null;
      page.on('request', r => { if (r.method() === 'PUT' && r.url().endsWith('/api/config')) body = r.postDataJSON(); });
      await page.click('#chg-hold');
      await page.getByRole('radio', {name: /^Volume up/}).click();
      await page.waitForSelector('#chg-hold');
      assert((await page.textContent('#holdName')).includes('Volume up'), 'hold card: ' + await page.textContent('#holdName'));
      assert(await page.$('#slot-hold .dot.dirty'), 'no unsaved dot on Hold');
      await page.click('#slot-tap');
      await page.waitForSelector('#renameBtn');
      assert(dialogs.length === 0, 'asked: ' + dialogs.join(' | '));
      await page.click('#renameBtn');
      await page.fill('.choice-txt input', 'Both Halves FM');
      await page.keyboard.press('Enter');
      await page.click('#saveBtn');
      await until(async () => body, 5000, 'the PUT');
      const k4 = body.keys.find(k => k.id === 'k4');
      assert(k4.hold && k4.hold.do === 'volume_up', 'hold sent: ' + JSON.stringify(k4.hold));
      const p = body.presets.find(x => x.n === k4.tap.n);
      assert(p && p.name === 'Both Halves FM', 'tap preset sent: ' + JSON.stringify(p));
      await until(async () => (await page.textContent('.save-msg')).includes('saved'), 6000, 'saved');
      const c = await (await fetch(radio.base + '/api/config')).json();
      assert(c.keys.find(k => k.id === 'k4').hold.do === 'volume_up', 'radio hold ' + JSON.stringify(c.keys.find(k => k.id === 'k4').hold));
      await page.close();
    });

    await check('key list: a Tap and a Hold line per key, the hold in full; the Hold line opens Hold', async () => {
      const page = await fresh('#edit');
      await page.waitForSelector('.keylist.slots');
      const ids = await page.$$eval('.keylist.slots li', lis => lis.length);
      const keys = (await (await fetch(radio.base + '/api/config')).json()).keys;
      assert(ids === keys.length, ids + ' rows for ' + keys.length + ' keys');
      for (const k of keys) assert(await page.$('#kl-' + k.id + '-tap') && await page.$('#kl-' + k.id + '-hold'), 'lines missing for ' + k.id);
      const cut = await page.$$eval('.kl-line .kl-v', els => els.filter(e => e.scrollWidth > e.clientWidth + 1 || getComputedStyle(e).textOverflow === 'ellipsis').map(e => e.textContent));
      assert(!cut.length, 'cut short: ' + cut.join(', '));
      assert((await page.textContent('#kl-pu-hold')).includes('Bluetooth pairing'), 'PU hold: ' + await page.textContent('#kl-pu-hold'));
      assert((await page.getAttribute('#kl-k5-hold', 'aria-label')).includes('hold: Volume down'), 'aria ' + await page.getAttribute('#kl-k5-hold', 'aria-label'));
      await shot(page, 'phone-keylist');
      await page.click('#kl-k5-hold');
      await page.waitForSelector('#slot-hold[aria-checked="true"]');
      assert(await page.evaluate(() => location.hash) === '#edit/k5/hold', 'hash ' + await page.evaluate(() => location.hash));
      await page.close();
    });

    await check('key bank: a mark and the hold in the label', async () => {
      const page = await fresh('#');
      await page.waitForSelector('#bank [data-key="k5"]');
      await until(async () => (await page.getAttribute('#bank [data-key="k5"]', 'aria-label') || '').includes('Hold: Volume down'), 4000, 'hold in the label');
      assert(await page.isVisible('#bank [data-key="k5"] .key-h'), 'no hold mark on key 5');
      assert(!(await page.isVisible('#bank [data-key="k1"] .key-h')), 'hold mark on key 1, which holds nothing');
      assert(!(await page.getAttribute('#bank [data-key="k1"]', 'aria-label')).includes('Hold'), 'key 1 label mentions a hold');
      await shot(page, 'phone-bank');
      await page.close();
    });

    await check('keyboard: Space held is a hold, a short Enter a tap', async () => {
      const page = await fresh('#');
      await page.waitForSelector('#bank [data-key="k1"]');
      const sent = [];
      await page.route('**/api/press', r => { sent.push(r.request().postDataJSON()); return r.fulfill({json: {ok: true}}); });
      await page.focus('#bank [data-key="k1"]');
      await page.keyboard.down(' ');
      await sleep(900);
      await page.keyboard.up(' ');
      await until(async () => sent.length, 2000, 'a press');
      await sleep(300);
      assert(sent.length === 1 && sent[0].event === 'hold' && sent[0].key === 'k1', 'sent ' + JSON.stringify(sent));
      await page.keyboard.down('Enter');
      await sleep(80);
      await page.keyboard.up('Enter');
      await until(async () => sent.length > 1, 2000, 'a tap');
      await sleep(300);
      assert(sent.length === 2 && sent[1].event === 'tap', 'sent ' + JSON.stringify(sent));
      await page.close();
    });

    await check('Reassign from Home Assistant: Hold tab, search open with focus, the missing name shown', async () => {
      const stubbed = async () => {
        const page = await phone.newPage();
        page.on('pageerror', e => errors.push('reassign: ' + e.message));
        await page.route('**/api/ha', r => r.request().method() === 'GET' ? r.fulfill({json: HA_MISSING}) : r.continue());
        await page.route('**/api/ha/entities*', r => r.fulfill({json: HA_ENTS}));
        await page.route('**/api/config', async r => {
          if (r.request().method() !== 'GET') return r.continue();
          const res = await r.fetch(), c = await res.json();
          c.keys.find(k => k.id === 'k4').hold = {do: 'ha_automation', entity: 'automation.wake_up'};
          return r.fulfill({json: c});
        });
        return page;
      };
      let page = await stubbed();
      await page.goto(radio.base + '/#settings/radio/ha');
      const btn = page.getByRole('button', {name: 'Reassign'});
      await btn.waitFor();
      await btn.click();                                                   // tap 1
      await until(async () => (await activeId(page)) === 'entq', 4000, 'focus on the entity search');
      assert(await page.evaluate(() => location.hash) === '#edit/k4/hold', 'hash ' + await page.evaluate(() => location.hash));
      assert(await page.getAttribute('#slot-hold', 'aria-checked') === 'true', 'not on Hold');
      assert((await page.textContent('#entMiss')).includes('automation.wake_up'), 'missing name not shown');
      assert(await page.$('#slot-hold .dot:not(.dirty)'), 'no problem dot on Hold');
      await shot(page, 'phone-reassign');
      await page.getByRole('radio', {name: /Morning lights/}).click();      // tap 2
      await page.waitForSelector('#holdName');
      assert((await page.textContent('#holdName')) === 'Morning lights', 'hold card: ' + await page.textContent('#holdName'));
      assert((await page.textContent('#saveBtn')).startsWith('Save'), 'Save not ready: ' + await page.textContent('#saveBtn'));   // tap 3 would save
      page.on('dialog', d => d.accept());     // leaving the unsaved change behind
      await page.close({runBeforeUnload: false});
      page = await stubbed();
      await page.goto(radio.base + '/#');
      await page.waitForSelector('#bank [data-key="k4"] .key-h.miss');
      assert((await page.getAttribute('#bank [data-key="k4"]', 'aria-label')).includes('missing in Home Assistant'), 'bank label: ' + await page.getAttribute('#bank [data-key="k4"]', 'aria-label'));
      await page.close();
    });

    await check('desktop: the Hold tab beside the browser', async () => {
      const desk = await browser.newContext({viewport: {width: 1280, height: 860}});
      const page = await fresh('#edit/k6/hold', desk);
      await page.waitForSelector('#slot-hold[aria-checked="true"]');
      await sleep(300);
      await shot(page, 'desktop-hold-k6');
      await page.click('#slot-tap');
      await sleep(300);
      await shot(page, 'desktop-tap-k6');
      await desk.close();
    });

    await check('no console errors', async () => { assert(!errors.length, errors.join('\n     ')); });
  } finally {
    await browser.close();
    radio.proc.kill();
    fs.rmSync(radio.tmp, {recursive: true, force: true});
  }
  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
