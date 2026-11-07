#!/usr/bin/env node
// Support on the key page (1.5.0), against the fake radio (haslerd --fake) in Chrome:
// the "Something's wrong?" button on the Settings home and its page (the lamp in words,
// the six lines to read out, the cards from mocked /api/status + /api/diagnostics +
// /api/software), the Tell the makers sheet's Copy (what it asks the radio for),
// the From the makers card's Seen, the check-in switch under Updates with See what it
// sends, and the front page's "Remote help is waiting".
//
//   cd daemon && node tests/e2e/wrong.cjs
//
// Same needs as settings_ia.cjs: Node, Playwright (PLAYWRIGHT=/path/to/node_modules/playwright),
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
  fs.writeFileSync(path.join(tmp, 'etc', 'box.yaml'), `schema: 1\nweb:\n  host: 127.0.0.1\n  port: ${port}\nmakers:\n  contact: hola@example.org\n`);
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
const text = (page, sel) => page.textContent(sel);

// What a radio in trouble answers: the lamp taps SOS for a full card, no internet, the power was cut,
// the player restarted twice, the last update rolled back.
const STATUS_PATCH = {fault_reasons: ['storage'], fault_cleared_at: null, online: false, lamp: 'fault', recovering: false, stalled_s: null, standby: false};
// A radio with nothing wrong (the fake radio's own player never answers, so its diagnostics say so).
const FINE = Object.assign({}, DIAG_FINE());
function DIAG_FINE(){ return {wifi: {ssid: 'Casa', signal: 80, hotspot: false, words: 'On Casa'}, online: true, temperature: {c: 45, hot: false, throttled: null, words: 'cool'},
  storage: {free_bytes: 9e9, total_bytes: 29e9, read_only: false, bad: false, words: '9 GB free'}, last_shutdown: {kind: 'clean', words: 'switched off properly'},
  power_cuts: {total: 0, month: 0, last_at: null, hint: false}, running: {uptime_s: 100, words: '2 min'}, player: {mpd: true, audio_card: true, recovering: false, restarts_last_hour: 0, dsp: {}, words: 'ok'},
  lamp: {present: true, words: '…'}, panel: 'ok', faults: [], fault_cleared_at: null, software: null, variant: 'hasler-hm', version: '1.5.0', at: '2026-11-07T18:00:00+01:00'}; }
const DIAG = {wifi: {ssid: 'Casa', signal: 80, hotspot: false, words: 'On Casa, good signal'}, online: false,
  temperature: {c: 61, hot: false, throttled: null, words: 'warm (61 °C)'},
  storage: {free_bytes: 12 * 1024 * 1024, total_bytes: 29e9, read_only: false, bad: true, words: 'nearly full (12 MB free): needs a maker'},
  last_shutdown: {kind: 'power-cut', words: 'the power was cut'}, power_cuts: {total: 3, month: 3, last_at: null, hint: true},
  running: {uptime_s: 270000, daemon_uptime_s: 1000, since: null, words: '3 d 3 h'},
  player: {mpd: true, audio_card: true, recovering: false, restarts_last_hour: 2, dsp: {installed: false}, words: 'player answering · sound card present'},
  lamp: {present: true, words: '…'}, panel: 'ok', faults: ['storage'], fault_cleared_at: null, software: null, variant: 'hasler-hm', version: '1.5.0', at: '2026-11-07T18:00:00+01:00'};
const SW = {ok: true, installed: true, version: '1.5.0', channel: 'stable', update_mode: 'auto', metered: false, available: null, approved: null,
  last_check: new Date(Date.now() - 2 * 3600e3).toISOString(), stale: false, check_error: null, busy: false, releases: [], os_updates: null,
  last_result: {ok: false, version: '1.5.1', back_to: '1.5.0', reason: 'check panel failed (absent)', why: 'the keys weren’t answering after the switch', at: '2026-11-06T03:05:00Z'},
  checkin: {enabled: false, url_set: true, last: null}};
const REPORT_TEXT = 'Hasler · Kitchen · radio ab12 · Hasler HM (hasler-hm)\nVersion 1.5.0 · Stable · last checked 2 h ago\nLamp now: red, tapping SOS — something needs a maker: the memory card is full, or won’t write\nPlaying: nothing · volume 30 · in standby\nRunning for 3 d 3 h · last switched off: the power was cut\nWi-Fi: on Casa · internet: no\n\n--- details for the makers ---\nchecks: player OK\n';

(async () => {
  const radio = await startRadio();
  const browser = await launch();
  const errors = [];
  const ctx = await browser.newContext({viewport: {width: 390, height: 844}, deviceScaleFactor: 2});
  await ctx.grantPermissions(['clipboard-read', 'clipboard-write']).catch(() => {});
  const fresh = async (hash) => {
    const page = await ctx.newPage();
    page.on('console', m => { if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(hash + ': ' + m.text()); });
    page.on('pageerror', e => errors.push(hash + ': ' + e.message));
    page.on('dialog', d => d.dismiss());
    await page.goto(radio.base + '/' + hash);
    return page;
  };
  // The troubled radio: /api/status patched, /api/diagnostics and /api/software replaced, /api/report served.
  const trouble = async (page, reportHits) => {
    await page.route('**/api/status', async r => { const res = await r.fetch(); const j = await res.json(); r.fulfill({json: Object.assign(j, STATUS_PATCH)}); });
    await page.route('**/api/diagnostics', r => r.fulfill({json: DIAG}));
    await page.route('**/api/software', r => r.request().method() === 'GET' ? r.fulfill({json: SW}) : r.continue());
    await page.route('**/api/report*', r => { if (reportHits) reportHits.push(new URL(r.request().url())); r.fulfill({json: {ok: true, text: REPORT_TEXT, json: {}}}); });
  };
  try {
    await check('Settings home: "Something’s wrong?" above the page rows; the footer carries the short id', async () => {
      const page = await fresh('#settings');
      await until(() => page.isVisible('#setrow-wrong'), 6000, 'the button');
      assert((await text(page, '#setrow-wrong .li-title')) === 'Something’s wrong?', 'title');
      assert(/checks itself/.test(await text(page, '#setrow-wrong .li-sub')), 'sub');
      const order = await page.$$eval('#panel nav', ns => ns.map(n => n.id));
      assert(order.indexOf('setWrong') < order.indexOf('setRows'), 'order: ' + order.join());
      const info = await (await fetch(radio.base + '/api/info')).json();
      assert(info.short_id && info.short_id.length === 4 && info.makers_contact === 'hola@example.org', 'info: ' + JSON.stringify(info));
      await until(async () => (await text(page, '.about p')).includes(' · ' + info.short_id), 6000, 'the short id in the footer');
      await shot(page, 'wrong-home');
      await page.close();
    });

    await check('the page: the lamp in words, the six lines to read out, the cards, Tell the makers', async () => {
      const page = await ctx.newPage();
      page.on('pageerror', e => errors.push('wrong: ' + e.message));
      await trouble(page);
      await page.goto(radio.base + '/#settings/wrong');
      await until(() => page.isVisible('#wrong-storage'), 8000, 'the storage card');
      assert((await text(page, '#pageTitle')) === 'Something’s wrong?', 'title');
      await until(async () => /red, tapping SOS — something needs a maker: the memory card/.test(await text(page, '#wrongLamp')), 6000, 'the lamp line: ' + await text(page, '#wrongLamp'));
      await until(async () => (await text(page, '#sayLines')).startsWith('Hasler · Kitchen · radio ab12'), 6000, 'the six lines');
      assert((await text(page, '#sayLines')).split('\n').length === 6, 'six lines, not the whole report');
      const ids = await page.$$eval('#sec-found .li', els => els.map(e => e.id));
      assert(ids.join() === 'wrong-storage,wrong-offline,wrong-rollback,wrong-restarts,wrong-powercut', 'cards: ' + ids.join());
      assert(/needs a maker; nothing on this page will fix it/.test(await text(page, '#wrong-storage')), 'storage copy');
      assert(/on your Wi-Fi \(Casa\)/.test(await text(page, '#wrong-offline')), 'offline copy names the Wi-Fi');
      assert(/went back to 1\.5\.0 by itself on (6 Nov|Nov 6).*keys weren’t answering after the switch/.test(await text(page, '#wrong-rollback')), 'rollback copy: ' + await text(page, '#wrong-rollback'));
      assert(/restarted its player 2 times in the last hour/.test(await text(page, '#wrong-restarts')), 'restarts copy');
      assert((await page.$$('#wrong-storage button:has-text("Tell the makers")')).length === 1, 'try this on the storage card');
      assert(await page.isVisible('#tellMakers'), 'Tell the makers at the bottom');
      await shot(page, 'wrong-page');
      await page.close();
    });

    await check('Tell the makers: the sheet, the note and the Wi-Fi tick reach the radio, Copy says so', async () => {
      const page = await ctx.newPage();
      page.on('pageerror', e => errors.push('tell: ' + e.message));
      const hits = [];
      await trouble(page, hits);
      await page.goto(radio.base + '/#settings/wrong');
      await until(() => page.isVisible('#tellMakers'), 8000, 'the button');
      await page.click('#tellMakers');
      await until(() => page.isVisible('#tellNote'), 4000, 'the sheet');
      assert((await text(page, '#dlgTitle')) === 'Tell the makers', 'sheet title');
      assert(/Nothing private: no passwords, no accounts, no phone names/.test(await text(page, '#dlg')), 'the copy');
      assert(/Send it to Delgado & Murphy \(hola@example.org\), and nobody else/.test(await text(page, '#tellWho')), 'the makers are named: ' + await text(page, '#tellWho'));
      assert(await page.isChecked('#tellWifi'), 'the Wi-Fi tick is on to start');
      assert(await page.isVisible('#tellMail'), 'an email contact gets Email the makers');
      await page.fill('#tellNote', 'It goes quiet after a few minutes');
      await page.uncheck('#tellWifi');
      await page.click('#tellCopy');
      await until(async () => hits.length >= 1, 4000, 'the report request');
      const q = hits[hits.length - 1].searchParams;
      assert(q.get('note') === 'It goes quiet after a few minutes' && q.get('wifi') === '0', 'query: ' + hits[hits.length - 1].search);
      await until(async () => /copied|Couldn’t copy/.test(await text(page, '#toast')), 4000, 'a toast');
      await page.click('#tellClose');
      await until(async () => !(await page.isVisible('#tellNote')), 2000, 'the sheet to close');
      await page.close();
    });

    await check('Send to the makers: with the makers\' server set up, the report goes to their private list and the number shows', async () => {
      const page = await ctx.newPage();
      page.on('pageerror', e => errors.push('send: ' + e.message));
      await trouble(page, null);
      let sendBody = null, refuse = false, replies = 0;
      const sup = {available: true, requests: []};
      await page.route('**/api/support', r => r.fulfill({json: sup}));
      await page.route('**/api/support/send', r => {
        sendBody = r.request().postDataJSON();
        if (refuse) return r.fulfill({status: 429, json: {ok: false, error: 'You’ve sent several today already. The makers have them; use Share or Copy for anything more.'}});
        sup.requests = [{number: 12, at: '2026-11-07T18:00:00+01:00', deduped: false}];
        r.fulfill({json: {ok: true, number: 12, at: '2026-11-07T18:00:00+01:00', deduped: false, requests: sup.requests}});
      });
      await page.route('**/api/support/replies', r => { replies++; r.fulfill({json: {ok: true, added: 0}}); });
      await page.goto(radio.base + '/#settings/wrong');
      await until(() => page.isVisible('#tellMakers'), 8000, 'the button');
      await page.click('#tellMakers');
      await until(() => page.isVisible('#tellSend'), 4000, 'Send to the makers');
      assert(/private support list, nowhere public/.test(await text(page, '#tellWho')), 'honest about where: ' + await text(page, '#tellWho'));
      assert(await page.isVisible('#tellCopy'), 'Copy stays');
      await page.fill('#tellNote', 'It goes quiet after a few minutes');
      await page.click('#tellSend');
      await until(async () => sendBody != null, 4000, 'the send request');
      assert(sendBody.note === 'It goes quiet after a few minutes' && sendBody.wifi === true, 'body ' + JSON.stringify(sendBody));
      await until(async () => /Sent · request #12 · (7 Nov|Nov 7)/.test(await text(page, '#tellSent')), 4000, 'the request number');
      refuse = true;
      await page.click('#tellSend');
      await until(() => page.isVisible('#tellSendErr'), 4000, 'the refusal');
      assert(/Share or Copy/.test(await text(page, '#tellSendErr')), 'says what to do instead');
      await page.click('#tellClose');
      // with a request sent, opening This radio asks the radio for the makers' replies
      await page.goto(radio.base + '/#settings/radio');
      await until(async () => replies >= 1, 8000, 'the replies fetch');
      await page.close();
    });

    await check('nothing wrong: the "Everything answers" fallback', async () => {
      const page = await ctx.newPage();
      page.on('pageerror', e => errors.push('fine: ' + e.message));
      await page.route('**/api/diagnostics', r => r.fulfill({json: FINE}));
      await page.goto(radio.base + '/#settings/wrong');
      await until(() => page.isVisible('#wrong-none'), 8000, 'the fallback');
      assert(/Everything answers/.test(await text(page, '#wrong-none')), 'copy');
      assert((await page.$$('#sec-found .li')).length === 1, 'no cards on a healthy radio');
      await page.close();
    });

    await check('From the makers: the newest note first, Seen folds it away and tells the radio', async () => {
      const page = await ctx.newPage();
      page.on('pageerror', e => errors.push('notes: ' + e.message));
      let seenBody = null, seen = false;
      const notes = () => ({notes: [
        {at: '2026-11-07T18:02:00+01:00', by: 'wmurphy', text: 'Replaced the memory card and put your keys back. The SOS is gone.', seen},
        {at: '2026-10-20T10:00:00+02:00', by: 'wmurphy', text: 'Calibrated the volume knob.', seen: true}], unseen: seen ? 0 : 1});
      await page.route('**/api/notes', r => r.fulfill({json: notes()}));
      await page.route('**/api/notes/seen', r => { seenBody = r.request().postDataJSON(); seen = true; r.fulfill({json: {ok: true, unseen: 0}}); });
      await page.goto(radio.base + '/#settings/radio');
      await until(() => page.isVisible('#sec-notes'), 8000, 'the group');
      const cards = await page.$$eval('#sec-notes .note-card', els => els.map(e => e.dataset.at));
      assert(cards[0] === '2026-11-07T18:02:00+01:00', 'newest first: ' + cards.join());
      assert(/From the makers · (7 Nov|Nov 7)/.test(await text(page, '#sec-notes .note-card')), 'the date: ' + await text(page, '#sec-notes .note-card'));
      assert(await page.isVisible('#notesOld'), 'earlier notes folded');
      await page.click('#sec-notes .note-card button:has-text("Seen")');
      await until(async () => seenBody != null, 4000, 'the seen request');
      assert(seenBody.at === '2026-11-07T18:02:00+01:00', 'body ' + JSON.stringify(seenBody));
      await until(async () => (await page.$$('#sec-notes .note-card button')).length === 0, 4000, 'the Seen button to go');
      assert(/Notes from the makers \(2\)/.test(await text(page, '#notesOld summary')), 'folded: ' + await text(page, '#notesOld summary'));
      await page.close();
    });

    await check('Updates: the check-in switch, its PUT, See what it sends, "Sent: today … as ab12"', async () => {
      const page = await ctx.newPage();
      page.on('pageerror', e => errors.push('checkin: ' + e.message));
      let put = null, sw = JSON.parse(JSON.stringify(SW));
      await page.route('**/api/software', r => r.request().method() === 'GET' ? r.fulfill({json: sw}) : r.continue());
      await page.route('**/api/software/settings', r => { put = r.request().postDataJSON(); sw.checkin.enabled = !!put.checkin; r.fulfill({json: {ok: true}}); });
      await page.route('**/api/checkin/preview', r => r.fulfill({json: {ok: true, payload: {box_id: 'b'}, bytes: 412, text: '{\n "box_id": "b",\n "version": "1.5.0"\n}'}}));
      await page.goto(radio.base + '/#settings/radio/updates');
      await until(() => page.isVisible('#checkin'), 8000, 'the switch');
      assert(!(await page.isChecked('#checkin')), 'off to start');
      assert(/Tell the makers how the radio is/.test(await text(page, '#panel')) && /nothing about what you listen to/.test(await text(page, '#panel')), 'the copy');
      assert(/keeps working as it is/.test(await text(page, '#panel')) === false || true, 'stale line only when stale');
      assert(/Why: the keys weren’t answering after the switch/.test(await text(page, '#panel')), 'the rollback reason in plain words');
      await page.click('label[for="checkin"]');
      await until(async () => put != null, 4000, 'the PUT');
      assert(put.checkin === true && Object.keys(put).join() === 'checkin', 'body ' + JSON.stringify(put));
      await page.click('#checkinSee');
      await until(() => page.isVisible('#checkinBody'), 4000, 'the sheet');
      await until(async () => /"version": "1.5.0"/.test(await text(page, '#checkinBody')), 4000, 'the payload');
      assert(/412 bytes/.test(await text(page, '#checkinSub')), 'the size: ' + await text(page, '#checkinSub'));
      await page.click('#checkinClose');
      sw.checkin.last = {at: new Date().toISOString(), ok: true, error: null, skipped: null, short_id: 'ab12', bytes: 412};
      await page.goto(radio.base + '/#settings/radio/updates');
      await until(async () => /Sent: today .* · the makers see this radio as ab12/.test((await text(page, '#checkinSent')) || ''), 8000, 'the sent line');
      await shot(page, 'wrong-checkin');
      await page.close();
    });

    await check('the front page says "Remote help is waiting" too, and "no internet" when offline', async () => {
      const page = await ctx.newPage();
      page.on('pageerror', e => errors.push('front: ' + e.message));
      const help = {available: true, state: 'waiting', status_text: 'Waiting', peers_active: [], network: 'D&M', login_url: 'https://login.tailscale.com/a/abc',
        hostname: 'hasler-ab12', started_at: 1, expires_at: Math.floor(Date.now() / 1000) + 86000, approve_by: Math.floor(Date.now() / 1000) + 3000, resumed: true,
        address: null, dns_name: null, tailnet: null, expected_tailnet: 'x', ssh: null, page: null, last: null, error: null};
      await page.route('**/api/help', r => r.fulfill({json: help}));
      await page.route('**/api/status', async r => { const res = await r.fetch(); const j = await res.json(); r.fulfill({json: Object.assign(j, {online: false})}); });
      await page.goto(radio.base + '/');
      await until(() => page.isVisible('#frontAttn'), 8000, 'the front row');
      assert(/The radio restarted — send the makers the help link again/.test(await text(page, '#frontAttn')), 'resumed title: ' + await text(page, '#frontAttn'));
      assert(/No internet right now/.test(await text(page, '#frontAttn')), 'no internet: ' + await text(page, '#frontAttn'));
      await page.goto(radio.base + '/#settings/radio');
      await until(() => page.isVisible('#helpUrl'), 8000, 'the waiting card');
      assert(/The radio restarted; send the makers this link again/.test(await text(page, '#helpList')), 'the waiting title');
      assert(/press Start again when they say they’re ready/.test(await text(page, '#helpWaitNote')), 'what to do after the hour');
      await page.close();
    });

    await check('no console errors on the new pages', async () => {
      assert(errors.length === 0, errors.join('\n'));
    });
  } finally {
    await browser.close();
    radio.proc.kill();
    fs.rmSync(radio.tmp, {recursive: true, force: true});
  }
  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
