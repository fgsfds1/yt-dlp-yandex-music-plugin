"""
yt-dlp plugin: Yandex Music (modern API, 2026).

Shadows the broken built-in ``YandexMusicTrackIE`` (which relies on the
deprecated ``handlers/*.jsx`` endpoints that now return 404) and adds
support for shared playlist URLs (``https://music.yandex.ru/playlists/<uuid>``).

Requires cookies of a logged-in Yandex Music account (``--cookies``).

How it works
------------
The current music.yandex.ru web app talks to ``api.music.yandex.ru``:

* Track metadata:  ``POST /tracks`` (multipart, repeated ``trackIds`` fields,
  values ``trackId`` or ``trackId:albumId``)
* Stream URLs:     ``GET /get-file-info?ts=&trackId=&quality=&codecs=&transports=&sign=``
* Playlist (by owner): ``GET /users/<uid>/playlists/<kind>``

The ``sign`` query parameter is an HMAC-SHA256 signature:

    sign = base64(HMAC-SHA256(secretKey, data)).rstrip("=")
    data = ts + trackId + quality + codecs.join("") + transport

Note the gotcha: for *signing* the codec list is joined with an empty
string, while the actual request URL uses commas.

The ``secretKey`` is an app-level secret hardcoded in the music-web
frontend bundle (module 25079 of the ``8290-*.js`` Next.js chunk). It is
stable across sessions and users; it only changes when Yandex ships a new
frontend. If signatures start being rejected (HTTP 403 "not-allowed"),
re-extract it with ``tools/extract_secret_key.py``.

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

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.extractor.yandexmusic import YandexMusicTrackIE as _BuiltinYandexMusicTrackIE
from yt_dlp.utils import ExtractorError, float_or_none, int_or_none

__all__ = ['YandexMusicTrackIE', 'YandexMusicV2PlaylistIE']

_API = 'https://api.music.yandex.ru'
# App-level secret, hardcoded in the music-web frontend bundle
# (module 25079 of the "8290-*.js" chunk, v4.1520.1). Stable across
# sessions. See research/api-notes.md and tools/extract_secret_key.py.
_SECRET_KEY = '7tvSmFbyf5hJnIHhCimDDD'
_CODECS = 'flac,mp3'
_TRANSPORT = 'raw'
_UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36')


def _make_sign(ts, track_id, quality, codecs, transport):
    """Compute the get-file-info request signature.

    data = ts + trackId + quality + codecs.join("") + transport
    (codecs are joined WITHOUT commas for signing; the URL uses commas)
    """
    data = f'{ts}{track_id}{quality}{codecs.replace(",", "")}{transport}'
    digest = hmac.new(_SECRET_KEY.encode(), data.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode().rstrip('=')


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
    sign = _make_sign(ts, track_id, quality, _CODECS, _TRANSPORT)
    url = (f'{_API}/get-file-info?ts={ts}&trackId={track_id}&quality={quality}'
           f'&codecs={urllib.parse.quote(_CODECS)}'
           f'&transports={_TRANSPORT}&sign={urllib.parse.quote(sign)}')
    info = ie._download_json(
        url, track_id,
        note=f'Yandex Music: requesting stream URL for track {track_id} ({quality})',
        headers=_api_headers(),
        errnote=f'Yandex Music: failed to get stream URL for track {track_id} ({quality})')
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
            except ExtractorError:
                if quality == 'nq':
                    raise
                self.report_warning(
                    f'Track {track_id}: lossless unavailable, falling back to nq')

        codec = di.get('codec') or 'mp3'
        ext = 'flac' if codec == 'flac' else 'mp3'
        artists = [a.get('name') for a in meta.get('artists', []) if a.get('name')]
        album = meta.get('album') or {}

        return {
            'id': str(track_id),
            'title': meta.get('title'),
            'artist': ', '.join(artists) or None,
            'album': album.get('title') if isinstance(album, dict) else None,
            'album_id': str(album.get('id')) if isinstance(album, dict) and album.get('id') else None,
            'track_number': int_or_none(meta.get('numberInSet')),
            'duration': float_or_none(meta.get('durationMs'), 1000),
            'formats': [{
                'url': di['url'],
                'ext': ext,
                'format_id': f'{codec}-{di.get("bitrate") or "?"}k',
                'abr': int_or_none(di.get('bitrate')),
                'vcodec': 'none',
            }],
        }


class YandexMusicV2PlaylistIE(InfoExtractor):
    """Shared Yandex Music playlist: https://music.yandex.ru/playlists/<uuid>"""

    IE_NAME = 'yandexmusicv2:playlist'
    _VALID_URL = r'^https?://music\.yandex\.[a-z.]+/playlists/(?P<id>[0-9a-fA-F-]{36})'

    def _parse_preloaded_playlist(self, webpage):
        """Extract the preloaded playlist object from the Next.js RSC payload."""
        chunks = re.findall(
            r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', webpage)
        payload = ''.join(json.loads('"' + c + '"') for c in chunks)
        i = payload.find('"preloadedPlaylistByUuid":')
        if i == -1:
            raise ExtractorError(
                'Yandex Music: playlist data not found in page '
                '(is the playlist public and are cookies valid?)', expected=True)
        j = payload.find('{', i)
        depth = 0
        end = None
        for k in range(j, len(payload)):
            c = payload[k]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    end = k + 1
                    break
        if end is None:
            raise ExtractorError('Yandex Music: malformed playlist data')
        return json.loads(payload[j:end])

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

        entries = []
        for t in data.get('tracks', []):
            track_id = t.get('id')
            album_id = t.get('albumId')
            if not track_id:
                continue
            track_url = (f'https://music.yandex.ru/album/{album_id}/track/{track_id}'
                         if album_id
                         else f'https://music.yandex.ru/track/{track_id}')
            entries.append(self.url_result(
                track_url, ie_key=YandexMusicTrackIE.ie_key()))

        return {
            '_type': 'playlist',
            'id': playlist_uuid,
            'title': data.get('title'),
            'description': data.get('description'),
            'entries': entries,
            'playlist_count': data.get('trackCount') or len(entries),
        }
