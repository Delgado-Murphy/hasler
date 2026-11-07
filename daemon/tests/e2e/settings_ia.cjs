#!/usr/bin/env node
// Settings, as laid out in 1.5.0, against the fake radio (haslerd --fake) in Chrome:
// the home rows and their summaries, Needs attention and the gear's dot, the page
// note, Music accounts, Rooms (the name, Identify), the ask() sheet where a browser
// confirm() used to be, the Allow action on a change refused for the page's address,
// and the old deep links that still land.
//
//   cd daemon && node tests/e2e/settings_ia.cjs
//
// Same needs as keypage_fixes.cjs: Node, Playwright (PLAYWRIGHT=/path/to/node_modules/playwright),
// a browser (PW_CHANNEL=chrome for installed Chrome), HASLER_PY for the fake radio's Python.
// SHOTS=/some/dir saves screenshots there.
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
  // HASLER_FAKE_LAMP=1: a lamp engine drawing to nowhere, so Identify is on the Rooms page.
  const proc = spawn(PY, ['-m', 'haslerd.main', '--fake', '--etc', path.join(tmp, 'etc'), '--state', path.join(tmp, 'state'), '--log-level', 'WARNING'],
    {cwd: DAEMON, stdio: ['ignore', 'ignore', 'pipe'], env: Object.assign({}, process.env, {HASLER_FAKE_LAMP: '1'})});
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
const shot = async (page, name) => { if (SHOTS){ fs.mkdirSync(SHOTS, {recursive: true}); await page.screenshot({path: path.join(SHOTS, name + '.png'), fullPage: true}); } };
const send = (base, method, url, body) => fetch(base + url, {method, headers: {'Content-Type': 'application/json', Origin: base}, body: JSON.stringify(body)});
const get = async (base, url) => (await fetch(base + url)).json();
const SW = {ok: true, installed: true, version: '1.4.0', channel: 'stable', update_mode: 'ask', available: null, approved: null, last_result: null, releases: [], history: []};

(async () => {
  const radio = await startRadio();
  const browser = await launch();
  const errors = [], natives = [];
  const ctx = await browser.newContext({viewport: {width: 390, height: 844}, deviceScaleFactor: 2});
  const fresh = async (hash, origin) => {
    const page = await ctx.newPage();
    page.on('console', m => { if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(hash + ': ' + m.text()); });
    page.on('pageerror', e => errors.push(hash + ': ' + e.message));
    page.on('dialog', d => { natives.push(d.message()); d.dismiss(); });
    await page.goto((origin || radio.base) + '/' + hash);
    return page;
  };
  const rows = page => page.$$eval('#setRows .set-row', els => els.map(e => ({id: e.id.replace('setrow-', ''), title: e.querySelector('.li-title').textContent, sub: (e.querySelector('.li-sub') || {}).textContent || ''})));
  const dlgText = page => page.evaluate(() => document.getElementById('dlg').textContent);
  try {
    await check('home: seven rows in the new order, each with a live summary, no Maker tools', async () => {
      const page = await fresh('#settings');
      await until(async () => (await rows(page)).every(r => !/Checking/.test(r.sub)), 6000, 'the summaries');
      await sleep(600);
      const rs = await rows(page);
      assert(rs.map(r => r.id).join() === 'sound,keys,phones,accounts,rooms,lamp,radio', 'rows: ' + rs.map(r => r.id).join());
      assert(rs.map(r => r.title).join('|') === 'Sound|Keys|Phones|Music accounts|Rooms|Pilot lamp|This radio', 'titles: ' + rs.map(r => r.title).join('|'));
      const by = Object.fromEntries(rs.map(r => [r.id, r.sub]));
      assert(/^Ceiling 70 · not set yet · quietest step 28/.test(by.sound), 'Sound: ' + by.sound);   // nobody set the ceiling on a fresh radio
      assert(/^1 .+ · 2 /.test(by.keys), 'Keys: ' + by.keys);
      assert(by.keys.length <= 70, 'Keys summary too long: ' + by.keys);
      assert(/Spotify isn’t on this radio|YouTube Music isn’t on this radio/.test(by.accounts), 'Accounts: ' + by.accounts);
      assert(/^Not named yet · this room only$/.test(by.rooms), 'Rooms: ' + by.rooms);
      assert(/^Wired · version /.test(by.radio) && !/No Wi-Fi/.test(by.radio), 'This radio: ' + by.radio);
      assert(!(await page.$('#sec-attention')), 'Needs attention shown with nothing to show');
      assert(!(await page.$('#pageNote')), 'a page note on the home list');
      await shot(page, 'ia-home');
      await page.close();
    });

    await check('each page says under its title how changes are kept; Sound ends with the knobs', async () => {
      const page = await fresh('#settings/sound');
      await until(() => page.isVisible('#pageNote'), 4000, 'the page note');
      assert((await page.textContent('#pageNote')) === 'Changes apply at once, with Undo.', 'note: ' + await page.textContent('#pageNote'));
      await until(() => page.isVisible('#sec-knobs'), 6000, 'the knobs group');
      const heads = await page.$$eval('#panel h3.grp-lbl', hs => hs.map(x => x.textContent.trim()));
      const secs = await page.$$eval('#panel .grp-sec', ss => ss.map(x => x.id));
      assert(heads[0] === 'Volume' && secs[secs.length - 1] === 'sec-knobs', 'headings: ' + heads.join('|') + ' / ' + secs.join('|'));
      assert((await page.textContent('#more-knobs summary')) === 'A knob feels off?' && !(await page.getAttribute('#more-knobs', 'open')), 'the knobs fold');
      assert((await page.$$('#sec-knobs button:has-text("Calibrate")')).length >= 1, 'no Calibrate');
      assert(await page.getAttribute('#limit', 'aria-valuetext') === '70 of 100', 'valuetext ' + await page.getAttribute('#limit', 'aria-valuetext'));
      assert(!(await page.$$eval('#panel .hint:not(.page-note)', hs => hs.some(x => /apply at once/.test(x.textContent)))), 'an ad-hoc "apply at once" hint left on the page');
      await page.goto(radio.base + '/#settings/phones');
      await until(() => page.isVisible('#pageNote'), 4000, 'the Phones note');
      assert(/waits for Save/.test(await page.textContent('#pageNote')), 'Phones note: ' + await page.textContent('#pageNote'));
      await page.close();
    });

    await check('Keys: points to Edit keys, the source key list, Starting over', async () => {
      const page = await fresh('#settings/keys');
      await until(() => page.isVisible('#sec-pu'), 6000, 'the source key list');
      const heads = await page.$$eval('#panel h3.grp-lbl', hs => hs.map(x => x.textContent.trim()));
      assert(heads.join('|') === 'What the keys play|What the source key steps through|Starting over', 'headings: ' + heads.join('|'));
      assert((await page.getAttribute('#setrow-reset', 'href')) === '#edit/reset' && (await page.getAttribute('#setrow-editkeys', 'href')) === '#edit', 'links');
      assert((await page.textContent('#pageTitle')) === 'Keys', 'title ' + await page.textContent('#pageTitle'));
      assert(!(await page.$('#sec-knobs')), 'knob calibration still on Keys');
      await page.close();
    });

    await check('Phones: one "What phones call the radio" row with a link to Rooms', async () => {
      const page = await fresh('#settings/phones');
      await until(() => page.isVisible('#more-phones summary'), 6000, 'More options');
      assert(!(await page.isVisible('#castNameBox')), 'the name row shown before More options');
      await page.click('#more-phones summary');
      await until(() => page.isVisible('#castNameBox .li-title'), 6000, 'the name row');
      assert((await page.textContent('#castNameBox .li-title')) === 'What phones call the radio', 'title');
      assert((await page.textContent('#castNameNow')) === 'Hasler', 'name ' + await page.textContent('#castNameNow'));
      assert((await page.getAttribute('#castNameRooms', 'href')) === '#settings/rooms/name', 'the Rooms link');
      assert(!(await page.$('input[id^="cn-"]')), 'per-service name fields shown with every target unavailable');
      assert(!(await page.$('#spAcct')), 'the Spotify account card still on Phones');
      await shot(page, 'ia-phones');
      await page.close();
    });

    await check('Music accounts: Spotify and YouTube Music as rows; Sign out asks in the page’s own sheet', async () => {
      const page = await ctx.newPage();
      page.on('pageerror', e => errors.push('accounts: ' + e.message));
      page.on('dialog', d => { natives.push(d.message()); d.dismiss(); });
      let deleted = 0;
      await page.route('**/api/ytm/account', r => {
        if (r.request().method() === 'DELETE'){ deleted++; return r.fulfill({json: {ok: true, account: {available: true, signed_in: false, name: ''}}}); }
        return r.fulfill({json: {available: true, signed_in: true, name: 'Bill'}});
      });
      await page.goto(radio.base + '/#settings/accounts');
      await until(() => page.isVisible('#acctOut'), 6000, 'Sign out');
      assert(/Spotify isn’t set up on this radio/.test(await page.textContent('#spAcctBox')), 'Spotify: ' + await page.textContent('#spAcctBox'));
      assert(!(await page.$('#panel .card-box')), 'a serif card on Music accounts');
      assert((await page.textContent('#acctH')) === 'Signed in as Bill', 'YTM: ' + await page.textContent('#acctH'));
      await page.click('#acctOut');
      await until(() => page.isVisible('#askOk'), 3000, 'the sheet');
      assert(/Sign out of YouTube Music\?/.test(await dlgText(page)) && (await page.textContent('#askOk')) === 'Sign out', 'sheet: ' + await dlgText(page));
      await page.click('#askCancel');
      await until(async () => !(await page.evaluate(() => document.getElementById('dlg').open)), 2000, 'the sheet to close');
      assert(deleted === 0, 'signed out on Cancel');
      await page.click('#acctOut');
      await until(() => page.isVisible('#askOk'), 3000, 'the sheet again');
      await page.click('#askOk');
      await until(() => page.isVisible('#acctIn'), 4000, 'Sign in after signing out');
      assert(deleted === 1, deleted + ' DELETEs');
      await shot(page, 'ia-accounts');
      await page.close();
    });

    await check('Rooms: the room name (saved by the bar), Identify, the other Haslers’ empty state', async () => {
      const page = await fresh('#settings/rooms');
      await until(() => page.isVisible('#roomEdit'), 6000, 'Name it');
      await until(() => page.isVisible('#otherRadiosEmpty'), 6000, 'the other Haslers’ empty state');
      const heads = await page.$$eval('#panel h3.grp-lbl', hs => hs.map(x => x.textContent.trim()));
      assert(heads.join('|') === 'This room|Several rooms|The other Haslers', 'headings: ' + heads.join('|'));
      assert(await page.isVisible('#identSelf'), 'Identify missing');
      assert(await page.isHidden('#saveBar'), 'the bar before any change');
      await page.click('#roomEdit');
      await page.fill('#roomInp', 'Kitchen');
      await until(() => page.isVisible('#saveBar'), 2000, 'the Save bar');
      assert((await page.textContent('#sbWhat')) === 'Room name', 'what: ' + await page.textContent('#sbWhat'));
      await page.click('#sbSave');
      await until(async () => ((await get(radio.base, '/api/settings')).room || {}).name === 'Kitchen', 4000, 'the name on the radio');
      await until(async () => (await page.textContent('#roomName')) === 'Kitchen', 3000, 'the name on the page');
      assert(await page.isChecked('#roomCast') === false && !(await page.$eval('#roomCast', e => e.disabled)), 'the names switch');
      await page.goto(radio.base + '/#settings');
      await until(async () => /^Kitchen · this room only$/.test((await rows(page)).find(r => r.id === 'rooms').sub), 4000, 'the Rooms summary');
      await shot(page, 'ia-rooms');
      await page.close();
    });

    await check('old addresses land on the new pages: #settings/radio/name opens the name field on Rooms', async () => {
      const page = await fresh('#settings/radio/name');
      await until(() => page.isVisible('#roomInp'), 6000, 'the name field open');
      assert(await page.evaluate(() => location.hash) === '#settings/rooms/name', 'hash ' + await page.evaluate(() => location.hash));
      await page.goto(radio.base + '/#settings/radio/ytm');
      await until(async () => (await page.evaluate(() => location.hash)) === '#settings/accounts/ytm', 3000, 'the YouTube Music alias');
      await until(() => page.isVisible('#ytmAcctBox'), 4000, 'Music accounts');
      await page.goto(radio.base + '/#settings/keys/knobs');
      await until(async () => (await page.evaluate(() => location.hash)) === '#settings/sound/knobs', 3000, 'the knobs alias');
      await page.goto(radio.base + '/#settings/sources');
      await until(async () => (await page.evaluate(() => location.hash)) === '#settings/phones', 3000, 'the Phones alias');
      await page.close();
    });

    await check('Needs attention: an update ready and a rolled-back update; the gear carries a dot; Updates marks the rollback seen', async () => {
      const page = await ctx.newPage();
      page.on('pageerror', e => errors.push('attention: ' + e.message));
      const sw = Object.assign({}, SW, {available: {version: '1.4.2', seq: 42, notes: '', kind: 'minor', requires_migration: false, staged: true},
        last_result: {ok: false, version: '1.4.1', back_to: '1.4.0', reason: 'it never came back', at: '2026-11-01T02:00:00+00:00'},
        releases: [{version: '1.4.1', status: 'bad', installed_at: '2026-11-01T02:00:00+00:00'}, {version: '1.3.0', status: 'previous'}]});
      await page.route('**/api/software', r => r.request().method() === 'GET' ? r.fulfill({json: sw}) : r.continue());
      await page.goto(radio.base + '/#');
      await until(() => page.isVisible('#gearDot'), 8000, 'the dot on the gear');
      await page.goto(radio.base + '/#settings');
      await until(() => page.isVisible('#setrow-attn-update'), 6000, 'the update row');
      assert(await page.isVisible('#setrow-attn-rollback'), 'no rollback row');
      assert(/1\.4\.1 didn’t work here/.test(await page.textContent('#setrow-attn-rollback')) && /went back to 1\.4\.0/.test(await page.textContent('#setrow-attn-rollback')), 'rollback row: ' + await page.textContent('#setrow-attn-rollback'));
      assert(/1\.4\.2 ready/.test((await rows(page)).find(r => r.id === 'radio').sub), 'This radio summary: ' + (await rows(page)).find(r => r.id === 'radio').sub);
      await shot(page, 'ia-attention');
      await page.click('#setrow-attn-rollback');
      await until(async () => ((await get(radio.base, '/api/settings')).dismissed || {}).rollback === '1.4.1', 6000, 'the rollback marked seen');
      // Updates: "When updates install", two equal install buttons, no Go back on the version that didn't work.
      const titles = await page.$$eval('#sec-version .li-title', ts => ts.map(t => t.textContent));
      assert(titles.includes('When updates install') && !titles.includes('Updates'), 'titles: ' + titles.join('|'));
      const inst = await page.$$eval('#sec-version .acts .btn', bs => bs.map(b => b.textContent + ':' + b.classList.contains('primary')));
      assert(inst.join() === 'Install tonight:false,Install now:false', 'install buttons: ' + inst.join());
      const backs = await page.$$eval('#sec-version .li', ls => ls.filter(l => /Didn’t work here/.test(l.textContent)).map(l => !!l.querySelector('.li-act')));
      assert(backs.length === 1 && backs[0] === false, 'Go back offered on the bad version');
      await page.goto(radio.base + '/#settings');
      await until(() => page.isVisible('#setrow-attn-update'), 6000, 'the list again');
      await sleep(500);
      assert(!(await page.$('#setrow-attn-rollback')), 'the rollback row back after it was seen');
      await page.close();
    });

    await check('a change refused for the page’s address: after Not now the red toast offers Allow', async () => {
      const page = await fresh('#settings/sound', `http://myradio.test:${radio.port}`);
      await until(() => page.isVisible('#limit'), 6000, 'the ceiling slider');
      await until(async () => !(await page.$eval('#limit', e => e.disabled)), 4000, 'the slider live');
      const slide = v => page.$eval('#limit', (el, v) => { el.value = v; el.dispatchEvent(new Event('input', {bubbles: true})); }, v);
      await slide(60);
      await until(() => page.isVisible('#hostAllow'), 6000, 'the offer');
      await page.click('text=Not now');
      await until(async () => (await page.$eval('#limit', e => e.value)) === '70', 4000, 'the slider back to the radio’s value');
      await slide(55);
      await until(async () => (await page.textContent('#toast')).includes('can’t make changes yet'), 6000, 'the red toast');
      assert(await page.evaluate(() => document.getElementById('toast').classList.contains('bad')), 'toast not red');
      assert((await page.textContent('.toast-undo')) === 'Allow', 'toast action: ' + await page.textContent('.toast-undo'));
      assert(!(await page.$eval('#limitErr', e => e.textContent)), 'an inline error for a host refusal: ' + await page.$eval('#limitErr', e => e.textContent));
      await page.click('.toast-undo');
      await until(() => page.isVisible('#hostAllow'), 4000, 'the offer again from the toast');
      await page.click('text=Not now');
      await page.close();
    });

    await check('the radio says no to an instant change: the words sit by the control, the control goes back', async () => {
      const page = await fresh('#settings/sound');
      await until(async () => await page.isVisible('#limit') && !(await page.$eval('#limit', e => e.disabled)), 6000, 'the slider');
      await page.route('**/api/settings', r => r.request().method() === 'PUT' ? r.fulfill({status: 400, json: {ok: false, error: 'that ceiling is too high for this cabinet'}}) : r.continue());
      await page.$eval('#limit', el => { el.value = 95; el.dispatchEvent(new Event('input', {bubbles: true})); });
      await until(async () => /too high/.test(await page.textContent('#limitErr')), 4000, 'the inline error');
      assert(/It’s back where it was/.test(await page.textContent('#limitErr')), 'err: ' + await page.textContent('#limitErr'));
      await until(async () => (await page.$eval('#limit', e => e.value)) === '70', 4000, 'the slider back');
      assert(!(await page.evaluate(() => document.getElementById('toast').classList.contains('show') && document.getElementById('toast').classList.contains('bad'))), 'a red toast as well');
      await page.unroute('**/api/settings');
      await page.close();
    });

    await check('Home Assistant: the token can be shown; the Can’t reach card’s Forget it asks in the sheet', async () => {
      const page = await ctx.newPage();
      page.on('pageerror', e => errors.push('ha: ' + e.message));
      page.on('dialog', d => { natives.push(d.message()); d.dismiss(); });
      let dropped = 0;
      await page.route('**/api/ha', r => {
        if (r.request().method() === 'DELETE'){ dropped++; return r.fulfill({json: {configured: false, connected: false, missing: []}}); }
        if (r.request().method() !== 'GET') return r.continue();
        return r.fulfill({json: {configured: !dropped, connected: false, url: 'http://ha.local:8123', location: '', version: '', publish: false, error: 'It didn’t answer.', missing: [], previous_instance: null, moved: null, lost: null}});
      });
      await page.goto(radio.base + '/#settings/radio/ha');
      await until(() => page.isVisible('#haTokShow'), 6000, 'the Show button');
      assert((await page.getAttribute('#haTok', 'type')) === 'password', 'token shown by default');
      await page.click('#haTokShow');
      assert((await page.getAttribute('#haTok', 'type')) === 'text' && (await page.textContent('#haTokShow')) === 'Hide', 'Show did nothing');
      await page.click('text=Forget it');
      await until(() => page.isVisible('#askOk'), 3000, 'the sheet');
      assert(/Disconnect Home Assistant\?/.test(await dlgText(page)), 'sheet: ' + await dlgText(page));
      await page.click('#askOk');
      await until(() => dropped === 1, 4000, 'the DELETE');
      await page.close();
    });

    await check('no browser confirm() boxes, no console errors', async () => {
      assert(!natives.length, natives.length + ' native dialogs: ' + natives.join(' | '));
      assert(!errors.length, errors.join('\n     '));
    });
  } finally {
    await browser.close();
    radio.proc.kill();
    fs.rmSync(radio.tmp, {recursive: true, force: true});
  }
  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
