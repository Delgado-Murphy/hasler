#!/usr/bin/env node
// Small things on the key page: the toast above Edit keys' Save bar, a tap on the
// playing station, "Still trying…" while the allow-address offer waits, a refusal
// for the page's address in plain words, and the keyboard focus through a Save.
// Runs the page against the fake radio (haslerd --fake) in Chrome.
//
//   cd daemon && node tests/e2e/keypage_fixes.cjs
//
// Same needs as net_resilience.cjs (PLAYWRIGHT, PW_CHANNEL, HASLER_PY). The page is
// also opened at http://myradio.test:<port> (Chrome maps the name to 127.0.0.1), an
// address the radio doesn't take changes through yet.
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
    try { const r = await fetch(base + '/api/status'); if (r.ok) return {proc, base, port, tmp}; } catch (_) {}
    if (proc.exitCode != null) break;
    await sleep(100);
  }
  proc.kill(); throw new Error('fake radio did not start:\n' + err);
}
async function launch(){
  const args = ['--host-resolver-rules=MAP myradio.test 127.0.0.1'];
  const want = process.env.PW_CHANNEL;
  if (!want){ try { return await chromium.launch({args}); } catch (_) {} }
  return chromium.launch({channel: want || 'chrome', args});
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
const post = (base, url, body) => fetch(base + url, {method: 'POST', headers: {'Content-Type': 'application/json', Origin: base}, body: JSON.stringify(body)});
const activeId = page => page.evaluate(() => document.activeElement ? (document.activeElement.id || document.activeElement.tagName) : 'none');

(async () => {
  const radio = await startRadio();
  const browser = await launch();
  const errors = [];
  const ctx = await browser.newContext({viewport: {width: 390, height: 844}});
  const fresh = async (hash, origin) => {
    const page = await ctx.newPage();
    page.on('console', m => { if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(hash + ': ' + m.text()); });
    page.on('pageerror', e => errors.push(hash + ': ' + e.message));
    await page.goto((origin || radio.base) + '/' + hash);
    return page;
  };
  const rename = async (page, name) => {
    await page.click('#renameBtn');
    await page.fill('.choice-txt input', name);
    await page.keyboard.press('Enter');
  };
  try {
    await check('the toast sits above Edit keys’ Save bar', async () => {
      const page = await fresh('#edit/k1');
      await page.waitForSelector('#renameBtn');
      await rename(page, 'Toast FM');
      await page.click('#saveBtn');
      await until(async () => (await page.textContent('#toast')).includes('saved'), 8000, 'the saved toast');
      await sleep(400);      // the toast slides up
      assert(await page.evaluate(() => document.documentElement.classList.contains('has-actions')), 'html has no has-actions class');
      const [t, a] = await page.evaluate(() => ['toast', null].map((id, i) => {
        const r = (i ? document.querySelector('.actions') : document.getElementById(id)).getBoundingClientRect(); return {top: r.top, bottom: r.bottom};
      }));
      assert(t.bottom <= a.top + 14, 'toast ' + JSON.stringify(t) + ' over the bar ' + JSON.stringify(a));
      await page.goto(radio.base + '/#');
      await until(async () => !(await page.evaluate(() => document.documentElement.classList.contains('has-actions'))), 3000, 'has-actions gone on the front page');
      await page.close();
    });

    await check('a tap on the playing station: "Standby — tap again to play"', async () => {
      const page = await fresh('#');
      await page.waitForSelector('#bank button');
      const c = await (await fetch(radio.base + '/api/config')).json();
      const k = c.keys.find(x => x.id !== 'pu' && x.tap && x.tap.do === 'preset' && (c.presets.find(p => p.n === x.tap.n) || {}).source === 'radio');
      assert(k, 'no radio key');
      await post(radio.base, '/api/standby', {on: false});
      const r = await post(radio.base, '/api/press', {key: k.id, event: 'tap'});
      assert(r.ok, 'press ' + r.status);
      const btn = `#bank button[data-key="${k.id}"]`;
      await until(async () => (await page.getAttribute(btn, 'aria-pressed')) === 'true', 6000, 'the key shown playing');
      await sleep(2200);      // past the radio's "again" guard
      await page.click(btn);
      await until(async () => (await page.textContent('#toast')).includes('Standby — tap again to play'), 4000, 'the standby toast');
      await until(async () => (await (await fetch(radio.base + '/api/status')).json()).standby, 3000, 'the radio in standby');
      await page.close();
    });

    await check('allow-address offer: no "Still trying…", plain words after Not now, the link allows and saves', async () => {
      const origin = `http://myradio.test:${radio.port}`;
      const page = await fresh('#edit/k1', origin);
      await page.waitForSelector('#renameBtn');
      // The radio needs a real key press to allow an address: the confirm is answered here,
      // and once "allowed" the page's changes go on to the radio through its own address.
      let allowed = false, hostsPosts = 0;
      await page.route('**/api/confirm/request', r => r.fulfill({json: {ok: true, token: 'tok', state: 'confirmed'}}));
      await page.route('**/api/hosts', r => {
        if (r.request().method() !== 'POST') return r.continue();
        hostsPosts++; allowed = true; return r.fulfill({json: {ok: true}});
      });
      await page.route('**/api/{config,play}', async r => {
        if (!allowed || r.request().method() === 'GET') return r.continue();
        const u = new URL(r.request().url());
        const headers = Object.assign({}, r.request().headers(), {origin: radio.base}); delete headers.host; delete headers.referer;
        return r.fulfill({response: await r.fetch({url: radio.base + u.pathname, headers})});
      });
      await rename(page, 'Allowed FM');
      await page.click('#saveBtn');
      await page.waitForSelector('#hostAllow', {state: 'visible', timeout: 6000});
      await sleep(5000);       // past save()'s 4 s "Still trying…"
      const during = await page.textContent('.save-msg');
      assert(!during.includes('Still trying'), 'said "' + during + '" while the offer waited');
      await page.click('text=Not now');
      await until(async () => (await page.textContent('.save-msg')).includes('Not saved'), 4000, 'the outcome');
      const panel = await page.textContent('#panel');
      assert(!/not this radio/i.test(panel), 'raw server text shown: ' + panel.match(/not this radio[^.]*/i));
      const box = await page.textContent('#genAlert');
      assert(box.includes('This page is open at myradio.test') && box.includes('can’t make changes yet') && box.includes('Allow this address'), 'box: ' + box);
      const f = await activeId(page);
      assert(f === 'genAlert' || f === 'saveMsg', 'focus on ' + f + ' after Not now');
      // Test on the radio, refused the same way: the same words, no raw text.
      await page.click('#testBtn');
      await until(async () => (await page.textContent('#saveMsg')).includes('can’t make changes yet'), 4000, 'Test refused in words');
      assert(await page.isVisible('#saveMsg button.link'), 'no Allow link by the Test message');
      assert(!(await page.isVisible('#hostAllow')), 'the offer came back by itself after Not now');
      // Save again: still no offer by itself, still the words and the link.
      await page.click('#saveBtn');
      await until(async () => (await page.textContent('.save-msg')).includes('Not saved'), 4000, 'the second outcome');
      assert(!(await page.isVisible('#hostAllow')), 'offer shown again after Not now');
      await page.click('#genAlert button.link');
      await page.click('#hostAllow');
      await until(async () => (await page.textContent('.save-msg')).includes('saved to the radio'), 8000, 'saved after allowing');
      assert(hostsPosts === 1, hostsPosts + ' POST /api/hosts');
      const c = await (await fetch(radio.base + '/api/config')).json();
      const k = c.keys.find(x => x.id === 'k1');
      assert(c.presets.find(p => p.n === k.tap.n).name === 'Allowed FM', 'the radio didn’t get it');
      const f2 = await activeId(page);
      assert(f2 === 'saveMsg', 'focus on ' + f2 + ' after allowing');
      await page.close();
    });

    await check('keyboard Save: focus stays on Save, one live message element, failures toasted', async () => {
      const page = await fresh('#edit/k1');
      await page.waitForSelector('#renameBtn');
      await rename(page, 'Focus FM');
      let hold = true, fail = false;
      await page.route('**/api/config', async r => {
        if (r.request().method() !== 'PUT') return r.continue();
        while (hold) await sleep(50);
        if (fail) return r.fulfill({status: 500, json: {ok: false, error: 'the disk is full'}});
        return r.continue();
      });
      await page.evaluate(() => { document.querySelector('.save-msg').__mark = 1; });
      await page.focus('#saveBtn');
      await page.keyboard.press('Enter');
      await until(async () => (await page.textContent('#saveBtn')) === 'Saving…', 2000, 'Saving…');
      assert(await activeId(page) === 'saveBtn', 'focus while saving on ' + await activeId(page));
      assert(await page.getAttribute('#saveBtn', 'aria-disabled') === 'true', 'Save not aria-disabled while saving');
      assert(await page.evaluate(() => !document.getElementById('saveBtn').disabled), 'Save disabled (it drops the focus)');
      await page.keyboard.press('Enter');      // a second press while saving does nothing
      hold = false;
      await until(async () => (await page.textContent('.save-msg')).includes('saved to the radio'), 8000, 'saved');
      assert(await page.textContent('#saveBtn') === 'Saved ✓', 'button: ' + await page.textContent('#saveBtn'));
      assert(await activeId(page) === 'saveBtn', 'focus after saving on ' + await activeId(page));
      assert(await page.evaluate(() => document.querySelector('.save-msg').__mark === 1), 'the live message was made afresh');
      assert(await page.getAttribute('.save-msg', 'role') === 'status', 'no role=status');
      // A failure says so in the toast too.
      await rename(page, 'Focus FM 2');
      hold = false; fail = true;
      await page.focus('#saveBtn');
      await page.keyboard.press('Enter');
      await until(async () => (await page.textContent('.save-msg')).includes('Not saved'), 6000, 'the failure');
      assert((await page.textContent('#toast')).includes('Not saved'), 'toast: ' + await page.textContent('#toast'));
      assert(await activeId(page) !== 'BODY', 'focus dropped to BODY after a failed save');
      await page.close();
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
