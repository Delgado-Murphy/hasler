#!/usr/bin/env node
// Moving this radio (Settings → This radio → Moving this radio), against the fake radio
// (haslerd --fake) in Chrome: the row on This radio, the page's three cards, Forget with
// the key press (and the sheet first for the current network), Hand over's confirm and
// the one-time password that goes onto the printed card, the "New Wi-Fi" attention row,
// Clock and place on the page and under Pilot lamp, the key card's "If it moves" block.
//
//   cd daemon && node tests/e2e/moving.cjs
//
// Same needs as settings_ia.cjs: Node, Playwright (PLAYWRIGHT=/path/to/node_modules/playwright),
// a browser (PW_CHANNEL=chrome for installed Chrome), HASLER_PY for the fake radio's Python.
// The key press itself is answered at the page's door (page.route on /api/confirm/request), as
// keypage_fixes.cjs does: the fake radio has no Wi-Fi helper and no hand to press with.
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
  fs.writeFileSync(path.join(tmp, 'etc', 'box.yaml'), `schema: 1\nstate_file: ${path.join(tmp, 'state', 'state.json')}\nweb:\n  host: 127.0.0.1\n  port: ${port}\n`);
  // The radio joined "Casa Nueva" an hour ago (main.consume_wifi_joined's note): the attention row.
  fs.writeFileSync(path.join(tmp, 'state', 'settings.json'), JSON.stringify({schema: 1, room: {name: 'Kitchen'},
    wifi_joined: {ssid: 'Casa Nueva', at: Date.now() / 1000 - 3600}}));
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
const shot = async (page, name) => { if (SHOTS){ fs.mkdirSync(SHOTS, {recursive: true}); await page.screenshot({path: path.join(SHOTS, name + '.png'), fullPage: true}); } };
const get = async (base, url) => (await fetch(base + url)).json();
const WIFI = {hotspot: false, current: 'Casa Nueva', networks: [], saved: ['Casa Nueva', 'Casa Vieja'],
  known: [{ssid: 'Casa Nueva', current: true}, {ssid: 'Casa Vieja', current: false}],
  setup: {ssid: 'Hasler-Kitchen', password_set: true, password: 'abcd-efgh-jkmn', address: '172.24.1.1', page: 'http://172.24.1.1:8080/wifi'}};

(async () => {
  const radio = await startRadio();
  const browser = await launch();
  const errors = [], natives = [];
  const ctx = await browser.newContext({viewport: {width: 390, height: 844}, deviceScaleFactor: 2});
  const fresh = async (hash) => {
    const page = await ctx.newPage();
    page.on('console', m => { if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(hash + ': ' + m.text()); });
    page.on('pageerror', e => errors.push(hash + ': ' + e.message));
    page.on('dialog', d => { natives.push(d.message()); d.dismiss(); });
    await page.route('**/api/wifi', r => r.request().method() === 'GET' ? r.fulfill({json: WIFI}) : r.continue());
    await page.goto(radio.base + '/' + hash);
    return page;
  };
  const dlgText = page => page.evaluate(() => document.getElementById('dlg').textContent);
  try {
    await check('This radio: a Moving row between Wi-Fi and Software; the page has its three cards and the note', async () => {
      const page = await fresh('#settings/radio');
      await until(() => page.isVisible('#setrow-move'), 6000, 'the Moving row');
      const heads = await page.$$eval('#panel h3.grp-lbl', hs => hs.map(x => x.textContent.trim()));
      assert(heads.slice(0, 3).join('|') === 'Wi-Fi|Moving|Software', 'headings: ' + heads.join('|'));
      assert((await page.textContent('#setrow-move .li-sub')) === 'Another room, another home, or a new owner', 'sub');
      await page.click('#setrow-move');
      await until(() => page.isVisible('#move-owner'), 6000, 'the owner card');
      assert((await page.textContent('#pageTitle')) === 'Moving this radio', 'title ' + await page.textContent('#pageTitle'));
      assert(/press on the radio/.test(await page.textContent('#pageNote')), 'note: ' + await page.textContent('#pageNote'));
      const cards = await page.$$eval('#panel .card-box > .mono', ms => ms.map(m => m.textContent));
      assert(cards.slice(0, 3).join('|') === 'Another room|Another home, or a new Wi-Fi|A new owner', 'cards: ' + cards.join('|'));
      assert((await page.getAttribute('#moveRename', 'href')) === '#settings/rooms/name' && (await page.textContent('#moveRename')) === 'Rename', 'Rename link');
      assert(await page.isVisible('#moveIdentify'), 'Identify missing');
      assert((await page.getAttribute('#moveWifi', 'href')) === '/wifi', 'the Change Wi-Fi link');
      await until(async () => (await page.$$('#moveNets .li[data-ssid]')).length === 2, 4000, 'the known networks');
      assert(/Casa Nueva/.test(await page.textContent('#moveNets .li[data-ssid="Casa Nueva"] .li-sub')) === false
        && /it is on now/.test(await page.textContent('#moveNets .li[data-ssid="Casa Nueva"] .li-sub')), 'the current network flagged');
      // The setup password: hidden, Show, Print.
      assert(/••••/.test(await page.textContent('#move-home')), 'password shown by default');
      await page.click('#movePwShow');
      assert(/abcd-efgh-jkmn/.test(await page.textContent('#move-home')), 'Show did nothing');
      assert(await page.isVisible('#handOver') && !(await page.$('#handResult')), 'the owner card before the press');
      assert(/the makers can still help/i.test(await page.textContent('#move-owner')), 'the makers’ sentence');
      await shot(page, 'moving-page');
      await page.close();
    });

    await check('Forget: a saved network takes the key press; the current one asks in the sheet first', async () => {
      const page = await fresh('#settings/radio/move');
      const asked = [], deleted = [];
      await page.route('**/api/confirm/request', r => { asked.push(r.request().postDataJSON()); return r.fulfill({json: {ok: true, token: 'tok1', state: 'confirmed', action: 'wifi'}}); });
      await page.route('**/api/wifi/networks/*', r => {
        deleted.push({url: decodeURIComponent(r.request().url().split('/api/wifi/networks/')[1]), body: r.request().postDataJSON()});
        return r.fulfill({json: {ok: true, forgotten: 'x', was_current: false, restarting: false}});
      });
      await until(async () => (await page.$$('#moveNets .li[data-ssid]')).length === 2, 6000, 'the known networks');
      await page.click('#moveNets .li[data-ssid="Casa Vieja"] button');
      await until(() => deleted.length === 1, 4000, 'the DELETE');
      assert(asked.length === 1 && asked[0].action === 'wifi' && asked[0].args.ssid === 'Casa Vieja' && asked[0].args.forget === true, 'the press asked: ' + JSON.stringify(asked));
      assert(deleted[0].url === 'Casa Vieja' && deleted[0].body.confirm === 'tok1', 'the DELETE: ' + JSON.stringify(deleted));
      assert(!(await page.$('#askOk')) || !(await page.isVisible('#askOk')), 'a sheet for a network it is not on');
      // The current one: the sheet, Keep it leaves it alone; Forget goes on to the press.
      await page.click('#moveNets .li[data-ssid="Casa Nueva"] button');
      await until(() => page.isVisible('#askOk'), 3000, 'the sheet');
      assert(/Forget Casa Nueva\?/.test(await dlgText(page)) && /setup network/.test(await dlgText(page)), 'sheet: ' + await dlgText(page));
      await page.click('#askCancel');
      await sleep(300);
      assert(deleted.length === 1, 'Keep it still forgot it');
      await page.click('#moveNets .li[data-ssid="Casa Nueva"] button');
      await until(() => page.isVisible('#askOk'), 3000, 'the sheet again');
      await page.click('#askOk');
      await until(() => deleted.length === 2, 4000, 'the second DELETE');
      assert(deleted[1].url === 'Casa Nueva' && asked.length === 2, 'the current network’s forget');
      await page.close();
    });

    await check('Hand over: the confirm, then the one-time password; Print a card puts it on the key card', async () => {
      const page = await fresh('#settings/radio/move');
      const asked = [], handed = [];
      await page.route('**/api/confirm/request', r => { asked.push(r.request().postDataJSON()); return r.fulfill({json: {ok: true, token: 'tok2', state: 'confirmed', action: 'handover'}}); });
      await page.route('**/api/handover', r => {
        if (r.request().method() !== 'POST') return r.continue();
        handed.push(r.request().postDataJSON());
        return r.fulfill({json: {ok: true, setup: {ssid: 'Hasler', password: 'vwxy-2345-mnpq'}, steps: [], failed: [], restarting: true}});
      });
      await until(() => page.isVisible('#handOver'), 6000, 'the Hand over button');
      // GET /api/handover from the real radio: what it forgets and keeps.
      const d = await get(radio.base, '/api/handover');
      assert(Array.isArray(d.forgets) && d.forgets.length >= 7 && Array.isArray(d.keeps) && Array.isArray(d.maker_notes), 'GET /api/handover: ' + JSON.stringify(d).slice(0, 200));
      assert(/knobs are calibrated/.test(await page.textContent('#move-owner')), 'the keeps list');
      await page.click('#handOver');
      await until(() => page.isVisible('#handResult'), 6000, 'the one-time block');
      assert(asked.length === 1 && asked[0].action === 'handover', 'the press asked: ' + JSON.stringify(asked));
      assert(handed.length === 1 && handed[0].confirm === 'tok2', 'POST /api/handover: ' + JSON.stringify(handed));
      assert((await page.textContent('#handPw')) === 'vwxy-2345-mnpq', 'the password: ' + await page.textContent('#handPw'));
      assert(/Write this down for the new owner/.test(await page.textContent('#move-owner')) && /restarts in setup mode/.test(await page.textContent('#move-owner')), 'the words');
      assert(!(await page.$('#handOver')), 'the button still there after the hand-over');
      await shot(page, 'moving-handed');
      await page.click('#handPrint');
      await until(() => page.isVisible('#kcMove'), 6000, 'the key card’s block');
      assert(await page.evaluate(() => location.hash) === '#print', 'hash ' + await page.evaluate(() => location.hash));
      assert((await page.textContent('#kcPw')) === 'vwxy-2345-mnpq', 'the card’s password: ' + await page.textContent('#kcPw'));
      assert(/Setup network: Hasler/.test(await page.textContent('#kcMove')) && /hasler-wifi\.txt/.test(await page.textContent('#kcMove')), 'the block: ' + await page.textContent('#kcMove'));
      await page.close();
    });

    await check('the key card carries the setup password to start; unticking the Include box leaves a blank', async () => {
      const page = await fresh('#print');
      await until(() => page.isVisible('#kcMove'), 6000, 'the block');
      await until(async () => (await page.textContent('#kcPw')) === 'abcd-efgh-jkmn', 6000, 'the password on the card: ' + await page.textContent('#kcPw'));
      assert(/Page: \S+:8080/.test(await page.textContent('#kcMove')), 'the address line: ' + await page.textContent('#kcMove'));
      await until(() => page.$('#printPw'), 4000, 'the Include box');
      assert(await page.isChecked('#printPw'), 'ticked to start');
      await page.click('#printPw');
      await until(async () => (await page.textContent('#kcPw')) === '________', 3000, 'the blank');
      await page.close();
    });

    await check('Needs attention: "New Wi-Fi: Casa Nueva" for a day, pointing at Moving this radio', async () => {
      const page = await fresh('#settings');
      await until(() => page.isVisible('#setrow-attn-newwifi'), 8000, 'the attention row');
      assert(/New Wi-Fi: Casa Nueva/.test(await page.textContent('#setrow-attn-newwifi')) && /Moved house\?/.test(await page.textContent('#setrow-attn-newwifi')), 'row: ' + await page.textContent('#setrow-attn-newwifi'));
      assert((await page.getAttribute('#setrow-attn-newwifi', 'href')) === '#settings/radio/move', 'href');
      await page.close();
    });

    await check('Clock and place: on the Moving page and under Pilot lamp → At night; a zone can be typed', async () => {
      const page = await fresh('#settings/radio/move');
      await until(() => page.isVisible('#clockZone'), 6000, 'the clock row');
      const put = [];
      await page.route('**/api/settings', r => {
        if (r.request().method() !== 'PUT'){ return r.continue(); }
        put.push(r.request().postDataJSON());
        return r.continue();
      });
      await page.click('#clockEdit');
      await page.fill('#clockInp', 'Europe/Zurich');
      await page.click('#clockSave');
      await until(() => put.length === 1, 4000, 'the PUT');
      assert(put[0].timezone === 'Europe/Zurich', 'PUT: ' + JSON.stringify(put));
      await page.goto(radio.base + '/#settings/lamp');
      await until(() => page.isVisible('#sec-night #clockZone'), 6000, 'the clock under At night');
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
})();
