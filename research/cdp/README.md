# CDP capture scripts

Chrome DevTools Protocol helpers (plain Node.js, no npm dependencies;
Node ≥ 22 for the global `WebSocket` and `fetch`). Chromium at
`/usr/bin/chromium` (override with `CHROME=/path/to/chromium`).

All scripts take a Netscape cookie file (browser export or yt-dlp format)
and inject it via `Network.setCookies` before navigating.

| script | purpose |
|--------|---------|
| `capture_signs.mjs <url> <cookies>` | The decisive one. Hooks `TextEncoder.prototype.encode` + `fetch` in the main world, clicks the play button, and logs (a) every string the page encodes — which includes the HMAC **key** and the **sign data** — and (b) the resulting `get-file-info` URLs with their `sign` param. Output: `/tmp/ym_enc_log.json`, `/tmp/ym_reqlog.json`. |
| `find_config.mjs <url> <cookies> [needle]` | Finds a config object (default: `player.secretKey`) in the React fiber tree (`#root` → `__reactContainer$`), scanning `memoizedProps` and hook `memoizedState` chains. Use when a value is absent from both the HTML and API responses. |

## What did NOT work (for the record)

* Hooking `crypto.subtle.importKey`/`sign` in the main world and in all
  workers (`Target.setAutoAttach` + `waitForDebuggerOnStart`, hooking
  before worker bootstrap): **zero calls captured**, even though the
  signing code path in the bundle clearly uses WebCrypto and the requests
  were provably made from the main world. The lesson: when a hook on the
  "obvious" API sees nothing, hook a lower-level primitive that the data
  must pass through (`TextEncoder` here).
* `JSON.parse` hook: the config/signing data never arrives as a JSON
  string (RSC payload is parsed by Next.js's own parser).
* The 766 KB blob worker (the "yasp" stream player) contains no signing
  code at all — fetched its source via `fetch(blobUrl)` from the page.
* No service worker is involved; no hidden player iframe.
