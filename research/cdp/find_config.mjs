#!/usr/bin/env node
// find_config.mjs — locate a config object (e.g. player.secretKey) inside a
// page's React runtime state when it is NOT present in the HTML or in any
// API response (it only exists in React hook state / memoized deps).
//
// Usage:
//   node find_config.mjs <url> <cookies-file> [needle]
//   needle defaults to "secretKey"
//
// Walks the React fiber tree from #root, scanning memoizedProps and the
// memoizedState chains (hook states, incl. useMemo deps) for an object
// whose `player` property is an object containing a string `secretKey`.

import { spawn } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';

const CHROME = process.env.CHROME || '/usr/bin/chromium';
const [URL, COOKIES_FILE, NEEDLE = 'secretKey'] = process.argv.slice(2);
if (!URL || !COOKIES_FILE) {
  console.error('usage: node find_config.mjs <url> <cookies-file> [needle]');
  process.exit(2);
}
const PORT = 9398;
const PROFILE = '/tmp/ym_cdp_profile_cfg';

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

const FIND_EXPR = `(() => {
  const out = { found: [] };
  const isCfg = (o) => o && typeof o === 'object' && o.player && typeof o.player === 'object'
    && typeof o.player.${NEEDLE} === 'string';
  const seen = new Set();
  const scan = (obj, path, depth) => {
    if (!obj || typeof obj !== 'object' || depth > 6 || out.found.length > 2) return;
    if (seen.has(obj)) return;
    seen.add(obj);
    if (isCfg(obj)) {
      out.found.push({ path, key: obj.player.${NEEDLE},
        playerKeys: Object.keys(obj.player).slice(0, 40) });
      return;
    }
    for (const k of Object.keys(obj)) {
      try {
        const v = obj[k];
        if (v && typeof v === 'object') scan(v, path + '.' + k, depth + 1);
      } catch {}
    }
  };
  const root = document.querySelector('#root') || document.body;
  const fiberKey = Object.keys(root).find(k =>
    k.startsWith('__reactContainer$') || k.startsWith('__reactFiber$'));
  if (fiberKey) {
    const queue = [root[fiberKey]];
    let count = 0;
    while (queue.length && count < 30000 && out.found.length < 3) {
      const node = queue.pop();
      count++;
      if (!node || typeof node !== 'object') continue;
      try {
        if (node.memoizedProps) scan(node.memoizedProps, 'props', 0);
        let hs = node.memoizedState, hd = 0;
        while (hs && hd < 30) { scan(hs.memoizedState, 'hook' + hd, 0); hs = hs.next; hd++; }
      } catch {}
      if (node.child) queue.push(node.child);
      if (node.sibling) queue.push(node.sibling);
    }
    out.fibersScanned = count;
  }
  return JSON.stringify(out);
})()`;

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
await send('Page.navigate', { url: URL });
await sleep(12000);

const probe = await send('Runtime.evaluate', {
  expression: FIND_EXPR, returnByValue: true });
console.error('CONFIG FOUND:', probe.result.value);
writeFileSync('/tmp/ym_cfg_found.json', probe.result.value || 'null');
chrome.kill();
process.exit(0);
