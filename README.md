# yt-dlp-yandex-music-plugin

A **yt-dlp plugin** (plus standalone tools and research notes) for
downloading from **Yandex Music** (`music.yandex.ru`) in 2026.

The built-in yt-dlp Yandex Music extractor is broken upstream: it calls
the retired `handlers/*.jsx` endpoints and crashes. This project
reverse-engineered the current web API and replaces the extractor.

**Just want to download something? → [`QUICKSTART.md`](QUICKSTART.md)**
(3 steps: install, cookies, one command.)

## What it does

* Fixes `https://music.yandex.ru/album/<albumId>/track/<trackId>`
  (shadows the broken built-in `YandexMusicTrackIE`).
* Adds support for **shared playlists**:
  `https://music.yandex.ru/playlists/<uuid>` (not supported by upstream).
* Downloads the best available audio: FLAC where the server offers it,
  otherwise MP3 320 kbps (falls back to MP3 192). Unencrypted `raw`
  transport — no client-side decryption.
* Requires a logged-in (paid) account: pass your cookies with `--cookies`
  (Netscape format, tab-separated, must include the `.yandex.ru`
  `Session_id` cookie). Without a valid session the API silently returns
  `smart_preview` streams — downloads "succeed" but files are ~13–30 s.
  See the troubleshooting table in `QUICKSTART.md`.

## Install

**Option A — PyPI (recommended):**

```sh
pip install yt-dlp-yandex-music
```

If your yt-dlp lives in a **pipx** install, the plugin must go into the
same environment:

```sh
pipx inject yt-dlp yt-dlp-yandex-music
```

**Option B — from this repo:**

```sh
pip install --user .        # or: pipx/uv tool install .
```

**Option C — drop-in (no build, no pip):**

```sh
mkdir -p ~/.config/yt-dlp/plugins/yandex-music-v2/yt_dlp_plugins/extractor
cp yt_dlp_plugins/extractor/yandex_music_v2.py \
   ~/.config/yt-dlp/plugins/yandex-music-v2/yt_dlp_plugins/extractor/
```

All options install into a location yt-dlp scans for the
`yt_dlp_plugins.extractor` namespace automatically.

Verify (both lines should say OK):

```sh
python3 tools/check_install.py
# OK:  yandexmusic:track      -> plugin (shadows broken built-in)
# OK:  yandexmusicv2:playlist -> plugin (shared playlists)
```

Note: `yt-dlp --list-extractors` won't show the plugin — yt-dlp handles
that flag before loading plugins.

## Usage

```sh
# cookies: Netscape-format file exported from a logged-in browser session
yt-dlp --cookies ~/Music/NFSMW/.cookies.txt \
  -o '%(playlist_index)02d - %(artist)s - %(title)s.%(ext)s' \
  'https://music.yandex.ru/playlists/<uuid>'

# single track
yt-dlp --cookies cookies.txt \
  'https://music.yandex.ru/album/39688866/track/143540328'

# pin the request-signing key (normally not needed — the plugin
# refreshes it automatically when Yandex rotates it)
yt-dlp --cookies cookies.txt \
  --extractor-args "yandexmusicv2:hmac_key=7tvSmFbyf5hJnIHhCimDDD" \
  'https://music.yandex.ru/album/39688866/track/143540328'
```

Filenames containing non-ASCII (e.g. Cyrillic artist names) are **not**
transliterated by yt-dlp — post-process if you need ASCII-only names
(`tools/standalone_download.py` does it for you).

## Standalone downloader (no yt-dlp)

```sh
python3 tools/standalone_download.py \
  --cookies cookies.txt \
  --playlist 'https://music.yandex.ru/playlists/<uuid>' \
  --out ~/Music/NFSMW \
  --m3u /path/to/Playlist.m3u \
  --wine-prefix 'Z:\home\lw\Music'
```

Pure stdlib; writes `NN - Artist - Title.ext` files (ASCII-sanitized) and
optionally an M3U (plain paths or Wine `Z:\`-style paths).

## If signatures start failing (403 `not-allowed`)

The HMAC key is hardcoded in the web frontend bundle and only changes
when Yandex ships a new frontend. The plugin handles a rotation
automatically: on a rejected signature it re-downloads the frontend,
extracts the new key, caches it in the yt-dlp cache dir, and retries.

If the automatic refresh cannot find the key (frontend layout changed
again), extract it manually and pin it with an extractor-arg:

```sh
python3 tools/extract_secret_key.py            # prints the current key
yt-dlp --extractor-args "yandexmusicv2:hmac_key=<key>" ...
```

The `hmac_key` extractor-arg also works for testing/overrides and takes
priority over the auto-refresh. Accepted names: `yandexmusicv2`,
`yandexmusic`, `yandexmusic:track`, `yandexmusicv2:playlist`.

## Versioning

CalVer, in the style of yt-dlp itself: `YYYY.MM.DD` (e.g. `2026.09.16`),
with optional suffixes:

* `YYYY.MM.DD.N` — same-day re-release (e.g. `2026.09.16.1`)
* `YYYY.MM.DDrcN` — pre-release (e.g. `2026.09.16rc1`)

The version is **the git tag** — releases are tagged and published by
CI (`.github/workflows/publish.yml`), so there is no version field to
bump by hand. PyPI versions are immutable: one tag = one release, and a
broken release gets a new date, not a re-upload. Release notes live in
GitHub Releases.

## Publishing (maintainers)

1. Commit the changes to `master`.
2. Tag the commit with the CalVer version (no `v` prefix):
   `git tag 2026.09.16 && git push origin 2026.09.16`
3. On GitHub: **Releases → Create a new release** → pick the tag →
   write notes → **Publish release**. (Tick *pre-release* for `rcN` tags.)
4. The `Publish to PyPI` workflow builds sdist+wheel and uploads them via
   PyPI **Trusted Publishing** (OIDC — no API token in the repo).

One-time setup (do once, before the first release):

1. GitHub → repo **Settings → Environments → New environment: `pypi`**
2. PyPI → your account → **Publishing** → add a *pending* GitHub
   publisher: owner `fgsfds1`, repo `yt-dlp-yandex-music-plugin`,
   workflow `publish.yml`, environment `pypi`, project name
   `yt-dlp-yandex-music`. The project is created automatically on first
   publish (the name is not reserved until then).

If you ever move the repo or rename the workflow, re-register the
publisher at PyPI → account → Publishing.

## Tests

```sh
python3 tests/test_sign.py
```

Verifies the signing algorithm against signatures captured from live web
app traffic (2026-08-17), including the codecs-without-commas gotcha.

CI runs these on every push/PR (`.github/workflows/tests.yml`), on
Python 3.10–3.14, plus a packaging build check.

## Layout

```
QUICKSTART.md                             3-step how-to (start here)
yt_dlp_plugins/extractor/yandex_music_v2.py   the plugin
tools/check_install.py                      verify the plugin is installed
tools/extract_secret_key.py                   re-extract the HMAC key
tools/standalone_download.py                  yt-dlp-free downloader + M3U
tests/test_sign.py                            sign-algorithm regression tests
research/api-notes.md                         full API + RE documentation
research/cdp/                                 CDP capture scripts (Node ≥ 22)
```

## The one-paragraph summary of the RE

The web player signs `GET api.music.yandex.ru/get-file-info` requests with
`sign = base64(HMAC-SHA256(key, ts + trackId + quality + codecs.join("") + transport)).rstrip("=")`.
The key is a static app secret hardcoded in the frontend bundle
(`7tvSmFbyf5hJnIHhCimDDD` as of v4.1603.1, where it lives in a
per-platform `player.secretKey` config object). The gotcha: codecs are joined
**without** commas for signing (commas in the URL). With
`quality=lossless&codecs=flac,mp3&transports=raw` the server returns
unencrypted `strm.yandex.net` URLs (MP3 320 / FLAC) that download without
cookies. Full details: `research/api-notes.md`.

## Disclaimer

* **Vibecoded:** this plugin was written with heavy AI assistance
  ("vibecoded"). It works for its author, but the code is not audited,
  not reviewed, and not tested against every edge case. Use with
  appropriate skepticism.
* **Use at your own risk — no ban guarantee:** this plugin talks to
  Yandex Music's *unofficial* web API with your personal session
  cookies. Nothing here is endorsed by or affiliated with Yandex. The
  author cannot guarantee that using it will not lead to rate limits,
  CAPTCHAs, or (in the worst case) restrictions on your Yandex Music
  account. Keep request volumes reasonable, and if you value the account
  highly, think twice before hammering it.
* **Personal use:** this is for downloading content you are entitled to
  access (your own subscriptions, e.g. personal playlist archiving).
  Respect Yandex Music's Terms of Service and applicable law; the
  built-in extractor's own documentation already assumes cookie-based
  personal use.

## License

MIT — see `LICENSE`.
