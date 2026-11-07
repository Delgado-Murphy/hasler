#!/usr/bin/env node
// "Make it yours" (1.5.0) against a fresh fake radio (haslerd --fake) in Chrome: the
// front page's quiet line for a new radio, the section on the Settings home with its
// rows from what the radio knows, the Sound page's "not set yet" note, Skip (and the
// Allow offer from another address), Hide → the row under This radio, the keys row
// opening Start from a set, completion ("That’s it. It’s yours." and the lamp's small
// OK), the Handover group in Maker tools, the welcome card, and the setup-mode line.
//
//   cd daemon && node tests/e2e/make_it_yours.cjs
//
// Same needs as settings_ia.cjs: Node, Playwright (PLAYWRIGHT=/path/to/node_modules/playwright),
// a browser (PW_CHANNEL=chrome for installed Chrome), HASLER_PY for the fake radio's Python.
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
  // A state dir, so settings.json holds made_mine as on a radio; allow_unconfirmed stands in for
  // the key press a starter set asks for; the install mark makes the radio "new" (installed today).
  fs.writeFileSync(path.join(tmp, 'etc', 'box.yaml'), `schema: 1\nstate_file: ${path.join(tmp, 'state', 'state.json')}\nweb:\n  host: 127.0.0.1\n  port: ${port}\n  allow_unconfirmed: true\n`);
  fs.writeFileSync(path.join(tmp, 'state', 'install-done'), new Date().toISOString().replace(/\.\d+Z$/, 'Z') + '\n');
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
const send = (base, method, url, body) => fetch(base + url, {method, headers: {'Content-Type': 'application/json', Origin: base}, body: JSON.stringify(body)});
const get = async (base, url) => (await fetch(base + url)).json();

(async () => {
  const radio = await startRadio();
  const browser = await launch();
  const errors = [];
  const ctx = await browser.newContext({viewport: {width: 390, height: 844}, deviceScaleFactor: 2});
  await ctx.grantPermissions(['clipboard-read', 'clipboard-write'], {origin: radio.base});
  const fresh = async (hash, origin, c) => {
    const page = await (c || ctx).newPage();
    page.on('console', m => { if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(hash + ': ' + m.text()); });
    page.on('pageerror', e => errors.push(hash + ': ' + e.message));
    await page.goto((origin || radio.base) + '/' + hash);
    return page;
  };
  // The open rows, by id, and the count / Done line as shown.
  const yours = page => page.evaluate(() => ({
    rows: [...document.querySelectorAll('#yoursList .yours-row')].map(r => ({id: r.dataset.row, title: r.querySelector('.li-title').textContent, sub: r.querySelector('.li-sub').textContent,
      core: !!r.querySelector('.yours-dot.core'), skip: !!r.querySelector('.li-act'), link: r.querySelector('.yours-go').tagName})),
    count: (document.getElementById('yoursCount') || {}).textContent || '', done: (document.getElementById('yoursDone') || {}).textContent || '',
    lede: !!document.getElementById('yoursLede'), section: !!document.getElementById('sec-yours')}));
  const madeMine = async () => (await get(radio.base, '/api/settings')).made_mine;
  try {
    await check('a fresh radio: /api/info says when it was installed; the front page says "New radio? Make it yours — two things to do first."', async () => {
      const info = await get(radio.base, '/api/info');
      assert(info.installed_at && Date.now() - Date.parse(info.installed_at) < 60000, 'installed_at: ' + info.installed_at);
      const page = await fresh('#');
      await until(() => page.isVisible('#newLine'), 8000, 'the new-radio line');
      assert((await page.textContent('#newLine')) === 'New radio? Make it yours — two things to do first.', 'line: ' + await page.textContent('#newLine'));
      assert((await page.getAttribute('#newLineLink', 'href')) === '#settings', 'link: ' + await page.getAttribute('#newLineLink', 'href'));
      assert(!(await page.isVisible('#setupLine')), 'the setup line on a radio that is not in setup mode');
      await page.close();
    });

    await check('Settings home: Make it yours above the page list, five rows from what the radio knows, the two core ones first and without Skip', async () => {
      const page = await fresh('#settings');
      await until(() => page.isVisible('#yoursList'), 8000, 'the section');
      await sleep(600);
      const y = await yours(page);
      assert(y.rows.map(r => r.id).join() === 'ceiling,keys,room,place,ha', 'rows: ' + y.rows.map(r => r.id).join());
      assert(y.count === '5 of 5 to go' && y.lede && !y.done, 'count/lede/done: ' + JSON.stringify([y.count, y.lede, y.done]));
      const by = Object.fromEntries(y.rows.map(r => [r.id, r]));
      assert(by.ceiling.title === 'Set how loud it may go' && by.ceiling.sub === 'Set it by ear — it protects the 1964 speaker.', 'ceiling: ' + JSON.stringify(by.ceiling));
      assert(by.keys.sub === 'They still play a test set. Choose your music.' && by.keys.link === 'BUTTON', 'keys: ' + JSON.stringify(by.keys));
      assert(by.ceiling.core && by.keys.core && !by.room.core && !by.ceiling.skip && !by.keys.skip && !by.room.skip && by.place.skip && by.ha.skip, 'core/skip marks: ' + JSON.stringify(y.rows));
      assert(by.room.link === 'A' && (await page.getAttribute('#yours-room', 'href')) === '#settings/rooms/name', 'the room row is a link into Rooms');
      assert((await page.getAttribute('#yours-ceiling', 'href')) === '#settings/sound/volume' && (await page.getAttribute('#yours-place', 'href')) === '#settings/lamp/night', 'deep links');
      assert(await page.evaluate(() => { const a = document.getElementById('sec-yours'), b = document.getElementById('setRows'); return !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING); }), 'the section is not above the page list');
      assert(/Hide this/.test(await page.textContent('.yours-foot')) && /it stays under This radio/.test(await page.textContent('.yours-foot')), 'footer: ' + await page.textContent('.yours-foot'));
      assert(/· not set yet/.test(await page.textContent('#setrow-sound .li-sub')), 'Sound summary: ' + await page.textContent('#setrow-sound .li-sub'));
      await page.close();
    });

    await check('Sound: the recipe under the ceiling while it is unset; saving a ceiling flips the row, the summary and the front-page line', async () => {
      const page = await fresh('#settings/sound');
      await until(async () => await page.isVisible('#limit') && !(await page.$eval('#limit', e => e.disabled)), 6000, 'the slider');
      assert(await page.isVisible('#limitUnset'), 'no "not set yet" note');
      const note = await page.textContent('#limitUnset');
      assert(/^Not set yet — how to set it/.test(note) && /Play something dense, hold key 6 until it stops rising/.test(note) && /go back two\.$/.test(note), 'note: ' + note);
      await page.$eval('#limit', el => { el.value = 62; el.dispatchEvent(new Event('input', {bubbles: true})); });
      await until(async () => (await get(radio.base, '/api/settings')).volume_limit_set === true, 6000, 'the ceiling saved');
      await until(async () => !(await page.isVisible('#limitUnset')), 4000, 'the note to go');
      await page.goto(radio.base + '/#settings');
      await until(async () => /Done · how loud/.test((await yours(page)).done), 8000, 'the Done line');
      const y = await yours(page);
      assert(y.rows.map(r => r.id).join() === 'keys,room,place,ha' && y.count === '4 of 5 to go' && !y.lede, 'after the ceiling: ' + JSON.stringify([y.rows.map(r => r.id), y.count, y.lede]));
      assert(/^Ceiling 62 · quietest step/.test(await page.textContent('#setrow-sound .li-sub')), 'Sound summary: ' + await page.textContent('#setrow-sound .li-sub'));
      await page.goto(radio.base + '/#');
      await until(async () => (await page.textContent('#newLine')) === 'New radio? Make it yours — put your music on the keys.', 8000, 'the front line after the ceiling');
      await page.close();
    });

    await check('Skip: "where it stands" joins the Done line as skipped, and the radio remembers it for every phone', async () => {
      const page = await fresh('#settings');
      await until(() => page.isVisible('#skip-place'), 8000, 'the Skip on the place row');
      await page.click('#skip-place');
      await until(async () => ((await madeMine()).skipped || []).includes('place'), 6000, 'the skip saved');
      await until(async () => /where it stands skipped/.test((await yours(page)).done), 4000, 'the Done line');
      const y = await yours(page);
      assert(y.done === '✓Done · how loud · where it stands skipped' && y.count === '3 of 5 to go', 'done/count: ' + JSON.stringify([y.done, y.count]));
      assert(y.rows.map(r => r.id).join() === 'keys,room,ha', 'rows: ' + y.rows.map(r => r.id).join());
      await page.close();
    });

    await check('from another address the rows stay links and Skip goes through the Allow offer', async () => {
      const page = await fresh('#settings', `http://myradio.test:${radio.port}`);
      await until(() => page.isVisible('#skip-ha'), 8000, 'the list');
      assert((await page.getAttribute('#yours-room', 'href')) === '#settings/rooms/name', 'rows are not links');
      await page.click('#skip-ha');
      await until(() => page.isVisible('#hostAllow'), 6000, 'the offer');
      await page.click('text=Not now');
      await until(async () => (await page.textContent('#toast')).includes('can’t make changes yet'), 6000, 'the red toast');
      assert((await page.textContent('.toast-undo')) === 'Allow', 'toast action: ' + await page.textContent('.toast-undo'));
      assert(!((await madeMine()).skipped || []).includes('ha'), 'the skip went through without Allow');
      await page.close();
    });

    await check('Hide this: the section goes, a row under This radio says what is left, and tapping it brings the list back', async () => {
      const page = await fresh('#settings');
      await until(() => page.isVisible('#yoursHide'), 8000, 'the footer');
      await page.click('#yoursHide');
      await until(async () => !!(await madeMine()).hidden_at, 6000, 'hidden_at saved');
      await until(async () => !(await page.$('#sec-yours')), 4000, 'the section to go');
      await until(async () => /under This radio/.test(await page.textContent('#toast')), 4000, 'the toast');
      await page.goto(radio.base + '/#');
      await sleep(800);
      assert(!(await page.isVisible('#newLine')), 'the front line while the list is hidden');
      await page.goto(radio.base + '/#settings/radio');
      await until(() => page.isVisible('#setrow-yours'), 8000, 'the row under This radio');
      assert(/^Make it yours$/.test(await page.textContent('#setrow-yours .li-title')) && /^3 to go/.test(await page.textContent('#setrow-yours .li-sub')), 'row: ' + await page.textContent('#setrow-yours'));
      await page.click('#setrow-yours');
      await until(async () => !(await madeMine()).hidden_at, 6000, 'hidden_at cleared');
      await until(async () => page.url().endsWith('#settings') && await page.isVisible('#yoursList'), 6000, 'the list back on Settings');
      await page.close();
    });

    await check('Put your music on the keys opens Start from a set; a chosen set makes the row done and brings the card row', async () => {
      const page = await fresh('#settings');
      await until(() => page.isVisible('#yours-keys'), 8000, 'the keys row');
      await page.click('#yours-keys');
      await page.waitForSelector('#setsDlg[open] .ss-set');
      await page.click('#setsDlg [data-choose="jazz"]');
      await until(async () => (await get(radio.base, '/api/config')).starter_from === 'page', 6000, 'the set chosen');
      await until(async () => /your keys/.test((await yours(page)).done), 6000, 'the keys row done');
      const y = await yours(page);
      assert(y.rows.map(r => r.id).join() === 'room,ha,print' && y.count === '3 of 6 to go', 'rows/count: ' + JSON.stringify([y.rows.map(r => r.id), y.count]));
      const print = y.rows.find(r => r.id === 'print');
      assert(print.skip && print.title === 'Print a card for the radio' && (await page.getAttribute('#yours-print', 'href')) === '#print', 'the card row: ' + JSON.stringify(print));
      await page.close();
    });

    await check('completion: the room named, the rest skipped → "That’s it. It’s yours." once, and the lamp’s small OK', async () => {
      const page = await fresh('#settings');
      let oks = 0;
      await page.route('**/api/lamp/ok', r => { oks++; r.continue(); });
      await until(() => page.isVisible('#skip-ha'), 8000, 'the list');
      const r = await send(radio.base, 'PUT', '/api/settings', {room: {name: 'Study'}});
      assert(r.ok, 'room PUT ' + r.status);
      await page.click('#skip-ha');
      await until(() => page.isVisible('#skip-print'), 6000, 'the card row');
      await page.click('#skip-print');
      await until(() => page.isVisible('#yoursFin'), 6000, 'the last line');
      assert((await page.textContent('#yoursFin')) === 'That’s it. It’s yours.', 'fin: ' + await page.textContent('#yoursFin'));
      assert(!(await page.$('#yoursList')), 'rows left under the last line');
      await until(() => oks === 1, 3000, 'the lamp OK');
      const mm = await madeMine();
      assert(mm.skipped.join() === 'place,ha,print' && !mm.hidden_at, 'made_mine: ' + JSON.stringify(mm));
      await sleep(8600);
      assert(!(await page.$('#sec-yours')), 'the section still there after the last line');
      await page.goto(radio.base + '/#');
      await sleep(800);
      assert(!(await page.isVisible('#newLine')) && oks === 1, 'front line / OK count after completion: ' + oks);
      await page.close();
    });

    await check('Maker tools: Handover first, each line marked, the ! lines open where they are fixed; Copy handover note; Print welcome card', async () => {
      const maker = await browser.newContext({viewport: {width: 390, height: 844}});
      await maker.grantPermissions(['clipboard-read', 'clipboard-write'], {origin: radio.base});
      await maker.addInitScript(() => { try { localStorage.setItem('hasler.maker', '1'); } catch (_) {} });
      const page = await fresh('#settings/maker', null, maker);
      await until(() => page.isVisible('#hoList'), 8000, 'the Handover group');
      await until(async () => !(await page.$$eval('#hoList .li-sub', ss => ss.some(s => /Checking/.test(s.textContent)))), 8000, 'every line answered');
      assert((await page.$eval('#panel .grp-sec', s => s.id)) === 'sec-handover', 'Handover is not the first group');
      const rows = await page.$$eval('#hoList .ho-row', rs => rs.map(r => ({id: r.id, mark: r.dataset.mark, sub: r.querySelector('.li-sub').textContent, href: r.getAttribute('href')})));
      const by = Object.fromEntries(rows.map(r => [r.id, r]));
      assert(rows.map(r => r.id).join() === 'ho-software,ho-health,ho-volume-ceiling,ho-knobs,ho-lamp,ho-wi-fi,ho-setup-network,ho-bluetooth,ho-spotify,ho-youtube-music,ho-other-addresses,ho-remote-help,ho-keys', 'lines: ' + rows.map(r => r.id).join());
      assert(by['ho-volume-ceiling'].mark === 'ok' && by['ho-volume-ceiling'].sub === '62', 'ceiling: ' + JSON.stringify(by['ho-volume-ceiling']));
      assert(by['ho-software'].mark === 'bad' && by['ho-software'].href === '#settings/radio/updates', 'software: ' + JSON.stringify(by['ho-software']));
      assert(by['ho-lamp'].mark === 'bad' && by['ho-lamp'].href === '#settings/maker/colours', 'lamp: ' + JSON.stringify(by['ho-lamp']));
      assert(by['ho-keys'].mark === 'ok' && /factory set · Jazz & soul/.test(by['ho-keys'].sub), 'keys: ' + JSON.stringify(by['ho-keys']));
      assert(by['ho-setup-network'].mark === 'ok' && /^Hasler · open, no password/.test(by['ho-setup-network'].sub) && by['ho-setup-network'].href === '#print/welcome', 'setup network: ' + JSON.stringify(by['ho-setup-network']));
      assert(by['ho-other-addresses'].mark === 'ok' && by['ho-remote-help'].mark === 'dot' && by['ho-spotify'].mark === 'dot', 'marks: ' + JSON.stringify(rows.map(r => r.id + ':' + r.mark)));
      assert(/things to see to before the radio leaves the bench/.test(await page.textContent('#hoNote')), 'note: ' + await page.textContent('#hoNote'));
      await page.click('#hoCopy');
      await until(async () => /Handover note copied/.test(await page.textContent('#toast')), 4000, 'the copied toast');
      const text = await page.evaluate(() => navigator.clipboard.readText());
      assert(/^Hasler · Study · handover · /.test(text) && /\nok Volume ceiling: 62\n/.test(text) && /\n! Software: updates not set up/.test(text), 'note text:\n' + text);
      await page.click('#hoCard');
      await until(() => page.isVisible('.kc-welcome'), 6000, 'the welcome card');
      assert(page.url().endsWith('#print'), 'url: ' + page.url());
      assert((await page.textContent('#pageTitle')) === 'Welcome card', 'title: ' + await page.textContent('#pageTitle'));
      await page.close();
      await maker.close();
    });

    await check('the welcome card: both sides on one sheet; the setup password only when ticked', async () => {
      const page = await ctx.newPage();
      page.on('pageerror', e => errors.push('welcome: ' + e.message));
      await page.route('**/api/wifi', r => r.request().method() === 'GET'
        ? r.fulfill({json: {hotspot: false, current: 'Casa', networks: [], saved: [], setup: {ssid: 'Hasler', password_set: true, password: 'abcd-efgh-jkmn', address: '172.24.1.1', page: 'http://172.24.1.1:8080/wifi'}}}) : r.continue());
      await page.goto(radio.base + '/#print/welcome');
      await until(() => page.isVisible('.kc-welcome .wc-back'), 8000, 'the card');
      await until(() => page.isVisible('#cardPw'), 6000, 'the password box');
      await until(async () => /Study/.test(await page.textContent('.wc-front')), 6000, 'the room on the card');   // once the settings have landed
      const front = await page.textContent('.wc-front'), back = await page.textContent('.wc-back');
      assert(/Hasler/.test(front) && /Study/.test(front) && /Press 1\./.test(front) && /http:\/\/[\w.-]+:8080/.test(front), 'front: ' + front);
      assert(/join the Wi-Fi network Hasler, password from the makers, and a page opens\./.test(front), 'front setup line: ' + front);
      assert(/Switching off/.test(front) && /Dark means safe to unplug/.test(front) && /tone knob/.test(front), 'front off lines: ' + front);
      assert(/What the lamp says/.test(back) && /set me up/.test(back) && /Make it yours/.test(back) && /1how loud2your keys3the room’s name/.test(back), 'back: ' + back);
      assert((await page.$$('.kc-welcome .kc-mark')).length === 2, 'a maker’s mark on each side');
      await page.check('#cardPw');
      await until(() => page.isVisible('#wcPw'), 4000, 'the password on the card');
      assert((await page.textContent('#wcPw')) === 'abcd-efgh-jkmn', 'pw: ' + await page.textContent('#wcPw'));
      // The three sizes stay on offer; the key card is still the default from Edit keys' link.
      const sizes = await page.$$eval('[data-size]', bs => bs.map(b => b.dataset.size + ':' + b.getAttribute('aria-checked')));
      assert(sizes.join() === 'a6:false,strip:false,welcome:true', 'sizes: ' + sizes.join());
      await page.close();
    });

    await check('setup mode: the front page says so and points at the Wi-Fi page; an older radio gets no new-radio line', async () => {
      const page = await ctx.newPage();
      page.on('pageerror', e => errors.push('setup: ' + e.message));
      await page.route('**/api/status', async r => { const j = await (await r.fetch()).json(); j.lamp = 'setup'; r.fulfill({json: j}); });
      await page.route('**/api/settings', async r => { if (r.request().method() !== 'GET') return r.continue(); const j = await (await r.fetch()).json(); j.volume_limit_set = false; j.made_mine = {skipped: [], hidden_at: null}; r.fulfill({json: j}); });
      await page.goto(radio.base + '/#');
      await until(() => page.isVisible('#setupLine'), 8000, 'the setup line');
      assert((await page.textContent('#setupLine')) === 'Not on your Wi-Fi yet. Set it up — this page only works from the Hasler network until then, or wait: it tries again by itself.', 'line: ' + await page.textContent('#setupLine'));
      assert((await page.getAttribute('#setupLink', 'href')) === '/wifi', 'link: ' + await page.getAttribute('#setupLink', 'href'));
      assert(!(await page.isVisible('#newLine')), 'the new-radio line as well, in setup mode');
      await page.close();
      const old = await ctx.newPage();
      old.on('pageerror', e => errors.push('old: ' + e.message));
      await old.route('**/api/info', async r => { const j = await (await r.fetch()).json(); j.installed_at = new Date(Date.now() - 30 * 864e5).toISOString(); r.fulfill({json: j}); });
      await old.route('**/api/settings', async r => { if (r.request().method() !== 'GET') return r.continue(); const j = await (await r.fetch()).json(); j.volume_limit_set = false; j.made_mine = {skipped: [], hidden_at: null}; r.fulfill({json: j}); });
      await old.goto(radio.base + '/#');
      await sleep(1500);
      assert(!(await old.isVisible('#newLine')), 'a new-radio line on a month-old radio');
      await old.goto(radio.base + '/#settings');
      await until(() => old.isVisible('#yoursList'), 8000, 'the section still on Settings for an older radio');
      await old.unrouteAll({behavior: 'ignoreErrors'});   // a poll still in flight must not fail the close
      await old.close();
    });

    await check('no console errors', async () => {
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
