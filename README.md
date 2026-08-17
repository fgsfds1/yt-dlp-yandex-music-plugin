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
* Requires a logged-in account: pass your cookies with `--cookies`.

## Install

**Option A — drop-in (no build):**

```sh
mkdir -p ~/.config/yt-dlp/plugins/yandex-music-v2/yt_dlp_plugins/extractor
cp yt_dlp_plugins/extractor/yandex_music_v2.py \
   ~/.config/yt-dlp/plugins/yandex-music-v2/yt_dlp_plugins/extractor/
```

**Option B — pip package** (installs into site-packages, which yt-dlp
scans for `yt_dlp_plugins.extractor` automatically):

```sh
pip install --user .        # or: pipx/uv tool install .
```

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
when Yandex ships a new frontend. Re-extract and update:

```sh
python3 tools/extract_secret_key.py --check   # prints key, compares with plugin
# if mismatched: update _SECRET_KEY in
#   yt_dlp_plugins/extractor/yandex_music_v2.py
```

## Tests

```sh
python3 tests/test_sign.py
```

Verifies the signing algorithm against signatures captured from live web
app traffic (2026-08-17), including the codecs-without-commas gotcha.

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
(`7tvSmFbyf5hJnIHhCimDDD` as of v4.1520.1). The gotcha: codecs are joined
**without** commas for signing (commas in the URL). With
`quality=lossless&codecs=flac,mp3&transports=raw` the server returns
unencrypted `strm.yandex.net` URLs (MP3 320 / FLAC) that download without
cookies. Full details: `research/api-notes.md`.

## Legal / ToS note

This is for downloading content you are entitled to access (your own
subscriptions, e.g. personal playlist archiving). Respect Yandex Music's
Terms of Service and applicable law; the built-in extractor's own
documentation already assumes cookie-based personal use.

## License

MIT — see `LICENSE`.
