"""
yt-dlp plugin: Yandex Music (modern API, 2026).

Shadows the broken built-in ``YandexMusicTrackIE`` / ``YandexMusicAlbumIE`` /
``YandexMusicArtistTracksIE`` / ``YandexMusicPlaylistIE`` /
``YandexMusicArtistAlbumsIE`` (which rely on the deprecated
``handlers/*.jsx`` endpoints that now return 404) and adds support for:

* shared playlist URLs (``https://music.yandex.ru/playlists/<uuid>``)
* the user's "liked"/favorites playlist
  (``https://music.yandex.ru/playlists/lk.<uuid>``)
* charts (``https://music.yandex.ru/playlists/ch.<uuid>``)
* user-playlist URLs (``https://music.yandex.ru/users/<login>/playlists/<id>``)
* artist pages (``https://music.yandex.ru/artist/<id>``) — all of the
  artist's tracks, via the paginated ``/artists/<id>/tracks`` API
* artist album lists (``https://music.yandex.ru/artist/<id>/albums``) —
  via ``GET /artists/<id>/direct-albums``
* album pages (``https://music.yandex.ru/album/<id>``) — all of the
  album's tracks, from the page's preloaded data

Requires cookies of a logged-in Yandex Music account (``--cookies``).

How it works
------------
The current music.yandex.ru web app talks to ``api.music.yandex.ru``:

* Track metadata:  ``POST /tracks`` (multipart, repeated ``trackIds`` fields,
  values ``trackId`` or ``trackId:albumId``)
* Stream URLs:     ``GET /get-file-info?ts=&trackId=&quality=&codecs=&transports=&sign=``
* Playlist (by owner): ``GET /users/<uid>/playlists/<kind>``
* Artist tracks:   ``GET /artists/<id>/tracks?page=<n>`` (20 per page;
  the ``perPage`` parameter is ignored by the server)

Album and playlist pages (including the liked playlist) embed their full
track lists in the Next.js RSC payload (``self.__next_f.push`` strings);
for very large albums the list is split into per-volume
(``volumes``) instead of a flat ``tracks`` array.

The ``sign`` query parameter is an HMAC-SHA256 signature:

    sign = base64(HMAC-SHA256(secretKey, data)).rstrip("=")
    data = ts + trackId + quality + codecs.join("") + transport

Note the gotcha: for *signing* the codec list is joined with an empty
string, while the actual request URL uses commas.

The ``secretKey`` is an app-level secret hardcoded in the music-web
frontend bundle. It is stable across sessions and users; it only changes
when Yandex ships a new frontend. If signatures start being rejected
(HTTP 403 "not-allowed"), the plugin automatically re-downloads the
frontend, extracts the new key, and retries. A refreshed key is cached in
the yt-dlp cache dir. You can also pin a key explicitly:

    yt-dlp --extractor-args "yandexmusicv2:hmac_key=<key>" ...

(when the automatic refresh cannot find the key — e.g. the frontend
layout changed again — extract it with ``tools/extract_secret_key.py``
and pass it via the option above).

Transport ``raw`` returns unencrypted media URLs on ``strm.yandex.net``
(no decryption key needed); ``encraw`` returns encrypted URLs plus a
per-request ``key``. This plugin always requests ``raw``.

Quality mapping (web app "stream quality" setting -> API ``quality``):

    high_quality -> lossless   (FLAC if available, else MP3 320)
    balanced     -> nq         (e.g. AAC-mp4 192 / MP3 192)
    efficient    -> lq
    preview      -> preview

This plugin requests ``lossless`` first and falls back to ``nq``.
"""

import base64
import hashlib
import hmac
import json
import re
import time
import uuid
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

from yt_dlp.extractor.common import InfoExtractor
try:
    from yt_dlp.extractor.yandexmusic import YandexMusicTrackIE as _BuiltinYandexMusicTrackIE
except ImportError:  # upstream removed/renamed the built-in: keep going,
    # the plugin's own extractors still work (only shadowing is lost)
    _BuiltinYandexMusicTrackIE = InfoExtractor
from yt_dlp.networking import HEADRequest
from yt_dlp.utils import ExtractorError, float_or_none, int_or_none

__all__ = [
    'YandexMusicTrackIE',
    'YandexMusicV2PlaylistIE',
    'YandexMusicV2LikedPlaylistIE',
    'YandexMusicPlaylistIE',
    'YandexMusicArtistIE',
    'YandexMusicArtistTracksIE',
    'YandexMusicArtistAlbumsIE',
    'YandexMusicAlbumIE',
]

_API = 'https://api.music.yandex.ru'
# Fallback app-level secret, hardcoded in the music-web frontend bundle.
# Current layout (v4.1603.1+): player:{secretKey:{web:"<key>",win32:...,darwin:...,linux:...}}
# Older layout (v4.1520.1): module 25079 of the "8290-*.js" chunk.
# Stable across sessions; only changes when Yandex ships a new frontend.
# See research/api-notes.md and tools/extract_secret_key.py.
_SECRET_KEY = '7tvSmFbyf5hJnIHhCimDDD'
_CODECS = 'flac,mp3'
_TRANSPORT = 'raw'
_UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36')
_FRONTEND_HOME = 'https://music.yandex.ru/'
# extractor-args names to look up user-provided arguments under
# (--extractor-args "<name>:<arg>=<value>")
_KEY_ARG_NAMES = ('yandexmusicv2', 'yandexmusic',
                  'yandexmusic:track', 'yandexmusic:album',
                  'yandexmusic:artist:tracks', 'yandexmusic:playlist',
                  'yandexmusicv2:playlist', 'yandexmusicv2:liked',
                  'yandexmusicv2:artist')
# in-process cache for keys refreshed from the frontend
_KEY_CACHE = {'key': None, 'refresh_failed': False}


class _KeyRejected(Exception):
    """Internal: the API rejected our request signature (403 not-allowed)."""


def _make_sign(ts, track_id, quality, codecs, transport, key):
    """Compute the get-file-info request signature.

    data = ts + trackId + quality + codecs.join("") + transport
    (codecs are joined WITHOUT commas for signing; the URL uses commas)
    """
    data = f'{ts}{track_id}{quality}{codecs.replace(",", "")}{transport}'
    digest = hmac.new(key.encode(), data.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode().rstrip('=')


def _extractor_arg(ie, key):
    """Value of --extractor-args "<name>:<key>" for any accepted name.

    yt-dlp's CLI parser splits on the *first* colon, so
    ``--extractor-args "yandexmusicv2:liked:hmac_key=K"`` lands as
    ``{'yandexmusicv2': {'liked:hmac_key': ['K']}}`` — scan for the bare key
    and the ``<sub>:<key>`` suffix under every accepted top-level name.
    """
    args = ie._downloader.params.get('extractor_args') or {}
    for name in _KEY_ARG_NAMES:
        sub = args.get(name.lower())
        if not isinstance(sub, dict):
            continue
        for arg_name, value in sub.items():
            if arg_name != key and not arg_name.endswith(':' + key):
                continue
            if isinstance(value, (list, tuple)):  # CLI passes a list of values
                value = value[0] if value else None
            return value
    return None


def _user_key(ie):
    """Key pinned via --extractor-args "<name>:hmac_key=<key>" (if any)."""
    key = _extractor_arg(ie, 'hmac_key')
    return key if isinstance(key, str) and key else None


def _load_cached_key(ie):
    """In-memory cache, then yt-dlp's on-disk cache (from a previous refresh)."""
    if _KEY_CACHE['key']:
        return _KEY_CACHE['key']
    data = ie.cache.load('yandex-music-v2', 'hmac-key')
    key = data.get('key') if isinstance(data, dict) else None
    if isinstance(key, str) and key:
        _KEY_CACHE['key'] = key
        return key
    return None


def _save_cached_key(ie, key):
    _KEY_CACHE['key'] = key
    ie.cache.store('yandex-music-v2', 'hmac-key', {'key': key})


def _current_key(ie):
    """Key resolution: --extractor-args > refreshed cache > hardcoded."""
    return _user_key(ie) or _load_cached_key(ie) or _SECRET_KEY


# Key locations in the frontend bundle, newest layout first:
#   v4.1603.1+:  player:{secretKey:{web:"KEY",win32:"...",...}}
#   older:       player:{...,secretKey:"KEY",...}
_KEY_PATTERNS = (
    re.compile(r'secretKey\s*:\s*\{\s*web\s*:\s*"([A-Za-z0-9]{8,64})"'),
    re.compile(r'secretKey\s*:\s*"([A-Za-z0-9]{8,64})"'),
)


def _frontend_chunk_urls(ie, html):
    """Collect all frontend chunk URLs from the page HTML and the
    webpack runtime's lazy-chunk map (``a.u``).

    ``a.u`` is a ternary chain with three entry styles (all handled):
    * literal names:      ``3266===e?"static/chunks/3266-HASH.js"``
    * concat names:       ``32732===e?"static/chunks/"+e+"-HASH.js"``
      (the chunk id is in the ternary condition)
    * hash tables:        ``"static/chunks/"+((prefixTable[e]||e)+"."+
      suffixTable[e])``
    """
    urls = set(re.findall(r'https://[^"\s]+?/static/chunks/[A-Za-z0-9_./()%-]+\.js', html))
    m = re.search(r'"p":"(https://[^"]+/static/chunks/)"', html)
    if m:
        for rel in re.findall(r'static/chunks/([A-Za-z0-9_./()%-]+\.js)', html):
            urls.add(m.group(1) + rel)
    for wurl in (u for u in urls if 'webpack-' in u):
        js = ie._download_webpage(wurl, 'ym-frontend', note=False,
                                  fatal=False, errnote=False)
        if not js:
            continue
        i = js.find('a.u=')
        if i == -1:
            continue
        j = js.find(',a.', i + 10)
        seg = js[i:j if j != -1 else i + 6000]
        base = wurl.rsplit('/static/chunks/', 1)[0] + '/static/chunks/'
        # literal names (the capture excludes the static/chunks/ prefix —
        # prepending it twice used to 404)
        for name in re.findall(r'"static/chunks/([^"]+\.js)"', seg):
            urls.add(base + name)
        # concat names: the chunk id is in the ternary condition
        for cid, hash_ in re.findall(
                r'(\d+)===e\?"static/chunks/"\+e\+"-([a-f0-9]+)\.js"', seg):
            urls.add(f'{base}{cid}-{hash_}.js')
        tables = re.findall(r'\(\{([^}]+)\}\)\[e\]', seg)
        if len(tables) >= 2:
            t_prefix = dict(re.findall(r'(\d+):"([a-f0-9]+)"', tables[-2]))
            t_suffix = dict(re.findall(r'(\d+):"([a-f0-9]+)"', tables[-1]))
            for cid, suffix in t_suffix.items():
                urls.add(f'{base}{t_prefix.get(cid, cid)}.{suffix}.js')
        break
    return urls


def _fetch_key_from_frontend(ie):
    """Download the current music-web frontend and extract the signing key.

    Returns the key, or None if it could not be found (layout change).
    """
    html = ie._download_webpage(
        _FRONTEND_HOME, 'ym-frontend',
        note='Yandex Music: downloading frontend to refresh signing key',
        fatal=False, errnote=False)
    if not html:
        return None
    urls = _frontend_chunk_urls(ie, html)
    if not urls:
        return None

    def _get(u):
        try:
            return ie._download_webpage(u, 'ym-frontend', note=False,
                                        fatal=False, errnote=False)
        except Exception:
            return None

    pool = ThreadPoolExecutor(max_workers=8)
    futures = [pool.submit(_get, u) for u in urls]
    key = None
    try:
        for fut in as_completed(futures):
            js = fut.result()
            if not js:
                continue
            for pat in _KEY_PATTERNS:
                m = pat.search(js)
                if m:
                    key = m.group(1)
                    break
            if key:
                break
    finally:
        # don't wait for the remaining chunk downloads (they finish in the
        # background; the interpreter joins the pool at exit)
        pool.shutdown(wait=False, cancel_futures=True)
    if key is None:
        ie.write_debug(
            f'Yandex Music: key refresh scanned {len(urls)} frontend chunks, '
            f'no key pattern matched (layout change?)')
        # a *completed* scan that found nothing means the layout changed —
        # re-scanning within this process cannot help, so negative-cache it.
        # (A failed homepage/chunk fetch returns before this point and stays
        # retryable on the next track.)
        _KEY_CACHE['refresh_failed'] = True
    return key


def _api_headers():
    return {
        'User-Agent': _UA,
        'Accept': 'application/json',
        'Origin': 'https://music.yandex.ru',
        'Referer': 'https://music.yandex.ru/',
        'X-Requested-With': 'XMLHttpRequest',
        'X-Retpath-Y': 'https://music.yandex.ru/',
        'x-request-id': str(uuid.uuid4()),
        'x-yandex-music-client': 'YandexMusicWebNext/1.0.0',
        'x-yandex-music-without-invocation-info': '1',
        'accept-language': 'en',
    }


def _get_track_meta(ie, track_id):
    boundary = '----WebKitFormBoundary' + uuid.uuid4().hex[:16]
    body = (f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="trackIds"\r\n\r\n'
            f'{track_id}\r\n'
            f'--{boundary}--\r\n').encode()
    headers = _api_headers()
    headers['Content-Type'] = f'multipart/form-data; boundary={boundary}'
    resp = ie._download_json(
        f'{_API}/tracks', track_id,
        note=f'Yandex Music: downloading track {track_id} metadata',
        data=body, headers=headers,
        errnote='Yandex Music: failed to download track metadata')
    if not resp or not isinstance(resp, list):
        raise ExtractorError('Yandex Music: unexpected track metadata response')
    return resp[0]


def _get_download_info(ie, track_id, quality):
    ts = str(int(time.time()))
    sign = _make_sign(ts, track_id, quality, _CODECS, _TRANSPORT, _current_key(ie))
    url = (f'{_API}/get-file-info?ts={ts}&trackId={track_id}&quality={quality}'
           f'&codecs={urllib.parse.quote(_CODECS)}'
           f'&transports={_TRANSPORT}&sign={urllib.parse.quote(sign)}')
    try:
        info = ie._download_json(
            url, track_id,
            note=f'Yandex Music: requesting stream URL for track {track_id} ({quality})',
            headers=_api_headers(),
            errnote=f'Yandex Music: failed to get stream URL for track {track_id} ({quality})',
            expected_status=(403,))
    except ExtractorError:
        # a non-JSON 403 body makes _download_json raise before returning —
        # re-fetch the raw body to check for a rejected signature
        # (expected_status: without it a 403 returns False, not the body)
        try:
            body = ie._download_webpage(
                url, track_id, note=None, fatal=False, errnote=False,
                expected_status=(403,)) or ''
        except Exception:
            body = ''
        if 'not-allowed' in body:
            raise _KeyRejected() from None
        raise
    if not isinstance(info, dict):
        # JSON array (or other non-object) 403 body
        if 'not-allowed' in json.dumps(info):
            raise _KeyRejected() from None
        raise ExtractorError(
            f'Yandex Music: unexpected 403 response: {str(info)[:200]}',
            expected=True)
    # a rejected signature is reported as a JSON error object
    # ({"name": "track-download-info-error", "message": "not-allowed", ...},
    # possibly wrapped in a "result" key)
    err = info.get('result') if isinstance(info.get('result'), dict) else info
    if err.get('name') == 'track-download-info-error':
        if err.get('message') == 'not-allowed':
            raise _KeyRejected() from None
        raise ExtractorError(
            f'Yandex Music: {err.get("message") or "download info error"}',
            expected=True)
    di = info.get('downloadInfo') if isinstance(info, dict) else None
    if not di or not di.get('url'):
        raise ExtractorError('Yandex Music: no download URL in response')
    return di


def _normalize_cover(cover):
    """Normalize a cover URL: protocol-relative (``//…``) and Yandex's
    ``%%`` size placeholder (1000x1000 is the largest served)."""
    if not cover:
        return None
    if cover.startswith('//'):
        cover = 'https:' + cover
    elif not cover.startswith('http'):
        cover = f'https://{cover}'
    return cover.replace('%%', '1000x1000') if '%%' in cover else cover


class YandexMusicTrackIE(_BuiltinYandexMusicTrackIE):
    """Yandex Music track via the modern api.music.yandex.ru endpoints."""

    IE_NAME = 'yandexmusic:track'  # explicit: also valid if the built-in
    # is ever removed upstream (the ImportError fallback base class)
    _VALID_URL = (r'^https?://music\.yandex\.[a-z.]+'
                  r'/(?:album/\d+/)?track/(?P<id>\d+)(?:[?#].*)?$')

    def _download_webpage_handle(self, *args, **kwargs):
        try:
            return super()._download_webpage_handle(*args, **kwargs)
        except TypeError:
            # the built-in override checks `'…' in webpage` without a type
            # check — a fatal=False failure returns False and raises
            # TypeError instead; treat it as a plain failure
            if kwargs.get('fatal', True):
                raise
            return False

    def _real_extract(self, url):
        track_id = self._match_id(url)
        meta = _get_track_meta(self, track_id)
        # a nonexistent track is not an HTTP error: 200 with error: not-found
        if meta.get('error') == 'not-found':
            raise ExtractorError(
                f'Yandex Music: track {track_id} not found', expected=True)
        if not meta.get('available', True):
            raise ExtractorError(
                f'Yandex Music: track {track_id} is not available', expected=True)

        di = None
        for quality in ('lossless', 'nq'):
            try:
                di = _get_download_info(self, track_id, quality)
                break
            except _KeyRejected:
                if _user_key(self):
                    raise ExtractorError(
                        'Yandex Music: request signature rejected (403 '
                        'not-allowed): the pinned hmac_key is out of date. '
                        'Run tools/extract_secret_key.py for the current key, '
                        'or drop the --extractor-args override to let the '
                        'plugin refresh automatically', expected=True)
                if _KEY_CACHE.get('refresh_failed'):
                    # the frontend scan already failed earlier in this
                    # process — don't re-download the whole frontend per track
                    raise ExtractorError(
                        'Yandex Music: request signature rejected (403 '
                        'not-allowed) and the signing key could not be '
                        'refreshed automatically (an earlier refresh in this '
                        'run already failed). Extract the key with '
                        'tools/extract_secret_key.py and pass it via '
                        '--extractor-args "yandexmusicv2:hmac_key=<key>"',
                        expected=True)
                old_key = _current_key(self)
                new_key = _fetch_key_from_frontend(self)
                if new_key and new_key != old_key:
                    _save_cached_key(self, new_key)
                    self.report_warning(
                        f'Yandex Music: signing key rejected, refreshed to '
                        f'{new_key!r} (frontend changed). Pass '
                        f'--extractor-args "yandexmusicv2:hmac_key={new_key}" '
                        f'to skip the auto-refresh')
                    try:
                        di = _get_download_info(self, track_id, quality)
                    except _KeyRejected:
                        raise ExtractorError(
                            'Yandex Music: the refreshed signing key was also '
                            'rejected — the frontend may have changed in a way '
                            'the auto-refresh does not handle. Extract the key '
                            'with tools/extract_secret_key.py and pass it via '
                            '--extractor-args "yandexmusicv2:hmac_key=<key>"',
                            expected=True) from None
                    break
                if new_key == old_key:
                    # the refreshed key is the one the server just rejected
                    # (e.g. clock skew) — re-scanning cannot help
                    _KEY_CACHE['refresh_failed'] = True
                hint = (' or your system clock is skewed outside the '
                         'server\'s signature window'
                         if new_key == old_key else '')
                raise ExtractorError(
                    'Yandex Music: request signature rejected (403 not-allowed) '
                    'and the signing key could not be refreshed automatically '
                    f'(frontend layout may have changed{hint}). Extract the '
                    'key with tools/extract_secret_key.py and pass it via '
                    '--extractor-args "yandexmusicv2:hmac_key=<key>"',
                    expected=True)
            except ExtractorError as e:
                if quality == 'nq':
                    raise
                self.report_warning(
                    f'Track {track_id}: lossless unavailable, falling back '
                    f'to nq ({e})')

        self._warn_if_preview_served(track_id, meta, di)

        codec = di.get('codec') or 'mp3'
        ext = 'flac' if codec == 'flac' else 'mp3'
        artists = [a.get('name') for a in meta.get('artists', []) if a.get('name')]
        # The API returns a *list* of albums (a track may appear on several
        # compilations); the first entry is the primary one.
        albums = meta.get('albums') or []
        album = albums[0] if albums and isinstance(albums[0], dict) else {}
        position = album.get('trackPosition') or {}
        album_artists = [a.get('name') for a in album.get('artists', []) if a.get('name')]
        thumbnail = _normalize_cover(meta.get('coverUri') or album.get('coverUri'))
        year = album.get('year')
        release = f'{year:04d}0101' if isinstance(year, int) else None

        return {
            'id': str(track_id),
            'title': meta.get('title'),
            'artist': ', '.join(artists) or None,
            'album': album.get('title') or None,
            'album_id': str(album['id']) if album.get('id') else None,
            'album_artist': ', '.join(album_artists) or None,
            'track_number': int_or_none(position.get('index')),
            'track_total': int_or_none(album.get('trackCount')),
            'disc_number': int_or_none(position.get('volume')),
            'date': release,
            # yt-dlp's metadata postprocessor reads upload_date, not date
            'upload_date': release,
            'thumbnail': thumbnail,
            'duration': float_or_none(meta.get('durationMs'), 1000),
            'formats': [{
                'url': di['url'],
                'ext': ext,
                'format_id': f'{codec}-{di.get("bitrate") or "?"}k',
                'abr': int_or_none(di.get('bitrate')),
                'vcodec': 'none',
            }],
        }

    def _warn_if_preview_served(self, track_id, meta, di):
        """Guard against the silent preview mode: with a stale/missing
        session the API serves `smart_preview` (13–30 s) streams for every
        quality level — no error, the file just comes out short.

        Two signals, cheapest first:
          1. the served `quality` is a preview level (a stale session makes
             the server report `smart_preview` for any requested quality),
          2. the served stream is far shorter than the track's metadata
             duration — estimated from the stream URL's Content-Length
             (HEAD) and the served bitrate.
        Best-effort: network problems here are never fatal.

        Disable with --extractor-args "<name>:preview_check=off" (e.g. for
        very large playlists where the extra HEAD per track is not worth it).
        """
        if _extractor_arg(self, 'preview_check') in ('off', 'false', '0'):
            return
        served = di.get('quality')
        if served in ('preview', 'smart_preview'):
            self.report_warning(
                f'Track {track_id}: server served a {served!r} stream — the '
                f'Yandex session may be stale (the API silently serves '
                f'smart_preview streams instead of full tracks) or this '
                f'track may be preview-only')
            return
        duration_ms = meta.get('durationMs')
        bitrate = di.get('bitrate')
        if not (duration_ms and bitrate and di.get('url')):
            return
        try:
            with self._downloader.urlopen(
                    HEADRequest(di['url'], headers={'User-Agent': _UA})) as resp:
                content_length = 0
                for k, v in (resp.headers or {}).items():
                    if k.lower() == 'content-length':
                        content_length = int(v)
                        break
        except Exception:
            return
        if not content_length:
            return
        est_ms = content_length * 8 / bitrate
        if est_ms < duration_ms * 0.5:
            self.report_warning(
                f'Track {track_id}: served stream is ~{est_ms / 1000:.0f} s '
                f'but the track is {duration_ms / 1000:.0f} s — the Yandex '
                f'session may be stale (the API silently serves smart_preview '
                f'streams) or this track may be preview-only')


def _rsc_payload(webpage):
    """Join the Next.js RSC payload chunks (``self.__next_f.push`` strings).

    Each push argument is a JSON-encoded string fragment; decoding and
    concatenating them reconstructs the full flight payload.
    """
    chunks = re.findall(
        r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', webpage)
    out = []
    for c in chunks:
        try:
            out.append(json.loads('"' + c + '"'))
        except json.JSONDecodeError:
            continue  # malformed escape sequence — skip the fragment
    return ''.join(out)


def _preloaded_object(payload, key):
    """Decode the JSON object stored under ``"key":`` in the RSC payload.

    Returns None if the key is missing or its value is not an object
    (e.g. ``null`` for a not-found page).
    """
    i = payload.find(f'"{key}":')
    if i == -1:
        return None
    j = i + len(key) + 3  # skip past '"key":'
    while j < len(payload) and payload[j] in ' \t':
        j += 1
    if j >= len(payload) or payload[j] != '{':
        return None
    # raw_decode: unlike manual brace counting, it handles {/} inside
    # JSON string values (playlist descriptions, track titles, ...)
    try:
        data, _ = json.JSONDecoder().raw_decode(payload, j)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


class YandexMusicV2PlaylistIE(InfoExtractor):
    """Shared Yandex Music playlist: https://music.yandex.ru/playlists/<uuid>

    Also matches chart playlists (``/playlists/ch.<uuid>`` — kind 1076,
    preloaded like any playlist).
    """

    IE_NAME = 'yandexmusicv2:playlist'
    _VALID_URL = (r'^https?://music\.yandex\.[a-z.]+/playlists/'
                  r'(?:(?:pl|ch)\.)?(?P<id>[0-9a-fA-F-]{36})(?:[?#].*)?$')
    # RSC payload key holding the preloaded playlist object
    _PRELOAD_KEY = 'preloadedPlaylistByUuid'

    def _parse_preloaded_playlist(self, webpage, url):
        """Extract the preloaded playlist object from the Next.js RSC payload."""
        data = _preloaded_object(_rsc_payload(webpage), self._PRELOAD_KEY)
        if data is not None:
            return data
        # no preload (the page rendered a shell — RSC layout change, server
        # hiccup) — the playlist API serves the full list by uuid. The
        # stored uuid includes its lk./pl./ch. prefix: it is exactly the
        # URL's path segment after /playlists/.
        full_uuid = url.split('/playlists/', 1)[1]
        full_uuid = full_uuid.split('?', 1)[0].split('#', 1)[0]
        api_data = self._fetch_playlist_via_api(
            {'playlistUuid': full_uuid}, expect_uuid=full_uuid)
        if api_data:
            return api_data
        raise ExtractorError(
            'Yandex Music: playlist data not found in page '
            '(is the playlist public and are cookies valid?)', expected=True)

    def _fetch_playlist_via_api(self, data, expect_uuid=None):
        """Fallback: fetch the playlist (incl. full track list) via the API.

        Used when the page preload is incomplete. Both endpoints return the
        whole playlist in one response (``page``/``perPage`` params are
        ignored by the server, verified with 1500+ track playlists).

        ``expect_uuid``: when set, a response whose ``playlistUuid`` does
        not match is skipped (guards against the users API returning a
        *different* user's playlist when the uid was guessed from the page).
        """
        uid = data.get('uid') or (data.get('owner') or {}).get('uid')
        kind = data.get('kind')
        playlist_uuid = data.get('playlistUuid')
        playlist_id = playlist_uuid or 'playlist'
        urls = []
        if uid and kind is not None:
            urls.append(f'{_API}/users/{uid}/playlists/{kind}')
        if playlist_uuid:
            urls.append(f'{_API}/playlist/{playlist_uuid}')
        for url in urls:
            try:
                info = self._download_json(
                    url, playlist_id,
                    note='Yandex Music: downloading playlist track list (API fallback)',
                    query={'resumeStream': 'false', 'richTracks': 'false'},
                    headers=_api_headers(),
                    errnote='Yandex Music: failed to download playlist track list')
            except ExtractorError:
                continue
            if isinstance(info, dict) and isinstance(info.get('result'), dict):
                info = info['result']
            if not (isinstance(info, dict) and info.get('tracks')):
                continue
            if (expect_uuid and info.get('playlistUuid')
                    and info['playlistUuid'] != expect_uuid):
                continue
            return info
        return None

    def _playlist_entries(self, tracks):
        entries = []
        for t in tracks or []:
            if not isinstance(t, dict):
                continue
            track_id = t.get('id')
            album_id = t.get('albumId')
            if not track_id:
                continue
            track_url = (f'https://music.yandex.ru/album/{album_id}/track/{track_id}'
                         if album_id
                         else f'https://music.yandex.ru/track/{track_id}')
            entries.append(self.url_result(
                track_url, ie_key=YandexMusicTrackIE.ie_key()))
        return entries

    def _real_extract(self, url):
        playlist_uuid = self._match_id(url)
        webpage = self._download_webpage(
            url, playlist_uuid,
            note='Yandex Music: downloading playlist page',
            errnote='Yandex Music: failed to download playlist page')
        data = self._parse_preloaded_playlist(webpage, url)
        if not data.get('available', True):
            raise ExtractorError(
                f'Yandex Music: playlist {playlist_uuid} is not available', expected=True)

        tracks = data.get('tracks') or []
        track_count = data.get('trackCount') or len(tracks)
        if len(tracks) < track_count:
            # the page preload should contain the full track list (verified
            # with 1500+ track playlists); if it is short, fall back to the
            # users API, which always returns the complete list
            self.report_warning(
                f'Yandex Music: playlist page only preloaded {len(tracks)} of '
                f'{track_count} tracks, falling back to the users API')
            api_data = self._fetch_playlist_via_api(data)
            if api_data and len(api_data.get('tracks') or []) > len(tracks):
                tracks = api_data['tracks']
                # the API list is authoritative — the preload's count is stale
                track_count = api_data.get('trackCount') or len(tracks)
            elif len(tracks) < track_count:
                self.report_warning(
                    f'Yandex Music: only {len(tracks)} of {track_count} '
                    f'playlist tracks could be retrieved')

        entries = self._playlist_entries(tracks)

        return {
            '_type': 'playlist',
            'id': playlist_uuid,
            'title': data.get('title'),
            'description': data.get('description'),
            'entries': entries,
            # don't report a count larger than what we actually have (the
            # progress display would otherwise promise tracks we never list)
            'playlist_count': min(track_count, len(entries)),
        }


class YandexMusicV2LikedPlaylistIE(YandexMusicV2PlaylistIE):
    """The user's "liked"/favorites playlist:
    https://music.yandex.ru/playlists/lk.<uuid>

    The page preload contains the full (1500+ track) list; the API fallback
    inherited from the base class covers a paginated/short preload.
    """

    IE_NAME = 'yandexmusicv2:liked'
    _VALID_URL = (r'^https?://music\.yandex\.[a-z.]+/playlists/lk\.'
                  r'(?P<id>[0-9a-fA-F-]{36})(?:[?#].*)?$')


class YandexMusicPlaylistIE(YandexMusicV2PlaylistIE):
    """User-playlist URLs (shadows the broken built-in ``yandexmusic:playlist``):

    * ``https://music.yandex.ru/users/<login>/playlists/<kind>.<uuid>``
      (kind ``lk``/``pl``/``ch`` — the mirror of ``/playlists/<uuid>``)
    * ``https://music.yandex.ru/users/<login>/playlists/<kind>``
      (numeric kind, e.g. ``3`` = liked, ``1003`` = a user playlist)

    The page preloads the complete playlist (``preloadedPlaylist``) for
    public playlists; the owner's private playlists in the uuid-mirror form
    have no preload and are fetched by uuid via the playlist API instead.
    """

    IE_NAME = 'yandexmusic:playlist'
    _PRELOAD_KEY = 'preloadedPlaylist'
    _VALID_URL = (r'^https?://music\.yandex\.[a-z.]+/users/'
                  r'(?P<login>[^/]+)/playlists/'
                  r'(?:(?P<kind>[a-z]{2,3})\.)?(?P<id>[0-9a-fA-F-]{36}|\d+)'
                  r'(?:[?#].*)?$')

    def _parse_preloaded_playlist(self, webpage, url):
        data = _preloaded_object(_rsc_payload(webpage), self._PRELOAD_KEY)
        if data is not None:
            return data
        # no preload (e.g. the owner's private playlist in the uuid-mirror
        # form — the web app renders a shell there too) — fetch via the API.
        # The users API returns the most complete list, so it is tried first
        # (the numeric kind is derived from the uuid prefix when possible);
        # GET /playlist/<uuid> (uuid incl. its lk./pl./ch. prefix) is the
        # fallback. Both are handled by the inherited dual-endpoint method.
        m = re.match(self._VALID_URL, url)
        kind, pid = m.group('kind'), m.group('id')
        kind_map = {'lk': 3, 'pl': 1003, 'ch': 1076}
        uid_m = re.search(r'"uid":(\d+)', _rsc_payload(webpage))
        data = {
            'uid': int(uid_m.group(1)) if uid_m else None,
            # numeric-kind form: the kind *is* the id (e.g. /playlists/3)
            'kind': kind_map.get(kind) or (int(pid) if pid.isdigit() else None),
            'playlistUuid': f'{kind}.{pid}' if kind else pid,
        }
        expect_uuid = (f'{kind}.{pid}' if kind else pid)
        if not re.fullmatch(r'[0-9a-fA-F-]{36}', pid):
            expect_uuid = None  # numeric-kind form: nothing to verify against
        api_data = self._fetch_playlist_via_api(data, expect_uuid=expect_uuid)
        if api_data:
            return api_data
        raise ExtractorError(
            'Yandex Music: playlist data not found in page '
            '(is the playlist public and are cookies valid?)', expected=True)


class YandexMusicArtistIE(InfoExtractor):
    """All tracks of a Yandex Music artist: https://music.yandex.ru/artist/<id>

    The track list comes from ``GET /artists/<id>/tracks``, which is
    paginated (20 tracks per page; the ``perPage`` parameter is ignored by
    the server), so pages are fetched until the pager's ``total`` is reached.
    """

    IE_NAME = 'yandexmusicv2:artist'
    _VALID_URL = r'^https?://music\.yandex\.[a-z.]+/artist/(?P<id>\d+)(?:[?#].*)?$'
    # safety cap against a misbehaving pager (20 tracks/page -> 20k tracks)
    _MAX_PAGES = 1000

    def _real_extract(self, url):
        artist_id = self._match_id(url)
        info = self._download_json(
            f'{_API}/artists/{artist_id}', artist_id,
            note='Yandex Music: downloading artist info',
            headers=_api_headers(),
            errnote='Yandex Music: failed to download artist info')
        artist = info.get('artist') or {}
        # a nonexistent artist is not an HTTP error: 200 with artist.error
        if artist.get('error') == 'not-found' or not artist.get('name'):
            raise ExtractorError(
                f'Yandex Music: artist {artist_id} not found', expected=True)

        entries = []
        seen = set()
        total = None
        page = 0
        while page < self._MAX_PAGES:
            data = self._download_json(
                f'{_API}/artists/{artist_id}/tracks', artist_id,
                note=f'Yandex Music: downloading artist tracks (page {page})',
                query={'page': page},
                headers=_api_headers(),
                errnote='Yandex Music: failed to download artist tracks')
            pager = data.get('pager') or {}
            if pager.get('total') is not None:
                total = pager['total']
            tracks = data.get('tracks') or []
            if not tracks:
                break
            new_on_page = 0
            for t in tracks:
                if not isinstance(t, dict):
                    continue
                track_id = t.get('id')
                if not track_id or track_id in seen:
                    continue
                seen.add(track_id)
                albums = t.get('albums') or []
                album_id = (albums[0].get('id')
                            if albums and isinstance(albums[0], dict) else None)
                track_url = (f'https://music.yandex.ru/album/{album_id}/track/{track_id}'
                             if album_id
                             else f'https://music.yandex.ru/track/{track_id}')
                entries.append(self.url_result(
                    track_url, ie_key=YandexMusicTrackIE.ie_key()))
                new_on_page += 1
            if new_on_page == 0:
                break  # exhausted (or the pager ignores `page`)
            if total is not None and len(entries) >= total:
                break
            page += 1
        if page >= self._MAX_PAGES:
            self.report_warning(
                'Yandex Music: stopped after ' + str(self._MAX_PAGES) +
                ' pages of artist tracks (pager may be misbehaving)')

        cover = artist.get('ogImage')
        if not cover:
            c = artist.get('cover')
            # the API usually returns a dict, but a plain URL string has been
            # seen — handle both
            cover = c.get('uri') if isinstance(c, dict) else (
                c if isinstance(c, str) else '')
        thumbnail = _normalize_cover(cover)

        return {
            '_type': 'playlist',
            'id': artist_id,
            'title': artist.get('name'),
            'thumbnail': thumbnail,
            'entries': entries,
            'playlist_count': (min(total, len(entries))
                               if total is not None else len(entries)),
        }


class YandexMusicArtistTracksIE(YandexMusicArtistIE):
    """Shadow for the broken built-in ``yandexmusic:artist:tracks`` extractor
    (https://music.yandex.ru/artist/<id>/tracks — the "all tracks" subpage).
    Same implementation as the artist-page extractor.
    """

    IE_NAME = 'yandexmusic:artist:tracks'
    _VALID_URL = r'^https?://music\.yandex\.[a-z.]+/artist/(?P<id>\d+)/tracks(?:[?#].*)?$'


class YandexMusicArtistAlbumsIE(InfoExtractor):
    """An artist's albums (shadows the broken built-in
    ``yandexmusic:artist:albums``): https://music.yandex.ru/artist/<id>/albums

    The page preloads only the first 20 albums; the complete list — the
    artist's own releases, matching the web app's Albums tab — comes from
    ``GET /artists/<id>/direct-albums`` (one response, cookieless; the
    server ignores ``page``/``perPage``). Featured appearances
    (``/also-albums``) are a separate web-app tab and are not included.
    """

    IE_NAME = 'yandexmusic:artist:albums'
    _VALID_URL = (r'^https?://music\.yandex\.[a-z.]+/artist/(?P<id>\d+)'
                  r'/albums(?:[?#].*)?$')

    def _real_extract(self, url):
        artist_id = self._match_id(url)
        host = re.match(r'https?://([^/]+)/', url).group(1)
        info = self._download_json(
            f'{_API}/artists/{artist_id}', artist_id,
            note='Yandex Music: downloading artist info',
            headers=_api_headers(),
            errnote='Yandex Music: failed to download artist info')
        artist = info.get('artist') or {}
        # a nonexistent artist is not an HTTP error: 200 with artist.error
        if artist.get('error') == 'not-found' or not artist.get('name'):
            raise ExtractorError(
                f'Yandex Music: artist {artist_id} not found', expected=True)
        data = self._download_json(
            f'{_API}/artists/{artist_id}/direct-albums', artist_id,
            note='Yandex Music: downloading artist albums',
            headers=_api_headers(),
            errnote='Yandex Music: failed to download artist albums')
        if isinstance(data, dict) and isinstance(data.get('result'), dict):
            data = data['result']
        albums = [a for a in (data or {}).get('albums') or []
                  if isinstance(a, dict) and a.get('id')]
        entries = []
        for album in albums:
            entries.append(self.url_result(
                f'https://{host}/album/{album["id"]}',
                ie_key=YandexMusicAlbumIE.ie_key(),
                video_id=str(album['id']),
                video_title=album.get('title')))
        return {
            '_type': 'playlist',
            'id': artist_id,
            'title': f'{artist["name"]} — albums',
            'entries': entries,
            'playlist_count': len(entries),
        }


class YandexMusicAlbumIE(InfoExtractor):
    """All tracks of a Yandex Music album: https://music.yandex.ru/album/<id>

    Shadows the broken built-in ``yandexmusic:album`` extractor. The track
    list is taken from the page's preloaded data: a flat ``tracks`` array
    for regular albums, or a per-volume ``volumes`` array of arrays for
    large/multi-disc albums (both verified to contain the full list).
    """

    IE_NAME = 'yandexmusic:album'
    _VALID_URL = r'^https?://music\.yandex\.[a-z.]+/album/(?P<id>\d+)(?:[?#].*)?$'

    def _real_extract(self, url):
        album_id = self._match_id(url)
        webpage = self._download_webpage(
            url, album_id,
            note='Yandex Music: downloading album page',
            errnote='Yandex Music: failed to download album page')
        data = _preloaded_object(_rsc_payload(webpage), 'preloadedAlbum')
        if data is None:
            raise ExtractorError(
                f'Yandex Music: album {album_id} not found in page '
                '(does the album exist and are cookies valid?)', expected=True)
        if not data.get('available', True):
            raise ExtractorError(
                f'Yandex Music: album {album_id} is not available', expected=True)

        track_ids = []
        for t in data.get('tracks') or []:
            if isinstance(t, dict) and t.get('id'):
                track_ids.append(t['id'])
        if not track_ids:
            for volume in data.get('volumes') or []:
                for t in volume or []:
                    if isinstance(t, dict) and t.get('id'):
                        track_ids.append(t['id'])
        track_count = data.get('trackCount') or len(track_ids)
        if len(track_ids) < track_count:
            self.report_warning(
                f'Yandex Music: album page only preloaded {len(track_ids)} of '
                f'{track_count} tracks')

        entries = [
            self.url_result(
                f'https://music.yandex.ru/album/{album_id}/track/{track_id}',
                ie_key=YandexMusicTrackIE.ie_key())
            for track_id in track_ids
        ]

        thumbnail = _normalize_cover(data.get('coverUri'))

        return {
            '_type': 'playlist',
            'id': album_id,
            'title': data.get('title'),
            'thumbnail': thumbnail,
            'entries': entries,
            'playlist_count': min(track_count, len(entries)),
        }
