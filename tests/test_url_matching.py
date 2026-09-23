#!/usr/bin/env python3
"""
Unit tests for the plugin's extractor URL matching.

Verifies that:
  1. each extractor's ``_VALID_URL`` matches its canonical URL shapes
     (all supported TLDs, with/without query strings),
  2. extractors do NOT cross-match each other's URL spaces (e.g. a
     ``lk.``-prefixed playlist must not match the shared-playlist
     extractor and vice versa),
  3. with the plugin loaded, yt-dlp's actual extractor list resolves each
     URL to exactly the intended extractor (and the shadowed built-ins —
     album, artist:tracks — are replaced by the plugin versions).

Run:  python3 tests/test_url_matching.py   (or: pytest tests/)
"""

import importlib.util
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

HERE = os.path.dirname(__file__)
PLUGIN_PATH = os.path.join(HERE, '..', 'yt_dlp_plugins', 'extractor',
                           'yandex_music_v2.py')

TLDs = ['ru', 'kz', 'ua', 'by', 'com']

TRACK_URL = 'https://music.yandex.ru/album/586554/track/123456789'
TRACK_URL_BARE = 'https://music.yandex.ru/track/123456789'
PLAYLIST_URL = 'https://music.yandex.ru/playlists/8edd98ff-3426-4a2c-918a-e03a43f31de0'
PLAYLIST_PL_URL = 'https://music.yandex.ru/playlists/pl.8edd98ff-3426-4a2c-918a-e03a43f31de0'
CHART_URL = 'https://music.yandex.ru/playlists/ch.448df3eb-daee-408a-a60a-252259db2f3b'
LIKED_URL = 'https://music.yandex.ru/playlists/lk.8edd98ff-3426-4a2c-918a-e03a43f31de0'
USERS_PLAYLIST_URL = 'https://music.yandex.ru/users/s.sofeychuk/playlists/pl.8edd98ff-3426-4a2c-918a-e03a43f31de0'
USERS_PLAYLIST_LK_URL = 'https://music.yandex.ru/users/s.sofeychuk/playlists/lk.8edd98ff-3426-4a2c-918a-e03a43f31de0'
USERS_PLAYLIST_KIND_URL = 'https://music.yandex.ru/users/s.sofeychuk/playlists/3'
ARTIST_URL = 'https://music.yandex.ru/artist/20258331'
ARTIST_TRACKS_URL = 'https://music.yandex.ru/artist/20258331/tracks'
ARTIST_ALBUMS_URL = 'https://music.yandex.ru/artist/20258331/albums'
ALBUM_URL = 'https://music.yandex.ru/album/586554'


def load_plugin():
    """Import the plugin module (requires yt_dlp to be importable)."""
    spec = importlib.util.spec_from_file_location('ymv2', PLUGIN_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_regex_matching():
    """Each _VALID_URL matches its own URLs and no other extractor's."""
    mod = load_plugin()
    ies = {
        'track': mod.YandexMusicTrackIE,
        'playlist': mod.YandexMusicV2PlaylistIE,
        'liked': mod.YandexMusicV2LikedPlaylistIE,
        'users_playlist': mod.YandexMusicPlaylistIE,
        'artist': mod.YandexMusicArtistIE,
        'artist_tracks': mod.YandexMusicArtistTracksIE,
        'artist_albums': mod.YandexMusicArtistAlbumsIE,
        'album': mod.YandexMusicAlbumIE,
    }
    patterns = {name: re.compile(cls._VALID_URL) for name, cls in ies.items()}

    def matches(name, url):
        return patterns[name].match(url) is not None

    # (extractor, urls that must match, urls that must NOT match)
    cases = {
        'track': (
            [TRACK_URL, TRACK_URL_BARE]
            + [f'https://music.yandex.{tld}/album/586554/track/123456789'
               for tld in TLDs],
            [PLAYLIST_URL, LIKED_URL, ARTIST_URL, ARTIST_TRACKS_URL, ALBUM_URL],
        ),
        'playlist': (
            [PLAYLIST_URL, PLAYLIST_PL_URL, CHART_URL]
            + [f'https://music.yandex.{tld}/playlists/8edd98ff-3426-4a2c-918a-e03a43f31de0'
               for tld in TLDs],
            [LIKED_URL, TRACK_URL, ARTIST_URL, ALBUM_URL, USERS_PLAYLIST_URL],
        ),
        'liked': (
            [LIKED_URL]
            + [f'https://music.yandex.{tld}/playlists/lk.8edd98ff-3426-4a2c-918a-e03a43f31de0'
               for tld in TLDs],
            [PLAYLIST_URL, TRACK_URL, ARTIST_URL, ALBUM_URL, USERS_PLAYLIST_LK_URL],
        ),
        'users_playlist': (
            [USERS_PLAYLIST_URL, USERS_PLAYLIST_LK_URL, USERS_PLAYLIST_KIND_URL,
             'https://music.yandex.ru/users/s.sofeychuk/playlists/1003']
            + [f'https://music.yandex.{tld}/users/s.sofeychuk/playlists/3'
               for tld in TLDs],
            [LIKED_URL, PLAYLIST_URL, CHART_URL, TRACK_URL, ARTIST_URL, ALBUM_URL],
        ),
        'artist': (
            [ARTIST_URL]
            + [f'https://music.yandex.{tld}/artist/20258331' for tld in TLDs],
            [ARTIST_TRACKS_URL, ARTIST_ALBUMS_URL, TRACK_URL, ALBUM_URL,
             PLAYLIST_URL],
        ),
        'artist_tracks': (
            [ARTIST_TRACKS_URL]
            + [f'https://music.yandex.{tld}/artist/20258331/tracks' for tld in TLDs],
            [ARTIST_URL, ARTIST_ALBUMS_URL, TRACK_URL, ALBUM_URL, PLAYLIST_URL],
        ),
        'artist_albums': (
            [ARTIST_ALBUMS_URL]
            + [f'https://music.yandex.{tld}/artist/20258331/albums' for tld in TLDs],
            [ARTIST_URL, ARTIST_TRACKS_URL, TRACK_URL, ALBUM_URL, PLAYLIST_URL],
        ),
        'album': (
            [ALBUM_URL]
            + [f'https://music.yandex.{tld}/album/586554' for tld in TLDs],
            [TRACK_URL, ARTIST_URL, ARTIST_TRACKS_URL, PLAYLIST_URL],
        ),
    }

    for name, (good, bad) in cases.items():
        for url in good:
            assert matches(name, url), f'{name} should match {url}'
        for url in bad:
            assert not matches(name, url), f'{name} must not match {url}'
    print('  ok: per-extractor regex matching (incl. TLD variants)')

    # query strings / fragments must not break matching
    for name, url in [
        ('album', ALBUM_URL + '?utm_source=web&utm_medium=copy_link'),
        ('artist', ARTIST_URL + '?utm_source=web'),
        ('liked', LIKED_URL + '#fragment'),
        ('playlist', CHART_URL + '?utm_source=web&utm_medium=copy_link'),
        ('users_playlist', USERS_PLAYLIST_KIND_URL + '?x=1'),
        ('artist_albums', ARTIST_ALBUMS_URL + '?utm_source=web'),
    ]:
        assert matches(name, url), f'{name} should match {url}'
    print('  ok: query strings / fragments do not break matching')

    # malformed URLs must not match
    malformed = {
        'track': ['https://music.yandex.ru/track/abc',
                  'https://music.yandex.ru/track/',
                  'https://music.yandex.ru/track/123456789x',
                  'https://music.yandex.ru/track/123456789/extra'],
        'album': ['https://music.yandex.ru/album/abc',
                  'https://music.yandex.ru/album/',
                  'https://music.yandex.ru/album/586554/extra'],
        'artist': ['https://music.yandex.ru/artist/abc',
                   'https://music.yandex.ru/artist/'],
        'artist_albums': ['https://music.yandex.ru/artist/abc/albums',
                          'https://music.yandex.ru/artist/20258331/albums/extra',
                          'https://music.yandex.ru/artist/20258331/album'],
        'liked': ['https://music.yandex.ru/playlists/lk.notauuid',
                  'https://music.yandex.ru/playlists/lk.8edd98ff-3426-4a2c-918a-e03a43f31de',
                  'https://music.yandex.ru/playlists/8edd98ff-3426-4a2c-918a-e03a43f31de0'],
        'playlist': ['https://music.yandex.ru/playlists/lk.8edd98ff-3426-4a2c-918a-e03a43f31de0',
                     'https://music.yandex.ru/playlists/short',
                     'https://music.yandex.ru/playlists/ch.short',
                     'https://music.yandex.ru/playlists/ch.448df3eb-daee-408a-a60a-252259db2f3b/extra'],
        'users_playlist': ['https://music.yandex.ru/users/s.sofeychuk/playlists/',
                           'https://music.yandex.ru/users/s.sofeychuk/playlists/xyz',
                           'https://music.yandex.ru/users/s.sofeychuk/playlists/lk.notauuid',
                           'https://music.yandex.ru/users/s.sofeychuk/albums/3'],
    }
    for name, urls in malformed.items():
        for url in urls:
            assert not matches(name, url), f'{name} must not match {url}'
    print('  ok: malformed URLs are rejected')


def test_full_resolver():
    """With the plugin loaded, yt-dlp's extractor list must resolve each URL
    to exactly one extractor — the intended one — and the shadowed
    built-ins must be gone (replaced by the plugin classes)."""
    try:
        from yt_dlp.plugins import load_all_plugins
    except ImportError as e:
        print(f'  skip: yt_dlp not importable ({e})')
        return
    load_all_plugins()
    from yt_dlp.extractor import gen_extractor_classes

    extractors = list(gen_extractor_classes())
    by_name = {cls.IE_NAME: cls for cls in extractors}

    # shadowing: the plugin classes must be the ones in the list
    for name in ('yandexmusic:track', 'yandexmusic:album',
                 'yandexmusic:artist:tracks', 'yandexmusic:playlist',
                 'yandexmusic:artist:albums'):
        assert by_name[name].__module__.startswith('yt_dlp_plugins'), \
            f'built-in {name} was not shadowed by the plugin'
    for name in ('yandexmusicv2:playlist', 'yandexmusicv2:liked',
                 'yandexmusicv2:artist'):
        assert name in by_name, f'{name} missing from extractor list'
    print('  ok: shadowing + plugin extractors present in extractor list')

    expected = {
        TRACK_URL: 'yandexmusic:track',
        TRACK_URL_BARE: 'yandexmusic:track',
        PLAYLIST_URL: 'yandexmusicv2:playlist',
        PLAYLIST_PL_URL: 'yandexmusicv2:playlist',
        CHART_URL: 'yandexmusicv2:playlist',
        LIKED_URL: 'yandexmusicv2:liked',
        USERS_PLAYLIST_URL: 'yandexmusic:playlist',
        USERS_PLAYLIST_LK_URL: 'yandexmusic:playlist',
        USERS_PLAYLIST_KIND_URL: 'yandexmusic:playlist',
        ARTIST_URL: 'yandexmusicv2:artist',
        ARTIST_TRACKS_URL: 'yandexmusic:artist:tracks',
        ARTIST_ALBUMS_URL: 'yandexmusic:artist:albums',
        ALBUM_URL: 'yandexmusic:album',
        ALBUM_URL + '?utm_source=web&utm_medium=copy_link': 'yandexmusic:album',
    }
    for url, want in expected.items():
        # GenericIE is the catch-all fallback and matches every URL; the
        # intended extractor must be the only real (non-generic) match
        hits = [cls.IE_NAME for cls in extractors
                if cls.suitable(url) and cls.IE_NAME != 'generic']
        assert hits == [want], f'{url}: matched {hits}, expected exactly [{want!r}]'
    print('  ok: every URL resolves to exactly the intended extractor')


def main():
    print('URL matching tests:')
    test_regex_matching()
    test_full_resolver()
    print('all tests passed')


if __name__ == '__main__':
    main()
