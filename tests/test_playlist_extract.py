#!/usr/bin/env python3
"""
Unit tests for the playlist / album / artist-albums extractor families
(previously zero line coverage — the plugin's flagship features):

* ``YandexMusicV2PlaylistIE`` (shared playlists, charts, liked):
  full preload, short preload -> API fallback (with the count updated
  from the API response), missing preload -> ``/playlist/<uuid>``
  fallback (full uuid incl. its lk./pl./ch. prefix), ``available=False``,
  entry URL construction (with and without ``albumId`` — chart entries
  have no albumId),
* ``YandexMusicPlaylistIE`` (users/<login>/playlists):
  the ``preloadedPlaylist`` preload key, the no-preload fallback
  (users API first, with the lk./pl./ch. -> numeric kind map and the
  ``expect_uuid`` guard), the numeric-kind form,
* ``YandexMusicAlbumIE``: flat ``tracks``, per-volume ``volumes``
  (multi-disc), ``available=False``, missing preload,
* ``YandexMusicArtistAlbumsIE``: ``/artists/<id>/direct-albums``,
  ``result``-wrapped responses, nonexistent artists.

Run:  python3 tests/test_playlist_extract.py   (or: pytest tests/)
"""

import importlib.util
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

HERE = os.path.dirname(__file__)
PLUGIN_PATH = os.path.join(HERE, '..', 'yt_dlp_plugins', 'extractor',
                           'yandex_music_v2.py')

UUID = '8edd98ff-3426-4a2c-918a-e03a43f31de0'


def load_plugin():
    spec = importlib.util.spec_from_file_location('ymv2', PLUGIN_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_page(*fragments):
    """Build a synthetic RSC page from raw payload fragments.

    Each fragment is a (unescaped) slice of the flight payload; the page
    embeds them the way Next.js does: as JSON-encoded strings inside
    ``self.__next_f.push([1, "..."])`` calls.
    """
    return ''.join(
        f'self.__next_f.push([1,"{json.dumps(frag)[1:-1]}"])' for frag in fragments)


def preload(key, obj):
    return f'"{key}":{json.dumps(obj)}'


def make_ie(mod, cls, page_html, api_responses=None):
    """An extractor instance with a scripted page and API responses.

    ``api_responses``: {url-prefix: response}; an unmatched URL returns
    None (treated as a failed fetch by the extractors).
    """
    ie = cls.__new__(cls)
    ie._download_webpage = lambda *a, **k: page_html
    calls = []

    def dj(url, video_id, *a, **k):
        calls.append(url)
        # longest prefix wins (…/artists/5 must not shadow …/artists/5/x)
        for prefix in sorted((api_responses or {}), key=len, reverse=True):
            if url.startswith(prefix):
                return api_responses[prefix]
        return None

    ie._download_json = dj
    ie.warnings = []
    ie.report_warning = lambda msg: ie.warnings.append(msg)
    return ie, calls


def expect_error(fn, needle):
    from yt_dlp.utils import ExtractorError
    try:
        fn()
    except ExtractorError as e:
        assert needle in str(e), f'{e!r} does not contain {needle!r}'
        return
    raise AssertionError(f'expected ExtractorError containing {needle!r}')


def test_playlist_full_preload(mod):
    """Full preload: entries + count, no API call, both entry URL forms."""
    tracks = [
        {'id': 111, 'albumId': 555},
        {'id': 222},  # no albumId (chart-style entry)
    ]
    page = make_page(preload('preloadedPlaylistByUuid',
                             {'title': 'PL', 'trackCount': 2, 'tracks': tracks,
                              'uid': 1, 'kind': 1003, 'playlistUuid': f'pl.{UUID}'}))
    ie, calls = make_ie(mod, mod.YandexMusicV2PlaylistIE, page)
    info = ie._real_extract(f'https://music.yandex.ru/playlists/pl.{UUID}')
    assert info['title'] == 'PL'
    assert info['playlist_count'] == 2
    assert ie.warnings == [] and calls == []
    assert [e['url'] for e in info['entries']] == [
        'https://music.yandex.ru/album/555/track/111',
        'https://music.yandex.ru/track/222',  # bare /track/<id> without albumId
    ]
    print('  ok: full preload — entries (both URL forms), count, no API call')


def test_playlist_short_preload_fallback(mod):
    """Short preload -> users-API fallback; the count must come from the
    API response, not the stale preload (regression: playlist_count could
    end up SMALLER than the number of entries)."""
    full = [{'id': i, 'albumId': i} for i in range(1, 6)]
    page = make_page(preload('preloadedPlaylistByUuid',
                             {'title': 'PL', 'trackCount': 5,
                              'tracks': full[:2], 'uid': 1, 'kind': 1003,
                              'playlistUuid': f'pl.{UUID}'}))
    api = {'playlistUuid': f'pl.{UUID}', 'trackCount': 5, 'tracks': full}
    ie, calls = make_ie(
        mod, mod.YandexMusicV2PlaylistIE, page,
        {'https://api.music.yandex.ru/users/1/playlists/1003': api})
    info = ie._real_extract(f'https://music.yandex.ru/playlists/pl.{UUID}')
    assert len(info['entries']) == 5
    assert info['playlist_count'] == 5, 'stale preload count kept'
    assert any('falling back' in w for w in ie.warnings)
    print('  ok: short preload -> API fallback, count updated from the API')


def test_playlist_missing_preload_fallback(mod):
    """No preload at all (page rendered a shell) -> /playlist/<uuid> with
    the FULL uuid (prefix included)."""
    page = make_page('"pathname":"/playlists/x"')
    # base class: bare uuid
    api = {'playlistUuid': UUID, 'trackCount': 1,
           'tracks': [{'id': 1, 'albumId': 2}]}
    ie, calls = make_ie(
        mod, mod.YandexMusicV2PlaylistIE, page,
        {'https://api.music.yandex.ru/playlist/' + UUID: api})
    info = ie._real_extract(f'https://music.yandex.ru/playlists/{UUID}')
    assert len(info['entries']) == 1
    assert calls == [f'https://api.music.yandex.ru/playlist/{UUID}']
    # liked subclass: the lk. prefix must survive into the API URL (and the
    # API echoes it back in playlistUuid — verified live)
    api_lk = dict(api, playlistUuid=f'lk.{UUID}')
    ie2, calls2 = make_ie(
        mod, mod.YandexMusicV2LikedPlaylistIE, page,
        {'https://api.music.yandex.ru/playlist/lk.' + UUID: api_lk})
    info2 = ie2._real_extract(f'https://music.yandex.ru/playlists/lk.{UUID}')
    assert len(info2['entries']) == 1
    assert calls2 == [f'https://api.music.yandex.ru/playlist/lk.{UUID}']
    print('  ok: missing preload -> /playlist/<uuid> fallback (full uuid)')


def test_playlist_unavailable(mod):
    page = make_page(preload('preloadedPlaylistByUuid',
                             {'title': 'PL', 'available': False,
                              'tracks': []}))
    ie, calls = make_ie(mod, mod.YandexMusicV2PlaylistIE, page)
    expect_error(lambda: ie._real_extract(
        f'https://music.yandex.ru/playlists/{UUID}'), 'not available')
    print('  ok: available=False -> clean error')


def test_users_playlist_preload(mod):
    """The users form preloads under a different key (preloadedPlaylist)."""
    page = make_page(preload('preloadedPlaylist',
                             {'title': 'UP', 'trackCount': 1,
                              'tracks': [{'id': 7, 'albumId': 8}]}))
    ie, calls = make_ie(mod, mod.YandexMusicPlaylistIE, page)
    info = ie._real_extract(
        f'https://music.yandex.ru/users/s.sofeychuk/playlists/pl.{UUID}')
    assert info['title'] == 'UP'
    assert len(info['entries']) == 1
    assert calls == []  # no API needed
    print('  ok: users-playlist preload (preloadedPlaylist key)')


def test_users_playlist_no_preload_uuid_mirror(mod):
    """No preload (private playlist) -> users API first, with the
    lk. -> 3 kind mapping derived from the uuid prefix."""
    page = make_page('"uid":211883909')
    users_api = {'playlistUuid': f'lk.{UUID}', 'trackCount': 1,
                 'tracks': [{'id': 7}]}
    ie, calls = make_ie(
        mod, mod.YandexMusicPlaylistIE, page,
        {'https://api.music.yandex.ru/users/211883909/playlists/3': users_api})
    info = ie._real_extract(
        f'https://music.yandex.ru/users/s.sofeychuk/playlists/lk.{UUID}')
    assert len(info['entries']) == 1
    assert calls == ['https://api.music.yandex.ru/users/211883909/playlists/3']
    print('  ok: users no-preload — users API first (lk. -> kind 3)')


def test_users_playlist_expect_uuid_guard(mod):
    """The users API may return a DIFFERENT playlist (wrong uid in the
    page) — the expect_uuid guard must skip it and use /playlist/<uuid>."""
    page = make_page('"uid":211883909')
    wrong = {'playlistUuid': 'pl.00000000-0000-0000-0000-000000000000',
             'trackCount': 1, 'tracks': [{'id': 1}]}
    right = {'playlistUuid': f'lk.{UUID}', 'trackCount': 1,
             'tracks': [{'id': 2}]}
    ie, calls = make_ie(
        mod, mod.YandexMusicPlaylistIE, page,
        {'https://api.music.yandex.ru/users/211883909/playlists/3': wrong,
         'https://api.music.yandex.ru/playlist/lk.' + UUID: right})
    info = ie._real_extract(
        f'https://music.yandex.ru/users/s.sofeychuk/playlists/lk.{UUID}')
    assert info['entries'][0]['url'] == 'https://music.yandex.ru/track/2'
    assert len(calls) == 2, 'the mismatching users-API result must be skipped'
    print('  ok: expect_uuid guard — mismatching users-API result skipped')


def test_users_playlist_numeric_kind(mod):
    """Numeric-kind form with no preload: the kind IS the id (regression:
    it used to only try /playlist/<numeric>, which 404s)."""
    page = make_page('"uid":211883909')
    # the users API echoes the real (prefixed) playlistUuid — it must NOT be
    # held against the numeric id (expect_uuid is disabled for this form)
    users_api = {'playlistUuid': f'lk.{UUID}', 'trackCount': 1,
                 'tracks': [{'id': 9}]}
    ie, calls = make_ie(
        mod, mod.YandexMusicPlaylistIE, page,
        {'https://api.music.yandex.ru/users/211883909/playlists/3': users_api})
    info = ie._real_extract('https://music.yandex.ru/users/s.sofeychuk/playlists/3')
    assert len(info['entries']) == 1
    assert calls == ['https://api.music.yandex.ru/users/211883909/playlists/3']
    print('  ok: users numeric-kind form — kind derived from the id')


def test_album_flat(mod):
    page = make_page(preload('preloadedAlbum',
                             {'title': 'AL', 'trackCount': 2,
                              'tracks': [{'id': 1}, {'id': 2}],
                              'coverUri': 'https://x/%%/c.jpg'}))
    ie, calls = make_ie(mod, mod.YandexMusicAlbumIE, page)
    info = ie._real_extract('https://music.yandex.ru/album/586554')
    assert info['title'] == 'AL'
    assert [e['url'] for e in info['entries']] == [
        'https://music.yandex.ru/album/586554/track/1',
        'https://music.yandex.ru/album/586554/track/2',
    ]
    assert info['thumbnail'] == 'https://x/1000x1000/c.jpg'
    assert info['playlist_count'] == 2 and calls == []
    print('  ok: album flat tracks + cover normalization')


def test_album_volumes(mod):
    """Multi-disc: no flat tracks key, per-volume arrays instead."""
    page = make_page(preload('preloadedAlbum',
                             {'title': 'BOX', 'trackCount': 3,
                              'volumes': [[{'id': 1}, {'id': 2}], [{'id': 3}]]}))
    ie, calls = make_ie(mod, mod.YandexMusicAlbumIE, page)
    info = ie._real_extract('https://music.yandex.ru/album/298151')
    assert [e['url'].rsplit('/', 1)[1] for e in info['entries']] == ['1', '2', '3']
    assert info['playlist_count'] == 3 and ie.warnings == []
    print('  ok: album volumes (multi-disc)')


def test_album_unavailable_and_missing(mod):
    page = make_page(preload('preloadedAlbum',
                             {'title': 'AL', 'available': False,
                              'tracks': []}))
    ie, _ = make_ie(mod, mod.YandexMusicAlbumIE, page)
    expect_error(lambda: ie._real_extract(
        'https://music.yandex.ru/album/586554'), 'not available')
    ie2, _ = make_ie(mod, mod.YandexMusicAlbumIE, make_page('"x":1'))
    expect_error(lambda: ie2._real_extract(
        'https://music.yandex.ru/album/586554'), 'not found in page')
    print('  ok: album available=False / missing preload -> clean errors')


def test_artist_albums(mod):
    artist = {'artist': {'id': 191175, 'name': 'AC/DC'}}
    albums = {'albums': [{'id': 1, 'title': 'A'}, {'id': 2, 'title': 'B'}],
              'pager': {'total': 2}}
    ie, calls = make_ie(mod, mod.YandexMusicArtistAlbumsIE, '', {
        'https://api.music.yandex.ru/artists/191175': artist,
        'https://api.music.yandex.ru/artists/191175/direct-albums': albums})
    info = ie._real_extract('https://music.yandex.ru/artist/191175/albums')
    assert info['title'] == 'AC/DC — albums'
    assert [e['url'] for e in info['entries']] == [
        'https://music.yandex.ru/album/1', 'https://music.yandex.ru/album/2']
    assert [e.get('title') for e in info['entries']] == ['A', 'B']
    assert info['playlist_count'] == 2
    print('  ok: artist albums (direct-albums, entries with titles)')


def test_artist_albums_wrapped_result(mod):
    artist = {'artist': {'id': 191175, 'name': 'AC/DC'}}
    albums = {'result': {'albums': [{'id': 1, 'title': 'A'}]}}
    ie, _ = make_ie(mod, mod.YandexMusicArtistAlbumsIE, '', {
        'https://api.music.yandex.ru/artists/191175': artist,
        'https://api.music.yandex.ru/artists/191175/direct-albums': albums})
    info = ie._real_extract('https://music.yandex.ru/artist/191175/albums')
    assert len(info['entries']) == 1
    print('  ok: artist albums — result-wrapped response unwrapped')


def test_artist_albums_not_found(mod):
    artist = {'artist': {'error': 'not-found'}}
    ie, calls = make_ie(mod, mod.YandexMusicArtistAlbumsIE, '', {
        'https://api.music.yandex.ru/artists/999999999': artist})
    expect_error(lambda: ie._real_extract(
        'https://music.yandex.ru/artist/999999999/albums'), 'not found')
    assert len(calls) == 1, 'no album request for a nonexistent artist'
    print('  ok: artist albums — nonexistent artist -> clean error')


def main():
    print('playlist / album / artist-albums extractor tests:')
    mod = load_plugin()
    test_playlist_full_preload(mod)
    test_playlist_short_preload_fallback(mod)
    test_playlist_missing_preload_fallback(mod)
    test_playlist_unavailable(mod)
    test_users_playlist_preload(mod)
    test_users_playlist_no_preload_uuid_mirror(mod)
    test_users_playlist_expect_uuid_guard(mod)
    test_users_playlist_numeric_kind(mod)
    test_album_flat(mod)
    test_album_volumes(mod)
    test_album_unavailable_and_missing(mod)
    test_artist_albums(mod)
    test_artist_albums_wrapped_result(mod)
    test_artist_albums_not_found(mod)
    print('all tests passed')


if __name__ == '__main__':
    main()
