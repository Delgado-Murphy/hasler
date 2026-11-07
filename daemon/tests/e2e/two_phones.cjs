#!/usr/bin/env node
// Two phones editing keys at once, and leaving unsaved changes: the key page against
// the fake radio (haslerd --fake) in Chrome.
//
//   cd daemon && node tests/e2e/two_phones.cjs
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
  const browser = await launch();
  const errors = [];
  // Each phone its own browser context: nothing shared but the radio.
  const phone = async hash => {
    const ctx = await browser.newContext({viewport: {width: 390, height: 844}});
    const page = await ctx.newPage();
    page.on('pageerror', e => errors.push(hash + ': ' + e.message));
    page.on('console', m => { if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(hash + ': ' + m.text()); });
    await page.goto(radio.base + '/' + hash);
    return page;
  };
  const config = async () => (await fetch(radio.base + '/api/config')).json();
  const tapName = (c, id) => { const k = c.keys.find(x => x.id === id); const p = k.tap && c.presets.find(x => x.n === k.tap.n); return p ? p.name : null; };
  const rename = async (page, name) => {
    await page.click('#renameBtn');
    await page.fill('.choice-txt input', name);
    await page.keyboard.press('Enter');
  };
  const saved = (page, re) => until(async () => re.test(await page.textContent('.save-msg')), 8000, 'saved message ' + re);
  const dlgOpen = page => page.evaluate(() => document.getElementById('dlg').open);
  try {
    await check('two phones, different keys: the later save keeps the other phone’s change', async () => {
      const a = await phone('#edit/k1'), b = await phone('#edit/k2');
      await a.waitForSelector('#renameBtn'); await b.waitForSelector('#renameBtn');
      await rename(a, 'Phone A FM');                // A starts editing first…
      await rename(b, 'Phone B FM');
      await b.click('#saveBtn'); await saved(b, /saved to the radio/);
      await a.click('#saveBtn'); await saved(a, /saved to the radio/);   // …and saves last
      const c = await config();
      assert(tapName(c, 'k2') === 'Phone B FM', 'B’s key 2 undone: ' + tapName(c, 'k2'));
      assert(tapName(c, 'k1') === 'Phone A FM', 'A’s key 1 not saved: ' + tapName(c, 'k1'));
      assert(!/also changed/.test(await a.textContent('.save-msg')), 'a clash reported where there was none');
      // A's page now shows B's key 2 as well.
      await a.click('.skey >> nth=1');
      await until(async () => (await a.textContent('.choice-name')) === 'Phone B FM', 3000, 'B’s name on A');
      await a.context().close(); await b.context().close();
    });

    await check('two phones, the same key: the later save wins and says so', async () => {
      const a = await phone('#edit/k3'), b = await phone('#edit/k3');
      await a.waitForSelector('#renameBtn'); await b.waitForSelector('#renameBtn');
      await rename(a, 'Clash A'); await rename(b, 'Clash B');
      await b.click('#saveBtn'); await saved(b, /saved to the radio/);
      await a.click('#saveBtn'); await saved(a, /Key 3 was also changed on another phone — yours is saved/);
      assert(tapName(await config(), 'k3') === 'Clash A', 'radio has ' + tapName(await config(), 'k3'));
      await a.context().close(); await b.context().close();
    });

    await check('a phone with nothing unsaved picks up the other phone’s save by itself', async () => {
      const a = await phone('#edit/k4'), b = await phone('#edit/k4');
      await a.waitForSelector('#renameBtn'); await b.waitForSelector('#renameBtn');
      await rename(b, 'Seen Elsewhere'); await b.click('#saveBtn'); await saved(b, /saved to the radio/);
      await until(async () => (await a.textContent('.choice-name')) === 'Seen Elsewhere', 9000, 'the change on the idle phone');
      await a.context().close(); await b.context().close();
    });

    await check('Done with unsaved keys: one in-page question, Keep editing / Discard / Save and close', async () => {
      const page = await phone('#edit/k5');
      let native = 0; page.on('dialog', d => { native++; d.dismiss(); });
      await page.waitForSelector('#renameBtn');
      await rename(page, 'Done Keep');
      await page.click('.btn-done');
      await until(() => dlgOpen(page), 2000, 'the question');
      const txt = await page.textContent('#dlg');
      assert(/Key 5/.test(txt) && /Save and close/.test(txt) && /Discard/.test(txt) && /Keep editing/.test(txt), 'dialog says: ' + txt);
      await page.click('#leaveKeep');
      await sleep(200);
      assert(page.url().includes('#edit') && (await page.textContent('.choice-name')) === 'Done Keep', 'work lost on Keep editing');
      await page.click('.btn-done'); await until(() => dlgOpen(page), 2000, 'the question again');
      await page.click('#leaveSave');
      await until(async () => !page.url().includes('#edit'), 5000, 'closing after the save');
      assert(tapName(await config(), 'k5') === 'Done Keep', 'not saved: ' + tapName(await config(), 'k5'));
      await page.goto(radio.base + '/#edit/k5'); await page.waitForSelector('#renameBtn');
      await rename(page, 'Done Discard');
      await page.click('.btn-done'); await until(() => dlgOpen(page), 2000, 'the question');
      await page.click('#leaveDiscard');
      await until(async () => !page.url().includes('#edit'), 3000, 'closing on Discard');
      assert(tapName(await config(), 'k5') === 'Done Keep', 'discarded change saved');
      assert(native === 0, native + ' browser confirm() boxes');
      await page.context().close();
    });

    await check('Back with unsaved keys: called off, a second Back asks again instead of leaving', async () => {
      const page = await phone('#');
      let native = 0; page.on('dialog', d => { native++; d.dismiss(); });
      await page.waitForSelector('#goEdit');
      await page.click('#goEdit');
      await page.waitForSelector('.keylist button');
      await page.click('.keylist button >> nth=0');
      await page.waitForSelector('#renameBtn');
      await rename(page, 'Back Twice');
      for (let i = 0; i < 2; i++){
        await page.goBack({waitUntil: 'commit'}).catch(() => {});
        await until(() => dlgOpen(page), 3000, 'the question (Back ' + (i + 1) + ')');
        await page.click('#leaveKeep');
        await until(async () => page.url().includes('#edit'), 3000, 'back on Edit keys');
        assert(page.url().startsWith(radio.base), 'left the app: ' + page.url());
        assert((await page.textContent('.choice-name')) === 'Back Twice', 'work lost');
      }
      await page.goBack({waitUntil: 'commit'}).catch(() => {});
      await until(() => dlgOpen(page), 3000, 'the question (Back 3)');
      await page.click('#leaveDiscard');
      await until(async () => !page.url().includes('#edit'), 3000, 'the front page');
      assert(page.url().startsWith(radio.base), 'left the app: ' + page.url());
      assert(await page.evaluate(() => document.getElementById('app').dataset.view) === 'remote', 'not on the front');
      assert(tapName(await config(), 'k1') !== 'Back Twice', 'discarded change saved');
      assert(native === 0, native + ' browser confirm() boxes');
      await page.context().close();
    });

    await check('Settings page with unsaved changes: the same question on Back', async () => {
      const page = await phone('#settings');
      let native = 0; page.on('dialog', d => { native++; d.dismiss(); });
      await page.goto(radio.base + '/#settings/lamp');
      await page.waitForSelector('#lampName');
      await page.fill('#lampName', 'Somewhere New');
      await page.goBack({waitUntil: 'commit'}).catch(() => {});
      await until(() => dlgOpen(page), 3000, 'the question');
      await page.click('#leaveKeep');
      await until(async () => page.url().includes('#settings/lamp'), 3000, 'back on the lamp page');
      assert(await page.inputValue('#lampName') === 'Somewhere New', 'typing lost');
      await page.goBack({waitUntil: 'commit'}).catch(() => {});
      await until(() => dlgOpen(page), 3000, 'the question again');
      await page.click('#leaveDiscard');
      await until(async () => !page.url().includes('#settings/lamp'), 3000, 'leaving on Discard');
      assert(page.url().startsWith(radio.base), 'left the app: ' + page.url());
      assert(native === 0, native + ' browser confirm() boxes');
      await page.context().close();
    });

    await check('no script errors', async () => { assert(!errors.length, errors.join('\n     ')); });
  } finally {
    await browser.close();
    radio.proc.kill();
    fs.rmSync(radio.tmp, {recursive: true, force: true});
  }
  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
