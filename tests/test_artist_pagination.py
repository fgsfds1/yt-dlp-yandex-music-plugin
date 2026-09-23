#!/usr/bin/env python3
"""
Unit tests for the artist extractor's pagination loop.

``GET /artists/<id>/tracks`` is paginated (20 tracks/page, ``perPage`` is
ignored by the server), so the extractor loops until one of:

* an empty page,
* a page with no NEW tracks (the pager may ignore ``?page=``),
* the pager's ``total`` is reached,
* the safety cap (``_MAX_PAGES``) is hit.

All of that control flow is pinned here with a scripted
``_download_json``.

Run:  python3 tests/test_artist_pagination.py   (or: pytest tests/)
"""

import importlib.util
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

HERE = os.path.dirname(__file__)
PLUGIN_PATH = os.path.join(HERE, '..', 'yt_dlp_plugins', 'extractor',
                           'yandex_music_v2.py')


def load_plugin():
    spec = importlib.util.spec_from_file_location('ymv2', PLUGIN_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeDownloader:
    def __init__(self, params=None):
        self.params = params or {}


def make_ie(mod, pages, artist=None, max_pages=None, total=None):
    """pages: list of track-id lists (one per /tracks response);
    total: pager total (default: the sum of the scripted pages)."""
    ie = mod.YandexMusicArtistIE.__new__(mod.YandexMusicArtistIE)
    ie._url = 'https://music.yandex.ru/artist/42'
    ie._downloader = FakeDownloader()
    ie.warnings = []
    ie.report_warning = lambda msg: ie.warnings.append(msg)
    if max_pages is not None:
        ie._MAX_PAGES = max_pages

    artist = artist if artist is not None else {'name': 'Test Artist'}
    if total is None:
        total = sum(len(p) for p in pages)
    calls = {'n': 0, 'pages': []}

    def dj(url, *a, **k):
        if '/tracks' not in url:
            return {'artist': artist}
        calls['n'] += 1
        page = pages[min(calls['n'] - 1, len(pages) - 1)]
        calls['pages'].append(k.get('query', {}).get('page'))
        return {
            'tracks': [{'id': tid, 'albums': [{'id': 1000 + tid}]} for tid in page],
            'pager': {'total': total, 'page': calls['n'] - 1},
        }

    ie._download_json = dj
    return ie, calls


def test_normal_pagination(mod):
    """20 + 20 + 5 tracks, total=45 -> stops exactly at 45."""
    pages = [list(range(1, 21)), list(range(21, 41)), list(range(41, 46))]
    ie, calls = make_ie(mod, pages)
    info = ie._real_extract(ie._url)
    assert len(info['entries']) == 45
    assert info['playlist_count'] == 45
    assert calls['pages'] == [0, 1, 2]
    assert ie.warnings == []
    print('  ok: stops exactly at the pager total')


def test_pager_ignores_page(mod):
    """The server repeats the same page forever -> the no-new-tracks
    guard must break the loop (no infinite loop, no duplicates).
    The pager claims a large total so only the new-tracks guard can fire."""
    same = list(range(1, 21))
    ie, calls = make_ie(mod, [same], total=10000)  # every page returns `same`
    info = ie._real_extract(ie._url)
    assert len(info['entries']) == 20
    # page 0 (20 new) + page 1 (0 new) -> stop
    assert calls['pages'] == [0, 1]
    print('  ok: pager ignoring ?page= -> no infinite loop, no dupes')


def test_empty_page(mod):
    # total larger than the first page so the empty page (not the total)
    # is what stops the loop
    pages = [list(range(1, 21)), []]
    ie, calls = make_ie(mod, pages, total=100)
    info = ie._real_extract(ie._url)
    assert len(info['entries']) == 20
    assert calls['pages'] == [0, 1]
    print('  ok: empty page stops the loop')


def test_no_total_in_pager(mod):
    """pager without `total` -> run to the empty page; count = entries."""
    ie = mod.YandexMusicArtistIE.__new__(mod.YandexMusicArtistIE)
    ie._url = 'https://music.yandex.ru/artist/42'
    ie._downloader = FakeDownloader()
    ie.warnings = []
    ie.report_warning = lambda msg: ie.warnings.append(msg)
    pages = [list(range(1, 21)), list(range(21, 30)), []]
    calls = {'n': 0}

    def dj(url, *a, **k):
        if '/tracks' not in url:
            return {'artist': {'name': 'X'}}
        page = pages[min(calls['n'], len(pages) - 1)]
        calls['n'] += 1
        return {'tracks': [{'id': tid, 'albums': []} for tid in page],
                'pager': {}}  # no total

    ie._download_json = dj
    info = ie._real_extract(ie._url)
    assert len(info['entries']) == 29
    assert info['playlist_count'] == 29
    print('  ok: missing pager total -> runs to empty page, count = entries')


def test_max_pages_cap(mod):
    """A misbehaving pager (always new tracks) must hit the safety cap."""
    ie2 = mod.YandexMusicArtistIE.__new__(mod.YandexMusicArtistIE)
    ie2._url = 'https://music.yandex.ru/artist/42'
    ie2._downloader = FakeDownloader()
    ie2.warnings = []
    ie2.report_warning = lambda msg: ie2.warnings.append(msg)
    ie2._MAX_PAGES = 2

    def dj(url, *a, **k):
        if '/tracks' not in url:
            return {'artist': {'name': 'X'}}
        n = dj.n = getattr(dj, 'n', 0) + 1
        # page n has 20 brand-new tracks, total claims 10000
        return {'tracks': [{'id': 1000 + n * 20 + i, 'albums': []}
                           for i in range(20)],
                'pager': {'total': 10000}}

    ie2._download_json = dj
    info = ie2._real_extract(ie2._url)
    assert len(info['entries']) == 40  # 2 pages x 20
    assert info['playlist_count'] == 40  # clamped to what we actually have
    assert len(ie2.warnings) == 1 and 'stopped after' in ie2.warnings[0]
    print('  ok: _MAX_PAGES cap -> warning, count clamped to entries')


def test_artist_not_found(mod):
    """A nonexistent artist is 200 with artist.error == 'not-found'."""
    from yt_dlp.utils import ExtractorError
    ie, calls = make_ie(mod, [], artist={'error': 'not-found'})
    try:
        ie._real_extract(ie._url)
        raise AssertionError('expected ExtractorError')
    except ExtractorError as e:
        assert e.expected is True
        assert 'not found' in str(e)
    assert calls['n'] == 0, 'no track pages must be fetched'
    print('  ok: artist.error=not-found -> clean ExtractorError(expected)')


def test_artist_cover_string(mod):
    """Regression: cover as a plain URL string (not a dict) must not crash."""
    ie, calls = make_ie(mod, [list(range(1, 3))],
                        artist={'name': 'X', 'cover': 'https://img.yandex.net/a.jpg'})
    info = ie._real_extract(ie._url)
    assert info['thumbnail'] == 'https://img.yandex.net/a.jpg'
    print('  ok: cover as string handled')


def main():
    print('artist pagination tests:')
    mod = load_plugin()
    test_normal_pagination(mod)
    test_pager_ignores_page(mod)
    test_empty_page(mod)
    test_no_total_in_pager(mod)
    test_max_pages_cap(mod)
    test_artist_not_found(mod)
    test_artist_cover_string(mod)
    print('all tests passed')


if __name__ == '__main__':
    main()
