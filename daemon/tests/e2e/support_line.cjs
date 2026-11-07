#!/usr/bin/env node
// The owner's support line end to end (1.6.x), against the fake radio (haslerd --fake) in Chrome, with
// /api/support stubbed as the radio would answer after talking to the makers' Worker: Send to the
// makers leads the sheet and confirms with the number; offline it waits ("Saved on the radio") and
// Your requests can send it now; a maker's reply shows as a note, in the request's thread, in Needs
// attention and as the gear's dot; reading it clears the dot; closed is "Done", reopened "Seen by the
// makers"; Your requests is reachable from Help from the makers and Something's wrong?. Also: the setup
// network's password is shown on This radio, no Show needed.
//
//   cd daemon && node tests/e2e/support_line.cjs
//
// Same needs as settings_ia.cjs: PLAYWRIGHT=/path/to/node_modules/playwright, PW_CHANNEL=chrome, HASLER_PY.
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
const text = async (page, sel) => (await page.textContent(sel)) || '';

const AT = '2026-11-07T18:00:00+01:00';
// The radio's side of the makers' Worker, as GET /api/support says it: one request, its thread, the outbox.
function makeSupport(){
  const s = {available: true, requests: [], pending: [], unseen_replies: 0};
  const api = {
    doc: () => JSON.parse(JSON.stringify(s)),
    sent: (n, title) => { s.requests.unshift({number: n, at: AT, deduped: false, state: 'sent', title, replies: [], unseen: 0}); },
    reply: (n, text, state) => {
      const r = s.requests.find(x => x.number === n);
      r.replies.push({at: '2026-11-08T09:00:00Z', text, state: state || null, seen: false});
      if (state) r.state = state;
      r.unseen = r.replies.filter(x => !x.seen).length;
      s.unseen_replies = s.requests.reduce((a, x) => a + x.unseen, 0);
    },
    seen: () => { s.requests.forEach(r => { r.replies.forEach(x => { x.seen = true; }); r.unseen = 0; }); s.unseen_replies = 0; },
    queue: (title) => { s.pending.push({id: 'p1', at: AT, title, error: null}); },
    flush: () => { const p = s.pending.shift(); if (p) api.sent(13, p.title); return p ? 1 : 0; },
    raw: s,
  };
  return api;
}
// Route a page to the stubbed support line; returns the counters.
async function wire(page, sup, opts = {}){
  const hits = {send: [], seen: 0, replies: 0, retry: 0};
  await page.route('**/api/support', r => r.fulfill({json: sup.doc()}));
  await page.route('**/api/support/replies', r => { hits.replies++; r.fulfill({json: {ok: true, added: 0}}); });
  await page.route('**/api/support/send', r => {
    const body = r.request().postDataJSON(); hits.send.push(body);
    if (opts.offline){ sup.queue(body.note.split('\n')[0]); return r.fulfill({json: {ok: true, queued: true, pending: sup.doc().pending}}); }
    sup.sent(12, body.note.split('\n')[0]);
    r.fulfill({json: {ok: true, number: 12, at: AT, deduped: false, requests: sup.doc().requests}});
  });
  await page.route('**/api/support/retry', r => { hits.retry++; const n = sup.flush(); r.fulfill({json: {ok: true, sent: n, pending: sup.doc().pending}}); });
  await page.route('**/api/notes/seen', r => { hits.seen++; sup.seen(); r.fulfill({json: {ok: true, unseen: 0}}); });
  await page.route('**/api/notes', r => {
    const notes = [];
    sup.raw.requests.forEach(q => q.replies.forEach(x => notes.push({at: x.at, by: 'makers', text: x.text, seen: x.seen})));
    notes.reverse();
    r.fulfill({json: {notes, unseen: notes.filter(n => !n.seen).length}});
  });
  return hits;
}

(async () => {
  const radio = await startRadio();
  const browser = await launch();
  const errors = [];
  const ctx = await browser.newContext({viewport: {width: 390, height: 844}, deviceScaleFactor: 2});
  const page0 = async (name) => { const p = await ctx.newPage(); p.on('pageerror', e => errors.push(name + ': ' + e.message)); return p; };
  try {
    await check('the fake radio itself never offers the real server', async () => {
      const d = await (await fetch(radio.base + '/api/support')).json();
      assert(d.available === false && Array.isArray(d.pending) && d.unseen_replies === 0, JSON.stringify(d));
    });

    const sup = makeSupport();
    await check('Send to the makers leads the sheet, says what happens, and confirms with the number', async () => {
      const page = await page0('send');
      const hits = await wire(page, sup);
      await page.goto(radio.base + '/#settings/radio');
      await until(() => page.isVisible('#setrow-requests'), 8000, 'Your requests under Help from the makers');
      assert(/Nothing sent yet/.test(await text(page, '#setrow-requests')), 'row: ' + await text(page, '#setrow-requests'));
      await page.goto(radio.base + '/#settings/wrong');
      await until(() => page.isVisible('#tellMakers'), 8000, 'the button');
      await page.click('#tellMakers');
      await until(() => page.isVisible('#tellSend'), 4000, 'Send');
      assert(/This opens a request with Delgado & Murphy\. You’ll see their answer here/.test(await text(page, '#tellSub')), 'the words');
      await page.fill('#tellNote', 'The keys stopped\nafter the storm');
      await page.click('#tellSend');
      await until(async () => /Sent\. Your request is #12\./.test(await text(page, '#tellDoneTitle')), 4000, 'the number');
      assert(hits.send[0].note === 'The keys stopped\nafter the storm', 'body ' + JSON.stringify(hits.send[0]));
      await shot(page, 'support-sent');
      await page.click('#tellSeeReqs');
      await until(() => page.isVisible('#req-12'), 6000, 'the request on Your requests');
      assert(/Request #12 · (7 Nov|Nov 7)/.test(await text(page, '#req-12')) && /“The keys stopped”/.test(await text(page, '#req-12')), 'card: ' + await text(page, '#req-12'));
      assert((await text(page, '#reqState-12')) === 'Sent', 'state');
      assert(/No answer yet/.test(await text(page, '#req-12')), 'what to expect');
      await page.close();
    });

    await check('a maker’s reply: a note, Needs attention, the gear’s dot, the thread; reading it clears the dot', async () => {
      sup.reply(12, 'Is the ribbon cable at the back pushed in?');
      const page = await page0('reply');
      const hits = await wire(page, sup);
      await page.goto(radio.base + '/');
      await until(async () => !(await page.$eval('#gearDot', e => e.hidden)), 8000, 'the dot on the gear');
      await page.goto(radio.base + '/#settings');
      await until(() => page.isVisible('#setrow-attn-replies'), 8000, 'Needs attention');
      assert(/New reply from the makers/.test(await text(page, '#setrow-attn-replies')) && /About request #12: The keys stopped/.test(await text(page, '#setrow-attn-replies')),
        'row: ' + await text(page, '#setrow-attn-replies'));
      await page.goto(radio.base + '/#settings/radio');
      await until(() => page.isVisible('#sec-notes'), 8000, 'From the makers');
      assert(/ribbon cable/.test(await text(page, '#sec-notes')), 'the reply as a note');
      assert(/New reply from the makers/.test(await text(page, '#setrow-requests')), 'the requests row says so');
      await page.click('#setrow-requests');
      await until(() => page.isVisible('#req-12 .req-thread'), 6000, 'the thread');
      assert(/From the makers · (8 Nov|Nov 8)New/.test(await text(page, '#req-12 .req-reply')) && /ribbon cable/.test(await text(page, '#req-12 .req-thread')), 'thread: ' + await text(page, '#req-12 .req-thread'));
      await until(async () => hits.seen >= 1, 4000, 'read: the notes marked seen');
      await shot(page, 'support-thread');
      await page.goto(radio.base + '/');
      await until(async () => await page.$eval('#gearDot', e => e.hidden), 8000, 'the dot gone');
      await page.close();
    });

    await check('closed is "Done" with the makers’ words in order; reopened is "Seen by the makers"', async () => {
      sup.reply(12, 'Your request #12 is done. Glad it plays again.', 'done');
      const page = await page0('done');
      await wire(page, sup);
      await page.goto(radio.base + '/#settings/radio/requests');
      await until(async () => (await text(page, '#reqState-12')) === 'Done', 8000, 'done');
      const thread = await page.$$eval('#req-12 .req-reply', ls => ls.map(l => l.textContent));
      assert(thread.length === 2 && /ribbon/.test(thread[0]) && /is done/.test(thread[1]), 'in order: ' + thread.join(' | '));
      sup.reply(12, 'The makers reopened your request #12.', 'seen');
      await page.reload();
      await until(async () => (await text(page, '#reqState-12')) === 'Seen by the makers', 8000, 'reopened');
      await page.goto(radio.base + '/#settings/wrong');
      await until(() => page.isVisible('#wrongReqs'), 8000, 'Your requests from Something’s wrong?');
      await until(async () => /Seen by the makers · request #12/.test(await text(page, '#wrongReqs')), 6000, 'the row, once read: ' + await text(page, '#wrongReqs'));
      await page.close();
    });

    await check('offline: the request waits on the radio, says so, and Send now sends it', async () => {
      const page = await page0('offline');
      const hits = await wire(page, sup, {offline: true});
      await page.goto(radio.base + '/#settings/wrong');
      await until(() => page.isVisible('#tellMakers'), 8000, 'the button');
      await page.click('#tellMakers');
      await until(() => page.isVisible('#tellSend'), 4000, 'Send');
      await page.fill('#tellNote', 'Silence again');
      await page.click('#tellSend');
      await until(async () => /Saved on the radio/.test(await text(page, '#tellDone')), 4000, 'saved');
      assert(/sends this by itself as soon as it can/.test(await text(page, '#tellDone')), 'says when');
      await page.click('#tellSeeReqs');
      await until(() => page.isVisible('#wait-p1'), 6000, 'Waiting to send');
      assert(/Silence again/.test(await text(page, '#wait-p1')) && /sends it by itself once it’s online/.test(await text(page, '#wait-p1')), 'card: ' + await text(page, '#wait-p1'));
      await page.click('#wait-p1 button:has-text("Send now")');
      await until(async () => hits.retry === 1 && await page.isVisible('#req-13'), 6000, 'sent now');
      assert(!(await page.$('#wait-p1')), 'no longer waiting');
      await page.close();
    });

    await check('This radio shows the setup network’s password as it is, with Copy', async () => {
      const page = await page0('setup');
      await page.route('**/api/wifi', r => r.request().method() === 'GET'
        ? r.fulfill({json: {hotspot: false, current: 'Casa', networks: [], saved: [], setup: {ssid: 'Hasler-ab12', password_set: true, password: 'abcd-efgh-jkmn'}}}) : r.continue());
      await page.goto(radio.base + '/#settings/radio');
      await until(() => page.isVisible('#setupPw'), 8000, 'the row');
      assert((await text(page, '#setupPw')) === 'abcd-efgh-jkmn', 'shown: ' + await text(page, '#setupPw'));
      assert(!(await page.$('#setupPwShow')) && await page.isVisible('#setupPwCopy'), 'no Show, a Copy');
      await page.close();
    });

    await check('no page errors', async () => { assert(errors.length === 0, errors.join('\n')); });
  } finally {
    await browser.close();
    radio.proc.kill();
    fs.rmSync(radio.tmp, {recursive: true, force: true});
  }
  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
