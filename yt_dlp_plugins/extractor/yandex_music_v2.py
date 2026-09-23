"""
yt-dlp plugin: Yandex Music (modern API, 2026).

Shadows the broken built-in ``YandexMusicTrackIE`` / ``YandexMusicAlbumIE`` /
``YandexMusicArtistTracksIE`` (which rely on the deprecated
``handlers/*.jsx`` endpoints that now return 404) and adds support for:

* shared playlist URLs (``https://music.yandex.ru/playlists/<uuid>``)
* the user's "liked"/favorites playlist
  (``https://music.yandex.ru/playlists/lk.<uuid>``)
* artist pages (``https://music.yandex.ru/artist/<id>``) — all of the
  artist's tracks, via the paginated ``/artists/<id>/tracks`` API
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
from concurrent.futures import ThreadPoolExecutor

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.extractor.yandexmusic import YandexMusicTrackIE as _BuiltinYandexMusicTrackIE
from yt_dlp.utils import ExtractorError, float_or_none, int_or_none

__all__ = [
    'YandexMusicTrackIE',
    'YandexMusicV2PlaylistIE',
    'YandexMusicV2LikedPlaylistIE',
    'YandexMusicArtistIE',
    'YandexMusicArtistTracksIE',
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
# extractor-args names to look up a user-provided hmac_key under
# (--extractor-args "<name>:hmac_key=<key>")
_KEY_ARG_NAMES = ('yandexmusicv2', 'yandexmusic',
                  'yandexmusic:track', 'yandexmusic:album',
                  'yandexmusic:artist:tracks',
                  'yandexmusicv2:playlist', 'yandexmusicv2:liked',
                  'yandexmusicv2:artist', 'yandexmusicv2:album')
# in-process cache for keys refreshed from the frontend
_KEY_CACHE = {'key': None, 'fetched_at': 0}


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


def _user_key(ie):
    """Key pinned via --extractor-args "<name>:hmac_key=<key>" (if any)."""
    args = ie._downloader.params.get('extractor_args') or {}
    for name in _KEY_ARG_NAMES:
        value = (args.get(name.lower()) or {}).get('hmac_key')
        if isinstance(value, (list, tuple)):  # CLI passes a list of values
            value = value[0] if value else None
        if value:
            return value
    return None


def _load_cached_key(ie):
    """In-memory cache, then yt-dlp's on-disk cache (from a previous refresh)."""
    if _KEY_CACHE['key']:
        return _KEY_CACHE['key']
    data = ie.cache.load('yandex-music-v2', 'hmac-key')
    key = data.get('key') if isinstance(data, dict) else None
    if isinstance(key, str) and key:
        _KEY_CACHE['key'] = key
        _KEY_CACHE['fetched_at'] = data.get('ts', 0)
        return key
    return None


def _save_cached_key(ie, key):
    _KEY_CACHE['key'] = key
    _KEY_CACHE['fetched_at'] = time.time()
    ie.cache.store('yandex-music-v2', 'hmac-key',
                   {'key': key, 'ts': _KEY_CACHE['fetched_at']})


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
    webpack runtime's lazy-chunk map (``a.u``)."""
    urls = set(re.findall(r'https://[^"\s]+?/static/chunks/[A-Za-z0-9_./()%-]+\.js', html))
    m = re.search(r'"p":"(https://[^"]+/static/chunks/)"', html)
    if m:
        for rel in re.findall(r'static/chunks/([A-Za-z0-9_./()%-]+\.js)', html):
            urls.add(m.group(1) + rel)
    for wurl in (u for u in urls if 'webpack-' in u):
        js = ie._download_webpage(wurl, 'ym-frontend', note=None,
                                  fatal=False, errnote=False)
        if not js:
            continue
        i = js.find('a.u=')
        if i == -1:
            continue
        j = js.find(',a.', i + 10)
        seg = js[i:j if j != -1 else i + 6000]
        base = wurl.rsplit('/static/chunks/', 1)[0] + '/static/chunks/'
        for name in re.findall(r'"(static/chunks/[^"]+\.js)"', seg):
            urls.add(base + name)
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
            return ie._download_webpage(u, 'ym-frontend', note=None,
                                        fatal=False, errnote=False)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_get, u) for u in urls]
        for fut in futures:
            js = fut.result()
            if not js:
                continue
            for pat in _KEY_PATTERNS:
                m = pat.search(js)
                if m:
                    return m.group(1)
        for fut in futures:  # let in-flight downloads finish
            fut.result()
    return None


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
    info = ie._download_json(
        url, track_id,
        note=f'Yandex Music: requesting stream URL for track {track_id} ({quality})',
        headers=_api_headers(),
        errnote=f'Yandex Music: failed to get stream URL for track {track_id} ({quality})',
        expected_status=(403,))
    if not isinstance(info, (dict, list)):
        # non-JSON 403 error response — read the body
        try:
            body = info.read().decode('utf-8', 'replace')
        except Exception:
            body = ''
        if 'not-allowed' in body:
            raise _KeyRejected() from None
        raise ExtractorError(
            f'Yandex Music: unexpected 403 response: {body[:200] or "(empty)"}',
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


class YandexMusicTrackIE(_BuiltinYandexMusicTrackIE):
    """Yandex Music track via the modern api.music.yandex.ru endpoints."""

    _VALID_URL = (r'^https?://music\.yandex\.(?:ru|kz|ua|by|com)'
                  r'/(?:album/\d+/)?track/(?P<id>\d+)')

    def _real_extract(self, url):
        track_id = self._match_id(url)
        meta = _get_track_meta(self, track_id)
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
                raise ExtractorError(
                    'Yandex Music: request signature rejected (403 not-allowed) '
                    'and the signing key could not be refreshed automatically '
                    '(frontend layout may have changed). Extract the key with '
                    'tools/extract_secret_key.py and pass it via '
                    '--extractor-args "yandexmusicv2:hmac_key=<key>"',
                    expected=True)
            except ExtractorError:
                if quality == 'nq':
                    raise
                self.report_warning(
                    f'Track {track_id}: lossless unavailable, falling back to nq')

        codec = di.get('codec') or 'mp3'
        ext = 'flac' if codec == 'flac' else 'mp3'
        artists = [a.get('name') for a in meta.get('artists', []) if a.get('name')]
        # The API returns a *list* of albums (a track may appear on several
        # compilations); the first entry is the primary one.
        albums = meta.get('albums') or []
        album = albums[0] if albums and isinstance(albums[0], dict) else {}
        position = album.get('trackPosition') or {}
        album_artists = [a.get('name') for a in album.get('artists', []) if a.get('name')]
        cover = meta.get('coverUri') or album.get('coverUri') or ''
        if cover and not cover.startswith('http'):
            cover = f'https://{cover}'
        # '%%' is Yandex's size placeholder; 1000x1000 is the largest served
        thumbnail = cover.replace('%%', '1000x1000') if '%%' in cover else (cover or None)
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


def _rsc_payload(webpage):
    """Join the Next.js RSC payload chunks (``self.__next_f.push`` strings).

    Each push argument is a JSON-encoded string fragment; decoding and
    concatenating them reconstructs the full flight payload.
    """
    chunks = re.findall(
        r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', webpage)
    return ''.join(json.loads('"' + c + '"') for c in chunks)


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
    if payload[j] != '{':
        return None
    # raw_decode: unlike manual brace counting, it handles {/} inside
    # JSON string values (playlist descriptions, track titles, ...)
    try:
        data, _ = json.JSONDecoder().raw_decode(payload, j)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


class YandexMusicV2PlaylistIE(InfoExtractor):
    """Shared Yandex Music playlist: https://music.yandex.ru/playlists/<uuid>"""

    IE_NAME = 'yandexmusicv2:playlist'
    _VALID_URL = r'^https?://music\.yandex\.[a-z.]+/playlists/(?P<id>[0-9a-fA-F-]{36})'

    def _parse_preloaded_playlist(self, webpage):
        """Extract the preloaded playlist object from the Next.js RSC payload."""
        data = _preloaded_object(_rsc_payload(webpage), 'preloadedPlaylistByUuid')
        if data is None:
            raise ExtractorError(
                'Yandex Music: playlist data not found in page '
                '(is the playlist public and are cookies valid?)', expected=True)
        return data

    def _fetch_playlist_via_api(self, data):
        """Fallback: fetch the playlist (incl. full track list) via the users API.

        Used when the page preload is incomplete. The API always returns the
        whole playlist in one response (``page``/``perPage`` params are
        ignored by the server, verified with 1500+ track playlists).
        """
        uid = data.get('uid') or (data.get('owner') or {}).get('uid')
        kind = data.get('kind')
        if not uid or kind is None:
            return None
        playlist_id = data.get('playlistUuid') or 'playlist'
        try:
            return self._download_json(
                f'{_API}/users/{uid}/playlists/{kind}', playlist_id,
                note='Yandex Music: downloading playlist track list (API fallback)',
                query={'resumeStream': 'false', 'richTracks': 'false'},
                headers=_api_headers(),
                errnote='Yandex Music: failed to download playlist track list')
        except ExtractorError:
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
        data = self._parse_preloaded_playlist(webpage)
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
            'playlist_count': track_count,
        }


class YandexMusicV2LikedPlaylistIE(YandexMusicV2PlaylistIE):
    """The user's "liked"/favorites playlist:
    https://music.yandex.ru/playlists/lk.<uuid>

    The page preload contains the full (1500+ track) list; the users-API
    fallback inherited from the base class covers a paginated/short preload.
    """

    IE_NAME = 'yandexmusicv2:liked'
    _VALID_URL = r'^https?://music\.yandex\.[a-z.]+/playlists/lk\.(?P<id>[0-9a-fA-F-]{36})'


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

        cover = artist.get('ogImage') or (artist.get('cover') or {}).get('uri') or ''
        if cover and not cover.startswith('http'):
            cover = f'https://{cover}'
        # '%%' is Yandex's size placeholder; 1000x1000 is the largest served
        thumbnail = cover.replace('%%', '1000x1000') if '%%' in cover else (cover or None)

        return {
            '_type': 'playlist',
            'id': artist_id,
            'title': artist.get('name'),
            'thumbnail': thumbnail,
            'entries': entries,
            'playlist_count': total if total is not None else len(entries),
        }


class YandexMusicArtistTracksIE(YandexMusicArtistIE):
    """Shadow for the broken built-in ``yandexmusic:artist:tracks`` extractor
    (https://music.yandex.ru/artist/<id>/tracks — the "all tracks" subpage).
    Same implementation as the artist-page extractor.
    """

    IE_NAME = 'yandexmusic:artist:tracks'
    _VALID_URL = r'^https?://music\.yandex\.[a-z.]+/artist/(?P<id>\d+)/tracks(?:[?#].*)?$'


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

        cover = data.get('coverUri') or ''
        if cover and not cover.startswith('http'):
            cover = f'https://{cover}'
        # '%%' is Yandex's size placeholder; 1000x1000 is the largest served
        thumbnail = cover.replace('%%', '1000x1000') if '%%' in cover else (cover or None)

        return {
            '_type': 'playlist',
            'id': album_id,
            'title': data.get('title'),
            'thumbnail': thumbnail,
            'entries': entries,
            'playlist_count': track_count,
        }
