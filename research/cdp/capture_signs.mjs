#!/usr/bin/env node
// capture_signs.mjs — capture the Yandex Music web player's signing key and
// sign data by hooking TextEncoder.prototype.encode (and fetch) in the page.
//
// This is the script that cracked the sign format: the signer encodes both
// the secret key and the data string with TextEncoder, so logging encodes
// reveals both.
//
// Usage:
//   node capture_signs.mjs <playlist-or-track-url> <cookies-file>
//
// Requirements: Node >= 22 (global WebSocket/fetch), /usr/bin/chromium or
// CHROME env var. Writes:
//   /tmp/ym_enc_log.json  — TextEncoder inputs (key + sign data strings)
//   /tmp/ym_reqlog.json   — get-file-info request URLs (with sign param)
//
// Then verify: the HMAC-SHA256 of each data string with the key must equal
// the `sign` query param of the matching request (see tests/test_sign.py).

import { spawn } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';

const CHROME = process.env.CHROME || '/usr/bin/chromium';
const [URL, COOKIES_FILE] = process.argv.slice(2);
if (!URL || !COOKIES_FILE) {
  console.error('usage: node capture_signs.mjs <url> <cookies-file>');
  process.exit(2);
}
const PORT = 9399;
const PROFILE = '/tmp/ym_cdp_profile_signs';

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

const cookies = [];
for (const line of readFileSync(COOKIES_FILE, 'utf8').split('\n')) {
  if (!line || line.startsWith('#')) continue;
  const parts = line.split('\t');
  if (parts.length < 7) continue;
  const [domain, , path, secure, expiry, name, value] = parts;
  cookies.push({ domain, path, secure: secure === 'TRUE', httpOnly: false, name, value,
    ...(expiry !== '0' ? { expires: parseInt(expiry, 10) } : {}) });
}

const HOOK_SOURCE = [
  'self.__encLog = [];',
  'const oe = TextEncoder.prototype.encode;',
  'TextEncoder.prototype.encode = function (s) {',
  '  try {',
  '    if (typeof s === "string" && s.length > 10 && /^[0-9a-zA-Z,\\-]+$/.test(s)) {',
  '      self.__encLog.push({ len: s.length, s: s.slice(0, 160) });',
  '    }',
  '  } catch {}',
  '  return oe.call(this, s);',
  '};',
  'self.__reqLog = [];',
  'const of = window.fetch;',
  'window.fetch = function (input, init) {',
  '  try {',
  '    const u = typeof input === "string" ? input : (input && input.url) || "";',
  '    if (u.indexOf("get-file-info") !== -1) self.__reqLog.push(u);',
  '  } catch {}',
  '  return of.apply(this, arguments);',
  '};',
].join('\n');

const chrome = spawn(CHROME, [
  '--headless=new', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
  `--user-data-dir=${PROFILE}`, `--remote-debugging-port=${PORT}`,
  '--window-size=1440,900', '--lang=ru-RU', 'about:blank',
], { stdio: ['ignore', 'pipe', 'pipe'] });
chrome.stderr.on('data', () => {});

let target;
for (let i = 0; i < 60; i++) {
  await sleep(500);
  try {
    const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
    target = list.find(t => t.type === 'page');
    if (target) break;
  } catch {}
}
if (!target) { console.error('no CDP target'); chrome.kill(); process.exit(1); }

const ws = new WebSocket(target.webSocketDebuggerUrl);
let id = 0;
const pending = new Map();
function send(method, params = {}) {
  return new Promise((resolve, reject) => {
    const mid = ++id;
    pending.set(mid, { resolve, reject });
    ws.send(JSON.stringify({ id: mid, method, params }));
  });
}
ws.onmessage = (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) {
    const { resolve, reject } = pending.get(msg.id);
    pending.delete(msg.id);
    msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
  }
};
await new Promise(r => ws.onopen = r);
await send('Page.enable');
await send('Runtime.enable');
await send('Network.setCookies', { cookies });
// install hooks before any page script runs
await send('Page.addScriptToEvaluateOnNewDocument', { source: HOOK_SOURCE });
await send('Page.navigate', { url: URL });
await sleep(12000);

// trigger playback so the player requests stream URLs
const click = await send('Runtime.evaluate', {
  expression: `(() => {
    const btns = [...document.querySelectorAll('button')];
    const playBtns = btns.filter(b => /play|воспроизв/i.test((b.getAttribute('aria-label')||'') + ' ' + (b.title||'')));
    if (playBtns[0]) { playBtns[0].click(); return 'clicked'; }
    return 'no play button found';
  })()`,
  returnByValue: true,
});
console.error('CLICK:', click.result.value);
await sleep(15000);

const enc = await send('Runtime.evaluate', {
  expression: `JSON.stringify(self.__encLog)`, returnByValue: true });
const reqs = await send('Runtime.evaluate', {
  expression: `JSON.stringify(self.__reqLog)`, returnByValue: true });
writeFileSync('/tmp/ym_enc_log.json', enc.result.value || '[]');
writeFileSync('/tmp/ym_reqlog.json', reqs.result.value || '[]');
console.error('ENC LOG:', enc.result.value);
console.error('REQS:', reqs.result.value);
chrome.kill();
process.exit(0);
