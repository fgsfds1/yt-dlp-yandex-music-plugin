# Yandex Music Web API — Reverse Engineering Notes

Investigation date: 2026-08-17. Frontend version: `music/v4.1520.1`
(`https://yastatic-net.ru/s3/music-frontend-static/music/v4.1520.1/`).
Target: downloading tracks from `music.yandex.ru` (shared playlist
`/playlists/<uuid>`) with a logged-in account.

## TL;DR

```
sign = base64(HMAC-SHA256(secretKey, data)).rstrip("=")
data = ts + trackId + quality + codecs.join("") + transport
secretKey = "7tvSmFbyf5hJnIHhCimDDD"   # hardcoded in the frontend bundle
```

`ts` is `Math.floor(Date.now()/1000)`. **Gotcha:** for signing, the codec
list is joined with an **empty string**; the request URL uses **commas**.
Getting this wrong yields HTTP 403 `{"name":"track-download-info-error",
"message":"not-allowed"}`.

## Why the built-in yt-dlp extractor is broken

`yt_dlp/extractor/yandexmusic.py` (all versions up to 2026.7.4, including
GitHub master) calls:

```
GET https://music.yandex.{tld}/handlers/{ep}.jsx?...
```

Those endpoints are gone — they now return an HTML 404 page
("This page is no longer available..."), which crashes the extractor with
`TypeError: argument of type 'bool' is not a container or iterable` in
`_download_webpage_handle`. The live web app no longer uses `handlers/`
at all; it uses `https://api.music.yandex.ru` (see below).

## Endpoints used by the live web app

Base: `https://api.music.yandex.ru`

Common request headers (all of them matter; missing `x-yandex-music-client`
or `X-Requested-With` etc. can yield 403):

```
User-Agent:            <browser UA>
Accept:                application/json
Origin:                https://music.yandex.ru
Referer:               https://music.yandex.ru/
X-Requested-With:      XMLHttpRequest
X-Retpath-Y:           https://music.yandex.ru/<current page path>
x-request-id:          <random UUIDv4>
x-yandex-music-client: YandexMusicWebNext/1.0.0
x-yandex-music-multi-auth-user-id: <uid>     (only when multi-auth is on)
x-yandex-music-without-invocation-info: 1
accept-language:       en
Cookie:                <logged-in session cookies>
```

### 1. Track metadata — `POST /tracks`

Multipart form data with one or more `trackIds` fields, each value either
`<trackId>` or `<trackId>:<albumId>` (bare id works fine):

```
POST /tracks
Content-Type: multipart/form-data; boundary=...

--b
Content-Disposition: form-data; name="trackIds"

143540328
--b
Content-Disposition: form-data; name="trackIds"

2219121
--b--
```

Response: JSON array of track objects: `id`, `title`, `durationMs`,
`available`, `artists[]` (each with `name`), `album` (`id`, `title`),
`numberInSet`, ...

### 2. Stream/download URL — `GET /get-file-info`

```
GET /get-file-info?ts=<unix ts>&trackId=<id>&quality=<q>
    &codecs=flac%2Caac%2Che-aac%2Cmp3%2Cflac-mp4%2Caac-mp4%2Che-aac-mp4
    &transports=encraw&sign=<base64 hmac, '=' stripped>
```

Response (200):

```json
{
  "downloadInfo": {
    "trackId": "143540328",
    "quality": "nq",
    "codec": "aac-mp4",
    "bitrate": 192,
    "transport": "encraw",
    "key": "eed9f6a6a9e6cc06abc34b043957f0f0",
    "urls": ["https://strm-...strm.yandex.net/music-v2/crypt/..."],
    "url": "https://strm-...strm.yandex.net/music-v2/crypt/..."
  }
}
```

* `transport=encraw` → URL path contains `/crypt/`, media is encrypted,
  the response carries a per-request `key` (hex). The web player
  decrypts in the browser.
* `transport=raw` → URL path contains `/raw/`, media is **unencrypted**,
  no `key`. The URL is self-contained (signed `ysign1=...` parameter) and
  can be fetched with **no cookies at all**. This is what the plugin uses.
* The server picks the best codec from the `codecs` list for the requested
  `quality`. The web player's default list is
  `flac,aac,he-aac,mp3,flac-mp4,aac-mp4,he-aac-mp4`; requesting
  `flac,mp3` yields FLAC where available, else MP3 320.
* Media URLs are on `strm-<region>-<n>.strm.yandex.net` and are
  time-limited (the `ts=...` segment is a hex timestamp).

### 3. Batch variant — `GET /get-file-info/batch`

Same parameters, but `trackIds=<id1,id2,...>` (comma-joined in the URL;
also comma-joined-without-commas for signing — i.e. the same string as the
URL value). Used by the player for preloading.

### 4. Playlist by owner — `GET /users/<uid>/playlists/<kind>`

```
GET /users/1024547490/playlists/1004?resumeStream=false&richTracks=false
```

Returns the playlist object: `owner`, `playlistUuid`, `title`,
`trackCount`, `tracks[]` (with `richTracks=false` each entry is just
`{originalIndex, id, albumId}`), `pager`, ...

`<kind>` is a numeric playlist type (1004 = user-created public playlist).

### 5. Shared playlist URL — `https://music.yandex.ru/playlists/<uuid>`

There is **no** direct API taking the UUID. The page embeds everything in
the Next.js RSC payload (`self.__next_f.push([1, "..."])` strings in the
HTML). After unescaping the push strings, the payload contains:

```json
"preloadedPlaylistByUuid": {
  "owner": {"uid": 1024547490, "login": "AskFlynn101", ...},
  "playlistUuid": "b938363e-...",
  "kind": 1004,
  "title": "...",
  "trackCount": 20,
  "tracks": [
    {"originalIndex": 0, "id": 143540328, "albumId": 39688866},
    ...
  ]
}
```

So: fetch the page (cookies needed for private playlists; public ones
render without login but the app still expects a session), parse the RSC
payload, then use endpoints 1–4.

## The signing key

* The web player signs `get-file-info` requests with
  `HMAC-SHA256` via WebCrypto (`crypto.subtle`), module `79713` in chunk
  `3254-*.js`:

  ```js
  i = async t => {
    let {secretKey: e, data: r} = t, a = new TextEncoder, i = a.encode(e);
    return crypto.subtle.importKey("raw", i, {name: "HMAC", hash: {name: "SHA-256"}}, true, ["sign", "verify"])
      .then(async t => {
        let e = a.encode(r);
        return crypto.subtle.sign("HMAC", t, e)
          .then(t => btoa(String.fromCharCode(...new Uint8Array(t))).slice(0, -1))
      })
  }
  ```

  (base64 with the trailing `=` padding removed — 43 chars for SHA-256.)

* The key is **not** per-session and not in any API response. It is
  hardcoded in the frontend bundle:

  ```js
  // chunk 8290-*.js, webpack module 25079
  25079:(e,t,a)=>{"use strict";function i(){return"7tvSmFbyf5hJnIHhCimDDD"}
              a.d(t,{E:()=>i}),...}
  ```

  consumed by the config module (chunk `5616-*.js`, module 35616):

  ```js
  player: { overembed: false, secretKey: (0, a.E)(),
            externalDomain: "next.music.yandex.ru", prefixUrl: <api base> }
  ```

* It only changes when Yandex ships a new frontend. If signatures start
  failing with 403 `not-allowed`, run `tools/extract_secret_key.py` and
  update `_SECRET_KEY` in the plugin.

## Quality / codec behavior observed

| requested `quality` | codecs asked  | result (Plus account)      |
|---------------------|---------------|----------------------------|
| `lossless`          | `flac,mp3`    | MP3 320 (FLAC not offered for these tracks) |
| `lossless`          | `flac`        | MP3 320 (server ignores, falls back) |
| `nq`                | `flac,aac,he-aac,mp3,flac-mp4,aac-mp4,he-aac-mp4` | AAC-mp4 192 |
| `nq`                | `mp3`         | MP3 192                    |

Web app "stream quality" setting → API quality:
`high_quality→lossless`, `balanced→nq`, `efficient→lq`, `preview→preview`.

Valid `quality` values (2026-09-16): `lossless`, `nq`, `lq` — anything else
(e.g. `hi`, `q`) → HTTP 400 `illegal-argument`.

### Determining available qualities (2026-09-16)

* **No metadata field:** track (`POST /tracks`), album (`GET /albums/<id>`)
  and search (`GET /search?text=…&type=track&page=0`) objects carry no
  quality/codec/lossless flag (all keys checked).
* **Only way: probe `get-file-info`** — the served `downloadInfo`
  (`quality`/`codec`/`bitrate`) is the availability indicator:
  request `lossless` + `codecs=flac,mp3` → server serves the best it has
  (flac if available, else mp3 320, else mp3 192).
* **Batch variant works and is cheap:** `GET /get-file-info/batch` with
  comma-joined `trackIds` (signed over the same comma-joined string),
  ~20 ids per request → 1500 tracks ≈ 75 requests.
* **Account-level finding:** ~1700 tracks probed (search results for 8
  major artists + a shared playlist + the full 1496-track favorites
  library) → **1536× mp3/320k, 4× mp3/192k, 0× flac**. No FLAC badge or
  FLAC setting exists in the web UI either → FLAC is simply not offered
  for this account/tier; "High" quality = MP3 320. Some tracks only have
  192k (the `nq` fallback covers those).

AAC/MP4 (`aac-mp4`, `he-aac-mp4`) is **not** streamable by BASS — for the
NFSMW use case only `mp3` (≥192) / `flac` / `ogg` / `wav` are acceptable,
hence `codecs=flac,mp3` + `transport=raw`.

## How the sign data format was found (methodology)

1. CDP (Chrome DevTools Protocol) against headless Chromium, cookies
   injected via `Network.setCookies`.
2. Hooked `fetch`/`XMLHttpRequest.open` in the main world
   (`Page.addScriptToEvaluateOnNewDocument`) → captured the exact
   `get-file-info` URLs with `sign`.
3. Hooked `crypto.subtle.importKey`/`sign` in main world + all workers
   (with `Target.setAutoAttach` + `waitForDebuggerOnStart`) → **no calls
   at all**, even though the code clearly uses WebCrypto. Dead end — the
   player's crypto calls happen in a context the hook couldn't see (the
   signing module runs before/around worker bootstrap; also a red herring
   since the key/data still had to pass through `TextEncoder`).
4. **The winning hook:** `TextEncoder.prototype.encode` in the main world.
   The signer encodes both the key and the data string, so the log showed
   exactly:

   ```
   {"len":22, "s":"7tvSmFbyf5hJnIHhCimDDD"}
   {"len":68, "s":"1786984334138802868nqflacaache-aacmp3flac-mp4aac-mp4he-aac-mp4encraw"}
   ```

   i.e. key + `ts trackId quality codecs-without-commas transport`.
5. Cross-checked computed HMAC against the captured `sign` → exact match.
6. Verified end-to-end: fresh `ts` + computed `sign` + the header set
   above → 200 OK, and the `raw` transport URL downloads with no cookies.

Scripts: `research/cdp/` (Node.js, no dependencies, Node ≥ 22 for the
global `WebSocket`).

## Artist / album / liked-playlist endpoints (2026-09-23)

### Artist tracks — `GET /artists/<id>/tracks?page=<n>` (paginated!)

The artist page's "all tracks" list. **This is the one genuinely
paginated endpoint** of the plugin:

* `perPage` is **fixed at 20** — the `perPage` query parameter is
  ignored by the server (verified: `?perPage=100` still returns 20).
* Loop `page=0,1,2,…` until `len(collected) >= pager.total` or a page
  comes back empty. `pager = {page, perPage: 20, total}`.
* Track objects are *rich* (unlike playlist entries): `id`, `title`,
  `durationMs`, `available`, `artists[]`, `albums[]` (each with `id`,
  `title`, `year`, …) — but the plugin only needs `id` + first
  `albums[0].id` to build the track URL.
* `GET /artists/<id>` (artist info) does **not** 404 for a nonexistent
  artist — it returns 200 with `artist.error: "not-found"`. The tracks
  endpoint returns 200 with `pager.total: 0` and no `tracks` key.
* `artist.counts.tracks` (e.g. 273 for AC/DC) counts *all* appearances
  incl. featured ("alsoAlbums"); the tracks endpoint lists only the
  primary tracks (127 for AC/DC) — that's what the web app's all-tracks
  view shows, and what the extractor returns.
* No duplicates observed in the full list (0 dupes for 127-track
  AC/DC); the extractor dedupes by track id anyway.

### Album pages — preloaded in the RSC payload (no API needed)

`GET /albums/<id>` returns album metadata **without** tracks, and there
is no `/albums/<id>/tracks` endpoint (404). The album page embeds the
full track list in the Next.js RSC payload as `"preloadedAlbum":{…}`:

* regular albums: flat `"tracks": [{"id": …}, …]`
* large/multi-disc albums (e.g. the 149-track *The Discovery Boxset*):
  no `tracks` key — instead `"volumes": [[{"id": …}, …], …]` (one
  array per disc). Flatten in order.
* `"pager": {"page": 0, "perPage": <N>, "total": <N>}` — in all cases
  observed `perPage == total`, i.e. the preload is complete (verified
  with 58- and 149-track albums). The extractor warns if
  `len(ids) < trackCount`.

### Liked playlist — `https://music.yandex.ru/playlists/lk.<uuid>`

Same page-preload mechanism as shared playlists
(`"preloadedPlaylistByUuid":{…}`), with `kind: 3` and a
user-specific `lk.`-prefixed uuid. The preload contains the **complete**
list (verified: `trackCount` 1515 == `len(tracks)` 1515; the pager's
`total` 1521 counts original indices incl. since-deleted slots, hence
the last `originalIndex` 1520 > 1514).

Fallback if a preload ever comes back short: the users API
`GET /users/<uid>/playlists/<kind>` (uid/kind from the preloaded
`owner.uid`/`kind`) — it **ignores `page`/`perPage` and always returns
the full playlist in one response** (verified with 1500+ tracks), so no
API-side pagination is needed there either.

## Silent preview mode & how the plugin detects it (2026-09-23)

With a stale/missing session, `get-file-info` answers **200 OK for every
quality level** but serves `smart_preview` streams (~13–30 s, 192 kbps
MP3) — the response's `quality` field reports `smart_preview` (verified
cookieless: requested `lossless`/`nq` → served `smart_preview`).

Detection in the track extractor (`_warn_if_preview_served`), cheapest
signal first:

1. **`quality` field** — a served `preview`/`smart_preview` warns
   immediately. Note the field does *not* blindly echo the request:
   with a valid session, requesting `lq` serves `quality: nq` (the
   server normalizes upward), so the check matches preview levels,
   not "anything != requested".
2. **Duration estimate** — HEAD the stream URL, take `Content-Length`,
   `est_ms = bytes * 8 / bitrate` (served bitrate from the response;
   exact for CBR MP3, close for FLAC). Warn when the estimate is under
   50% of the track's `durationMs`. Verified: full Firesuite @192k =
   6,626,742 B → 276.1 s vs. metadata 276.06 s; its smart_preview =
   333,047 B → 13.9 s.

Both paths are best-effort (any HEAD failure is swallowed) and only
`report_warning` — a preview track still downloads, just flagged.

## Other observations

* `account/about` → `hasPlus: true` for the test account; lossless/MP3-320
  availability is Plus-gated.
* The app also maintains a `rotor/session/.../clone` websocket-ish session
  and a 766 KB blob-worker (the "yasp" stream player) — neither is needed
  for downloading.
* `get-file-info` responses embed `invocationInfo` (hostname, req-id) —
  handy for support tickets; the `x-yandex-music-without-invocation-info: 1`
  header suppresses it.
* The old `music.yandex.ru/handlers/*.jsx` endpoints (used by yt-dlp) are
  fully retired, not just deprecated.
