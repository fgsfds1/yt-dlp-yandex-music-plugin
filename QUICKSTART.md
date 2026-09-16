# Quickstart — download from Yandex Music in 3 steps

Goal: get audio files from `music.yandex.ru` with `yt-dlp`.
~2 minutes. Everything else in this repo is optional.

> Unofficial plugin, vibecoded with AI assistance, no ban guarantee —
> see the [Disclaimer](README.md#disclaimer) in the README.

## 0. What you need

* `yt-dlp` (any recent version — the plugin does the work, not yt-dlp)
* `ffmpeg` (only if you want to convert/re-mux)
* Cookies from a logged-in Yandex Music browser session. Two options:
  * **Native (no export needed):** `--cookies-from-browser` with a profile
    path, e.g. for the Helium browser (KDE, verified 2026-08):
    `yt-dlp --cookies-from-browser chromium:~/.config/net.imput.helium/Default ...`
    Helium stores its cookie-encryption key in KWallet as *"Chromium Safe
    Storage"*, so the browser name must be `chromium` (not `chrome`).
    Requires a running KDE session (D-Bus + `kwallet-query` from the
    `kwallet` package).
  * **Exported file:** Netscape-format `cookies.txt` (browser extension
    "Get cookies.txt LOCALLY" or similar). Must include the `.yandex.ru`
    session cookies (they cover `api.music.yandex.ru` automatically) — the
    critical one is `Session_id`. The file must be the extension's
    **tab-separated** export; hand-pasted space-separated lines are
    silently dropped by the parser (only a few cookies survive, and the
    session is lost).
* A paid Yandex Music subscription on that account (free/expired sessions
  only get preview streams — see troubleshooting).

## 1. Install the plugin (once)

**From PyPI (recommended):**

```sh
pip install yt-dlp-yandex-music
# if your yt-dlp is a pipx install:
pipx inject yt-dlp yt-dlp-yandex-music
```

**Or from the repo** (latest master, possibly newer than PyPI):

```sh
git clone <this repo> ~/project/yt-dlp-yandex-music-plugin
cd ~/project/yt-dlp-yandex-music-plugin
pip install --user .
```

Verify (both lines should say OK):

```sh
python3 tools/check_install.py   # from the repo checkout
# OK:  yandexmusic:track      -> plugin (shadows broken built-in)
# OK:  yandexmusicv2:playlist -> plugin (shared playlists)
```

(Not `yt-dlp --list-extractors` — yt-dlp processes that flag *before*
loading plugins, so plugin extractors never appear in its list.)

No-install alternative for a one-off run. Note: the flag is
`--plugin-dirs` (plural), and it scans the **subdirectories** of the given
dir for a `yt_dlp_plugins/` tree — so point it at the repo's *parent*:

```sh
yt-dlp --plugin-dirs <repo>/.. ...   # see step 2
```

## 2. Download

```sh
yt-dlp --cookies /path/to/cookies.txt \
  -o '%(playlist_index)02d - %(artist)s - %(title)s.%(ext)s' \
  --embed-metadata --embed-thumbnail \
  'https://music.yandex.ru/playlists/<uuid>'
```

That's it. Notes:

* **Metadata/cover art:** `--embed-metadata --embed-thumbnail` are
  optional yt-dlp flags (off by default) — they tag title, artist,
  album, album artist, track/disc numbers, year, and embed the 1000×1000
  cover
* **Single track:** `yt-dlp --cookies cookies.txt 'https://music.yandex.ru/album/<albumId>/track/<trackId>'`
  (bare `https://music.yandex.ru/track/<trackId>` works too)
* **Only some tracks:** add `-I 1-5` (first five), `-I 3` (one), `-I 10-20` (range)
* **Output dir:** add `-P /path/to/dir`
* You get the best quality the server offers: FLAC if available, else
  MP3 320 kbps (Plus account), unencrypted, no decryption step.

## 3. Check the result

```sh
ffprobe -v error -show_entries format=duration,bit_rate -of csv=p=0 file.mp3
# -> 173.06,320000   (seconds, kbps)
```

## Troubleshooting (30 seconds)

| Symptom | Fix |
|---|---|
| Download "succeeds" but the file is only ~13–30 s (e.g. 13.5 s of a 5-min track) | Stale/invalid session: the API returns `smart_preview` streams for **every** quality level, so there is no error. Re-export cookies from a logged-in browser (must include `.yandex.ru` `Session_id`). Verify with `ffprobe -v error -show_entries format=duration file`. |
| `HTTP 403 ... "not-allowed"` | The frontend signing key rotated. The plugin auto-refreshes it from the frontend on the next request. If it can't (layout change), run `python3 tools/extract_secret_key.py` and pass the key via `--extractor-args "yandexmusicv2:hmac_key=<key>"`. |
| `playlist data not found in page` | Playlist is private/deleted, or cookies are stale — re-export them. |
| `not available` for a track | Track is geo/subscription-blocked for your account. |
| Cyrillic in filenames | yt-dlp doesn't transliterate; use `tools/standalone_download.py` (does it, plus writes an M3U). |
| Sanity check before a big run | `python3 tests/test_sign.py` verifies the signing *algorithm* (catches code regressions). To check whether the *key* is still current, run `python3 tools/extract_secret_key.py --check`. |

## Going further

* `README.md` — install options, standalone downloader, project layout
* `research/api-notes.md` — the full reverse-engineering writeup
* `tools/` — key extractor + yt-dlp-free downloader
