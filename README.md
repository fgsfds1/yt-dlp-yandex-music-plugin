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
* Requires a logged-in (paid) account — see
  [Session (cookies)](#session-cookies). Without a valid session the API
  silently returns `smart_preview` streams — downloads "succeed" but
  files are ~13–30 s.

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
pip install --user .
# if your yt-dlp is a pipx install:  pipx inject yt-dlp <repo>
# (not `pipx/uv tool install .` — that creates an isolated venv where no
#  yt-dlp runs, so the plugin would never be discovered)
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

## Session (cookies)

The plugin talks to the unofficial web API with **your** Yandex session.
Two ways to provide it:

**From your browser directly (no export):**

```sh
yt-dlp --cookies-from-browser <browser>[:<profile-dir>] ...
# e.g. for the Helium browser (KDE):
yt-dlp --cookies-from-browser chromium:~/.config/net.imput.helium/Default ...
```

The browser name must match where the browser stores its cookie-
encryption key: Helium registers it as *Chromium Safe Storage*, so use
`chromium`, not `chrome`. Needs the keychain/KWallet to be reachable
(i.e. a running desktop session — for headless/cron use the exported
file instead).

**Exported cookie file:**

```sh
yt-dlp --cookies /path/to/cookies.txt ...
```

Netscape format, **tab-separated** (export with a browser extension like
"Get cookies.txt LOCALLY"; hand-pasted space-separated lines are silently
dropped by the parser). Must include the `.yandex.ru` `Session_id`
cookie — it covers `api.music.yandex.ru` automatically.

**The silent failure mode:** with a stale/missing session the API
returns `smart_preview` streams for *every* quality level — no error,
just ~13–30 s files. Verify results with
`ffprobe -v error -show_entries format=duration file`.

## Usage

```sh
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

## CI/CD & releases

### Pipeline

| Workflow | Trigger | What it does |
|---|---|---|
| `.github/workflows/tests.yml` | every push / PR to `master` | **test** job: Python 3.10 + 3.14 matrix — installs the latest yt-dlp and the plugin, runs `tests/test_sign.py` and `tools/check_install.py`. **build** job: builds sdist + wheel (packaging sanity check), uploads a `dist` artifact. |
| `.github/workflows/publish.yml` | GitHub **Release published**, or manual dispatch (Actions → *Publish to PyPI* → *Run workflow*; optional `tag` input, defaults to the latest tag on the branch) | **build** job: resolves the version (release tag → dispatch input → latest tag), validates the CalVer format *and* that it is a real calendar date, injects it into `pyproject.toml`, builds sdist + wheel, uploads the artifact. **publish** job: uploads to PyPI (see below). |

### How the PyPI upload is authenticated — Trusted Publishing (OIDC)

No API token is stored anywhere. PyPI is configured to trust *this repo's
`publish.yml` workflow running in the `pypi` environment*; at publish
time GitHub presents a short-lived OIDC token, PyPI verifies it and mints
a 15-minute API token that is used for the upload. The workflow declares
`environment: pypi` + `permissions: id-token: write`, and
`skip-existing: true` makes re-runs idempotent (PyPI versions are
immutable, so re-uploading the same version would otherwise fail).

One-time setup (done 2026-09-16):

* GitHub → repo **Settings → Environments** → environment **`pypi`**
* PyPI → account → **Publishing** → *pending* GitHub publisher: owner
  `fgsfds1`, repo `yt-dlp-yandex-music-plugin`, workflow `publish.yml`,
  environment `pypi`, project name `yt-dlp-yandex-music` (the project was
  created automatically on first publish)

If the repo is ever moved/renamed, or the workflow file is renamed,
re-register the publisher at PyPI → account → Publishing.

### Release process

1. Commit the changes to `master` (wait for `Tests` to go green).
2. Tag the commit with the CalVer version (no `v` prefix):

   ```sh
   git tag 2026.09.16 && git push origin 2026.09.16
   ```

3. Create and **publish** a GitHub Release for that tag (the *published*
   event is the trigger):

   ```sh
   gh release create 2026.09.16 --title "2026.09.16" --notes "..."
   ```

   or GitHub UI: **Releases → Create a new release → pick tag → notes →
   Publish release**.
4. Watch Actions → **Publish to PyPI**.
5. Verify: <https://pypi.org/project/yt-dlp-yandex-music/> shows the new
   version, and in a fresh venv:

   ```sh
   python3 -m venv /tmp/ymtest && /tmp/ymtest/bin/pip install yt-dlp-yandex-music
   /tmp/ymtest/bin/python tools/check_install.py   # both lines OK
   ```

### Version edge cases

* **Same-day fix:** tag `2026.09.16.1` — a new release, not a re-upload.
* **Pre-release:** tag `2026.09.16rc1` and tick *Set as pre-release* on
  the GitHub Release. Note that `pip install` skips pre-releases unless
  given `--pre`.
* **Broken release:** PyPI versions are immutable — cut a new version
  (`.1` suffix or a new date). You *can* delete a version as the project
  owner (PyPI → project → release → delete), but a new version is cleaner.
* **Re-run / re-publish:** Actions → *Publish to PyPI* → *Run workflow*
  (optionally with a `tag` input), or re-run the release run —
  `skip-existing` makes this idempotent.
* **What CI does not test:** live downloads need Yandex session cookies,
  which are not a CI secret. `tests/test_sign.py` pins the signing
  algorithm against captured live vectors, and the key-rotation path is
  smoke-tested manually (see QUICKSTART troubleshooting).

## Tests

```sh
python3 tests/test_sign.py
```

Verifies the signing algorithm against signatures captured from live web
app traffic (2026-08-17), including the codecs-without-commas gotcha.
`tools/check_install.py` verifies an installed plugin. Both run in CI on
every push/PR — see [CI/CD & releases](#cicd--releases).

## Layout

```
QUICKSTART.md                             3-step how-to (start here)
yt_dlp_plugins/extractor/yandex_music_v2.py   the plugin
.github/workflows/tests.yml                 CI: tests + packaging build
.github/workflows/publish.yml               CI: CalVer tag → PyPI (OIDC)
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
