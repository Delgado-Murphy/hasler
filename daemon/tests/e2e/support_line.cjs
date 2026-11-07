#!/usr/bin/env node
// The owner's support line end to end, against the fake radio (haslerd --fake) in Chrome, with
// /api/support stubbed as the radio would answer after talking to the makers' Worker. Write to the
// makers: with nothing sent, the page IS the composer (a real field, no box that only looks like one);
// the sheet is the words and Send, and closes into the request; offline it waits ("Saved on the radio")
// and can go now; a maker's reply shows as a note, in the thread (a little safe Markdown), in Needs
// attention and as the gear's dot; reading it clears the dot; states Sent · Answered · Seen · Done; the
// owner writes back in the thread; New request opens the composer above the list; nothing of a thread
// sits outside its card. Help from the makers: writing leads, remote help is the quieter row. Also: the
// setup network's password is shown on This radio, no Show needed.
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
    back: (n, text) => { const r = s.requests.find(x => x.number === n); r.replies.push({at: '2026-11-08T10:00:00+01:00', text, state: null, seen: true, mine: true}); if (r.state !== 'done') r.state = 'sent'; else r.state = 'sent'; },
    queue: (title) => { s.pending.push({id: 'p1', at: AT, title, error: null}); },
    flush: () => { const p = s.pending.shift(); if (p) api.sent(13, p.title); return p ? 1 : 0; },
    raw: s,
  };
  return api;
}
// Route a page to the stubbed support line; returns the counters.
async function wire(page, sup, opts = {}){
  const hits = {send: [], seen: 0, replies: 0, retry: 0, back: []};
  await page.route('**/api/support/reply', r => {
    const body = r.request().postDataJSON(); hits.back.push(body);
    if (opts.refuseBack) return r.fulfill({status: 503, json: {ok: false, error: 'The radio can’t reach the internet just now. Your words are still here; send them once it’s back.'}});
    sup.back(body.number, body.note); r.fulfill({json: {ok: true, requests: sup.doc().requests}});
  });
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
    // Nothing of a request's thread outside its card (a live report: "From the makers" and "You" showed above the page).
    const inCards = page => page.$$eval('#reqList .req-card', cards => cards.every(c => {
      const b = c.getBoundingClientRect();
      return [...c.querySelectorAll('.req-msg, .req-who, .req-new, .req-state')].every(e => { const r = e.getBoundingClientRect();
        return r.width === 0 || (r.left >= b.left - 1 && r.right <= b.right + 1 && r.top >= b.top - 1 && r.bottom <= b.bottom + 1); });
    }) && [...document.querySelectorAll('.req-msg, .req-who')].every(e => e.closest('.req-card')));
    // No box that only looks like a field: a list with nothing to press and no title has no border.
    const noFakeFields = page => page.$$eval('#panel .list', ls => ls.filter(l => !l.querySelector('button, a, input, textarea, select, .li-title, pre, .skel')
      && parseFloat(getComputedStyle(l).borderTopWidth) > 0).map(l => l.textContent.slice(0, 60)));

    await check('nothing sent yet: Write to the makers is the composer itself, a real labelled field, no fake one', async () => {
      const page = await page0('empty');
      await wire(page, sup);
      await page.goto(radio.base + '/#settings/radio');
      await until(() => page.isVisible('#setrow-requests'), 8000, 'the row under Help from the makers');
      assert(/^Write to the makers/.test(await text(page, '#setrow-requests .li-title')) && /Delgado & Murphy answer here/.test(await text(page, '#setrow-requests')),
        'row: ' + await text(page, '#setrow-requests'));
      const order = await page.$$eval('#sec-help .set-row', rs => rs.map(r => r.id));
      assert(order[0] === 'setrow-requests', 'writing leads: ' + order.join());
      assert(!(await page.$('#helpStart')) && !(await page.$('#sec-help .btn.primary')), 'no big remote-help button on This radio');
      await page.click('#setrow-requests');
      await until(() => page.isVisible('#reqNote'), 6000, 'the composer on the page');
      assert((await text(page, '#pageTitle')) === 'Write to the makers', 'title');
      assert((await page.$eval('#reqNote', e => e.tagName)) === 'TEXTAREA', 'a real field');
      assert((await text(page, 'label[for="reqNote"]')) === 'What’s going on?', 'labelled');
      assert(await page.$eval('#reqNote', e => parseFloat(getComputedStyle(e).fontSize)) >= 16, '16 px');
      assert(await page.isVisible('#reqSend') && (await text(page, '#reqSend')) === 'Send to the makers', 'Send right there');
      assert((await text(page, '#reqWho')) === 'Goes to Delgado & Murphy only. They’ll answer here.', 'one line: ' + await text(page, '#reqWho'));
      assert(!(await page.$('#req-none')) && !(await page.$('#reqNew')), 'no empty-state box and separate button');
      assert(!(await page.$('#sec-write .grp-lbl')), 'no heading repeating the title');
      const fake = await noFakeFields(page); assert(!fake.length, 'boxes that look like fields: ' + fake.join(' | '));
      const h = await page.$eval('#reqSend', e => e.getBoundingClientRect().height); assert(h >= 44, '44 px target: ' + h);
      await shot(page, 'support-empty');
      await page.close();
    });

    await check('the sheet: the words and Send; sent, it closes into the request on Write to the makers', async () => {
      const page = await page0('send');
      const hits = await wire(page, sup);
      await page.goto(radio.base + '/#settings/wrong');
      await until(() => page.isVisible('#tellMakers'), 8000, 'the button');
      await page.click('#tellMakers');
      await until(() => page.isVisible('#tellSend'), 4000, 'Send');
      assert((await text(page, '#dlgTitle')) === 'Write to the makers', 'one verb');
      assert((await page.$$('#dlg .btn.primary')).length === 1 && (await page.$$('#dlg .dlg-acts .btn')).length === 2, 'Cancel and Send, nothing more');
      await page.fill('#tellNote', 'The keys stopped\nafter the storm');
      await page.click('#tellSend');
      await until(() => page.isVisible('#reqJust'), 6000, 'the request on its page');
      assert(!(await page.isVisible('#dlg')), 'no separate confirmation');
      assert(hits.send[0].note === 'The keys stopped\nafter the storm', 'body ' + JSON.stringify(hits.send[0]));
      assert(/Sent · request #12\. They’ll answer here\./.test(await text(page, '#reqJust')), 'line: ' + await text(page, '#reqJust'));
      assert(/^You · (7 Nov|Nov 7)The keys stopped$/.test(await text(page, '#req-12 .req-msg.mine')), 'card: ' + await text(page, '#req-12'));
      assert((await text(page, '#reqState-12')) === 'Sent', 'state');
      assert(/No answer yet/.test(await text(page, '#req-12')), 'what to expect');
      assert(await page.isVisible('#reqNew'), 'New request at the top');
      assert(await inCards(page), 'the thread inside its card');
      await shot(page, 'support-sent');
      await page.close();
    });

    await check('a maker’s reply: a note, Needs attention, the gear’s dot, the thread (Answered); reading it clears the dot', async () => {
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
      assert(/New reply from the makers/.test(await text(page, '#setrow-requests')), 'the row says so');
      await page.click('#setrow-requests');
      await until(() => page.isVisible('#req-12 .req-msg.theirs'), 6000, 'the thread');
      assert(/^Delgado & Murphy · (8 Nov|Nov 8)New/.test(await text(page, '#req-12 .req-msg.theirs')) && /ribbon cable/.test(await text(page, '#req-12 .req-thread')), 'thread: ' + await text(page, '#req-12 .req-thread'));
      assert((await text(page, '#reqState-12')) === 'Answered', 'a reply moves it to Answered: ' + await text(page, '#reqState-12'));
      const badge = await page.$eval('#req-12 .req-new', e => ({bg: getComputedStyle(e).backgroundColor, tag: e.tagName}));
      assert(badge.tag === 'SPAN' && !/\(\s*(1[2-9]\d|2\d\d),\s*(1[5-9]\d|2\d\d),\s*(1[2-9]\d|2\d\d)/.test(badge.bg), 'a quiet badge, no green: ' + JSON.stringify(badge));
      await until(async () => hits.seen >= 1, 4000, 'read: the notes marked seen');
      assert(await inCards(page), 'the thread inside its card, after open');
      // a refresh tick (the page asks again when it comes back into view; every minute while it's open)
      const before = hits.replies;
      await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
      await until(async () => hits.replies > before, 4000, 'replies asked again');
      await sleep(300);
      assert(await inCards(page), 'the thread inside its card, after a refresh');
      assert(await page.isVisible('#req-12 .req-new'), '"New" stays until the owner leaves');
      assert(/Checked just now/.test(await text(page, '#reqChecked')), 'checked quietly');
      await shot(page, 'support-thread');
      await page.goto(radio.base + '/');
      await until(async () => await page.$eval('#gearDot', e => e.hidden), 8000, 'the dot gone');
      await page.close();
    });

    await check('the makers’ words: a little Markdown, safely (no HTML, links only https and mailto)', async () => {
      // Rendered as the page renders a reply: each case one reply in a request's thread.
      const CASES = {
        live: 'Hello!! **how are you** doing?\n### Hope all is good\nClick [here](httpss://litio.net) ❤️❤️❤️',
        list: 'Try:\n- unplug it\n- wait `10 s`\n\n1. one\n2. two\n*soft*',
        xss: '<script>alert(1)</script> <img src=x onerror=alert(1)> [x](javascript:alert(1)) [y](JAVASCRIPT:alert(1)) [z](data:text/html,hi)',
        nest: '[a [b]](https://evil.example) [**bold link**](https://ok.example/a?b=1&c=2) https://bare.example/x. mailto [m](mailto:hola@example.org)',
        attr: '[q](https://ok.example/"onmouseover="alert(1))',
        // a real /owner reply in GitHub's Markdown (indentation kept by the Worker and the radio)
        rich: '> On Nov 3 you wrote:\n> The radio stops after an hour\n\n## What we found\n\nThanks! Fixed in __1.7.4__, _not_ ~~1.7.3~~. Try:\n\n' +
          '```\nsudo systemctl restart haslerd\n  --verbose **not bold**\n```\n\n- [x] update radio\n- [ ] check wifi\n  - 2.4 GHz only\n    3. deep\n- done\n\n' +
          '3. third\n4. fourth\n\n---\n\nSee https://en.wikipedia.org/wiki/Radio_(disambiguation). or <https://ok.example/x> or [docs](https://ok.example/d "Docs").\n' +
          '![shot](https://ok.example/s.png) \\*not italic\\* snake_case_name `a    b` | a | b |',
      };
      const md = makeSupport();
      md.sent(30, 'Markdown');
      Object.values(CASES).forEach(t => md.reply(30, t));
      const page = await page0('md');
      let alerted = false; page.on('dialog', d => { alerted = true; d.dismiss(); });
      await wire(page, md);
      await page.goto(radio.base + '/#settings/radio/requests');
      await until(() => page.isVisible('#req-30 .req-msg.theirs'), 8000, 'the replies');
      const got = await page.$$eval('#req-30 .req-msg.theirs .md', els => els.map(el => ({html: el.innerHTML, text: (c => { c.querySelectorAll('.sr').forEach(x => x.remove()); return c.textContent; })(el.cloneNode(true)),
        sr: [...el.querySelectorAll('a')].every(a => a.lastChild.className === 'sr' && a.lastChild.textContent === ' (opens in a new tab)'),
        links: [...el.querySelectorAll('a')].map(a => [a.getAttribute('href'), a.target, a.rel])})));
      const out = Object.fromEntries(Object.keys(CASES).map((k, i) => [k, got[i]]));
      assert(!alerted && !(await page.$('#req-30 script, #req-30 img')), 'nothing ran');
      assert(/<strong>how are you<\/strong>/.test(out.live.html) && /<p class="md-h"><strong>Hope all is good<\/strong><\/p>/.test(out.live.html), 'bold, heading: ' + out.live.html);
      assert(out.live.links.length === 0 && /\[here\]\(httpss:\/\/litio\.net\) ❤️❤️❤️/.test(out.live.text), 'a bad link stays plain text: ' + out.live.html);
      assert(/<ul><li>unplug it<\/li><li>wait <code>10 s<\/code><\/li><\/ul>/.test(out.list.html) && /<ol><li>one<\/li><li>two<\/li><\/ol>/.test(out.list.html) && /<em>soft<\/em>/.test(out.list.html), 'lists: ' + out.list.html);
      assert(!/<script|<img|onerror=|javascript:|data:text/i.test(out.xss.html.replace(/&lt;[^&]*&gt;/g, '').replace(/\[[xyz]\]\([^)]*\)/g, '')) && out.xss.links.length === 0, 'no HTML, no script links: ' + out.xss.html);
      assert(/&lt;script&gt;/.test(out.xss.html), 'shown as text: ' + out.xss.html);
      assert(JSON.stringify(out.nest.links) === JSON.stringify([['https://evil.example/', '_blank', 'noopener noreferrer'], ['https://ok.example/a?b=1&c=2', '_blank', 'noopener noreferrer'],
        ['https://bare.example/x', '_blank', 'noopener noreferrer'], ['mailto:hola@example.org', '_blank', 'noopener noreferrer']]), 'links: ' + JSON.stringify(out.nest.links) + ' ' + out.nest.html);
      // nested brackets: no disguised link; the address inside shows as itself
      assert(/\[a \[b\]\]\(https:\/\/evil\.example\)/.test(out.nest.text) && /<a [^>]*>https:\/\/evil\.example<span class="sr"> \(opens in a new tab\)<\/span><\/a>/.test(out.nest.html), 'nested: ' + out.nest.html);
      assert(Object.values(out).every(o => o.sr), 'every link says it opens a new tab, to a screen reader');
      assert(out.attr.links.every(l => !/"/.test(l[0]) || /%22/.test(l[0])) && !/<a [^>]*\sonmouseover=/.test(out.attr.html), 'no attribute breakout: ' + out.attr.html);
      // the rich reply: quote, heading, __bold__ _em_ ~~del~~, a fence verbatim, tasks, nested lists, a start number, hr, links
      const r = out.rich.html;
      const has = (re, what) => assert(re.test(r), what + ': ' + r);
      has(/^<blockquote><p>On Nov 3 you wrote:<br>The radio stops after an hour<\/p><\/blockquote><p class="md-h"><strong>What we found<\/strong><\/p>/, 'quote, then heading');
      has(/<strong>1\.7\.4<\/strong>, <em>not<\/em> <del>1\.7\.3<\/del>/, 'bold, em, del');
      has(/<pre><code>sudo systemctl restart haslerd\n  --verbose \*\*not bold\*\*<\/code><\/pre>/, 'fence verbatim, indent kept');
      has(/<li class="md-task"><input type="checkbox" disabled="" aria-label="Done"> update radio<\/li>/, 'done task');
      has(/<li class="md-task"><input type="checkbox" disabled="" aria-label="To do"> check wifi<ul><li>2\.4 GHz only<ol start="3"><li>deep<\/li><\/ol><\/li><\/ul><\/li><li>done<\/li><\/ul>/, 'nested lists');
      has(/<ol start="3"><li>third<\/li><li>fourth<\/li><\/ol><hr>/, 'start number, hr');
      const ticks = await page.$$eval('#req-30 .md-task input', els => els.map(e => e.checked));
      assert(JSON.stringify(ticks) === '[true,false]', 'ticked as written: ' + ticks);
      assert(JSON.stringify(out.rich.links.map(l => l[0])) === JSON.stringify(['https://en.wikipedia.org/wiki/Radio_(disambiguation)', 'https://ok.example/x', 'https://ok.example/d', 'https://ok.example/s.png']),
        'links: ' + JSON.stringify(out.rich.links));
      has(/>Picture: shot<span class="sr">/, 'an image is a link, never loaded');
      has(/<\/a>\. or /, 'the full stop after a bare URL stays outside it');
      assert(/ \*not italic\* snake_case_name a {4}b \| a \| b \|$/.test(out.rich.text) && /<code>a {4}b<\/code>/.test(r), 'escapes, snake_case, code spaces, a table as text: ' + out.rich.text);
      await page.close();
    });

    await check('the owner writes back in the thread: Sent again, marked You; a failure keeps the words', async () => {
      const page = await page0('back');
      const hits = await wire(page, sup, {refuseBack: true});
      await page.goto(radio.base + '/#settings/radio/requests');
      await until(() => page.isVisible('#reqBackOpen-12'), 8000, 'Write back');
      await page.click('#reqBackOpen-12');
      await until(() => page.isVisible('#reqBack-12'), 4000, 'the box');
      assert((await page.getAttribute('#reqBack-12', 'placeholder')) === 'Write back…', 'placeholder');
      assert(await page.isDisabled('#reqBackSend-12'), 'nothing to send yet');
      await page.fill('#reqBack-12', 'Pushed it in.\nStill quiet.');
      await page.click('#reqBackSend-12');
      await until(() => page.isVisible('#reqBackErr-12'), 4000, 'the refusal');
      assert(/still here/.test(await text(page, '#reqBackErr-12')) && (await page.inputValue('#reqBack-12')) === 'Pushed it in.\nStill quiet.', 'the words kept');
      await page.unroute('**/api/support/reply');
      await page.route('**/api/support/reply', r => { const b = r.request().postDataJSON(); hits.back.push(b); sup.back(b.number, b.note); r.fulfill({json: {ok: true, requests: sup.doc().requests}}); });
      await page.click('#reqBackSend-12');
      await until(async () => !(await page.$('#reqBack-12')), 4000, 'the box to close');
      assert(hits.back.at(-1).number === 12 && hits.back.at(-1).note === 'Pushed it in.\nStill quiet.', 'body ' + JSON.stringify(hits.back.at(-1)));
      const msgs = await page.$$eval('#req-12 .req-msg', ls => ls.map(l => l.className.replace('req-msg ', '') + ':' + l.textContent));
      assert(/^mine:You/.test(msgs.at(-1)) && /Pushed it in\./.test(msgs.at(-1)), 'in order, marked You: ' + msgs.join(' | '));
      assert((await text(page, '#reqState-12')) === 'Sent', 'Sent again');
      assert(await inCards(page), 'inside its card after a change');
      await page.close();
    });

    await check('closed is "Done" with the makers’ words in order; reopened is "Seen"', async () => {
      sup.reply(12, 'Your request #12 is done. Glad it plays again.', 'done');
      const page = await page0('done');
      const hits = await wire(page, sup);
      await page.goto(radio.base + '/#settings/radio/requests');
      await until(async () => (await text(page, '#reqState-12')) === 'Done', 8000, 'done');
      const thread = await page.$$eval('#req-12 .req-msg:not(.mine)', ls => ls.map(l => l.textContent));
      assert(thread.length === 2 && /ribbon/.test(thread[0]) && /is done/.test(thread[1]), 'in order: ' + thread.join(' | '));
      assert(await inCards(page), 'inside its card');
      sup.reply(12, 'The makers reopened your request #12.', 'seen');
      await page.reload();
      const seen0 = hits.seen;
      await until(async () => (await text(page, '#reqState-12')) === 'Seen', 8000, 'reopened');
      await until(async () => hits.seen > seen0, 6000, 'read');
      await page.goto(radio.base + '/#settings/wrong');
      await until(() => page.isVisible('#wrongReqs'), 8000, 'Your requests from Something’s wrong?');
      await until(async () => /Request #12 · Seen/.test(await text(page, '#wrongReqs')), 6000, 'the row: ' + await text(page, '#wrongReqs'));
      await page.close();
    });

    await check('offline: the request waits on the radio, says so, and Send now sends it; New request writes inline', async () => {
      const page = await page0('offline');
      const hits = await wire(page, sup, {offline: true});
      await page.goto(radio.base + '/#settings/radio/requests');
      await until(() => page.isVisible('#reqNew'), 8000, 'New request');
      await page.click('#reqNew');
      await until(() => page.isVisible('#reqNote'), 4000, 'the composer, above the list');
      assert((await text(page, '#sech-write')) === 'New request' && await page.isVisible('#reqCancel'), 'with Cancel');
      const y = await page.$eval('#reqNote', e => e.getBoundingClientRect().top), ly = await page.$eval('#reqList', e => e.getBoundingClientRect().top);
      assert(y < ly, 'above the list');
      await page.fill('#reqNote', 'Silence again');
      await page.click('#reqSend');
      await until(async () => /Saved on the radio/.test(await text(page, '#reqJust')), 4000, 'saved');
      assert(/goes by itself once the internet is back/.test(await text(page, '#reqJust')), 'says when');
      assert(!(await page.$('#reqNote')), 'the composer folds away');
      await until(() => page.isVisible('#wait-p1'), 6000, 'Waiting to send');
      assert(/Silence again/.test(await text(page, '#wait-p1')) && /goes by itself once the radio is online/.test(await text(page, '#wait-p1')), 'card: ' + await text(page, '#wait-p1'));
      await page.click('#wait-p1 button:has-text("Send now")');
      await until(async () => hits.retry === 1 && await page.isVisible('#req-13'), 6000, 'sent now');
      assert(!(await page.$('#wait-p1')), 'no longer waiting');
      await page.close();
    });

    await check('remote help: its own quiet row and page; the key press and How it works live there', async () => {
      const page = await page0('help');
      const help = {available: true, state: 'off', status_text: '', peers_active: [], network: 'D&M', login_url: null, last: null, error: null};
      await page.route('**/api/help', r => r.fulfill({json: help}));
      await page.goto(radio.base + '/#settings/radio');
      await until(async () => /Off · for 24 hours, with a key press/.test(await text(page, '#setrow-help')), 8000, 'the row: ' + await text(page, '#setrow-help'));
      assert((await text(page, '#setrow-help .li-title')) === 'Let them look from afar', 'its name');
      await page.click('#setrow-help');
      await until(() => page.isVisible('#helpStart'), 6000, 'the page');
      assert((await text(page, '#pageTitle')) === 'Remote help', 'title');
      assert(/Nothing starts until you press a key on the radio/.test(await text(page, '#helpWhat')), 'the key press');
      assert(await page.isVisible('#sec-help a:has-text("How it works")') && /Write to the makers/.test(await text(page, '#helpWrite')), 'How it works, and the everyday way');
      assert(!(await page.$('#helpTell')), 'one action per idea');
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
