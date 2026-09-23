#!/usr/bin/env python3
"""
Unit tests for the track extractor's quality fallback and metadata
mapping:

* lossless unavailable -> warn + fall back to nq; both unavailable ->
  the nq error is re-raised (not swallowed),
* the final info dict: albums[0]/trackPosition mapping, cover
  normalization (``%%`` placeholder, protocol-relative URLs),
  date/upload_date from the album year, formats ext from the codec,
* unavailable tracks -> clean ExtractorError(expected=True),
* ``_normalize_cover`` in isolation.

Run:  python3 tests/test_track_extract.py   (or: pytest tests/)
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


def make_ie(mod, meta, di_script, params=None):
    """di_script: list of (return-value | exception) per quality attempt."""
    ie = mod.YandexMusicTrackIE.__new__(mod.YandexMusicTrackIE)
    ie._url = 'https://music.yandex.ru/track/123'
    ie._downloader = FakeDownloader(params)
    ie.warnings = []
    ie.report_warning = lambda msg: ie.warnings.append(msg)
    ie._warn_if_preview_served = lambda *a: None

    mod._get_track_meta = lambda ie_, tid: meta
    calls = {'n': 0}

    def di(ie_, tid, quality):
        action = di_script[min(calls['n'], len(di_script) - 1)]
        calls['n'] += 1
        if isinstance(action, Exception):
            raise action
        return action

    mod._get_download_info = di
    return ie, calls


META = {
    'title': 'Song',
    'durationMs': 200000,
    'artists': [{'name': 'Artist One'}, {'name': 'Artist Two'}],
    'albums': [{
        'id': 555,
        'title': 'Album Title',
        'year': 2021,
        'trackCount': 12,
        'artists': [{'name': 'Album Artist'}],
        'trackPosition': {'index': 3, 'volume': 2},
        'coverUri': 'https://yandex.ru/images/cover/%%/big.jpg',
    }],
    'coverUri': None,
}
DI_FLAC = {'url': 'https://strm/flac', 'quality': 'lossless', 'bitrate': 900,
           'codec': 'flac'}
DI_MP3 = {'url': 'https://strm/mp3', 'quality': 'nq', 'bitrate': 192,
          'codec': 'mp3'}


def test_quality_fallback(mod):
    """lossless fails -> warning + nq result."""
    from yt_dlp.utils import ExtractorError
    ie, calls = make_ie(mod, META,
                        [ExtractorError('lossless unavailable', expected=True),
                         DI_MP3])
    info = ie._real_extract(ie._url)
    assert calls['n'] == 2
    assert len(ie.warnings) == 1 and 'falling back to nq' in ie.warnings[0]
    assert info['formats'][0]['url'] == 'https://strm/mp3'
    assert info['formats'][0]['ext'] == 'mp3'
    print('  ok: lossless unavailable -> warn + nq fallback')


def test_both_qualities_fail(mod):
    """both fail -> the nq error propagates (not swallowed)."""
    from yt_dlp.utils import ExtractorError
    ie, calls = make_ie(mod, META,
                        [ExtractorError('lossless gone', expected=True),
                         ExtractorError('nq gone', expected=True)])
    try:
        ie._real_extract(ie._url)
        raise AssertionError('expected ExtractorError')
    except ExtractorError as e:
        assert 'nq gone' in str(e)
    assert calls['n'] == 2
    print('  ok: both qualities fail -> nq error re-raised')


def test_metadata_mapping(mod):
    ie, calls = make_ie(mod, META, [DI_FLAC])
    info = ie._real_extract(ie._url)
    assert info['id'] == '123'
    assert info['title'] == 'Song'
    assert info['artist'] == 'Artist One, Artist Two'
    assert info['album'] == 'Album Title'
    assert info['album_id'] == '555'
    assert info['album_artist'] == 'Album Artist'
    assert info['track_number'] == 3
    assert info['track_total'] == 12
    assert info['disc_number'] == 2
    assert info['date'] == '20210101'
    assert info['upload_date'] == '20210101'
    # '%%' size placeholder -> 1000x1000
    assert info['thumbnail'] == 'https://yandex.ru/images/cover/1000x1000/big.jpg'
    assert info['duration'] == 200.0
    fmt = info['formats'][0]
    assert fmt['url'] == 'https://strm/flac'
    assert fmt['ext'] == 'flac'
    assert fmt['format_id'] == 'flac-900k'
    assert fmt['abr'] == 900
    print('  ok: metadata mapping (albums[0], position, cover, date, formats)')


def test_track_unavailable(mod):
    from yt_dlp.utils import ExtractorError
    ie, calls = make_ie(mod, dict(META, available=False), [DI_FLAC])
    try:
        ie._real_extract(ie._url)
        raise AssertionError('expected ExtractorError')
    except ExtractorError as e:
        assert e.expected is True
        assert 'not available' in str(e)
    assert calls['n'] == 0, 'no stream requests for an unavailable track'
    print('  ok: unavailable track -> clean ExtractorError(expected)')


def test_normalize_cover(mod):
    n = mod._normalize_cover
    assert n(None) is None
    assert n('') is None
    assert n('http://a/b') == 'http://a/b'
    assert n('https://a/b') == 'https://a/b'
    # protocol-relative: must NOT become https:////...
    assert n('//yandex.ru/a/b') == 'https://yandex.ru/a/b'
    # bare host (no scheme)
    assert n('yandex.ru/a/b') == 'https://yandex.ru/a/b'
    # %% placeholder
    assert n('https://a/%%/b') == 'https://a/1000x1000/b'
    assert n('//a/%%/b') == 'https://a/1000x1000/b'
    print('  ok: _normalize_cover (protocol-relative, bare host, %%)')


def main():
    print('track extraction tests:')
    mod = load_plugin()
    test_quality_fallback(mod)
    test_both_qualities_fail(mod)
    test_metadata_mapping(mod)
    test_track_unavailable(mod)
    test_normalize_cover(mod)
    print('all tests passed')


if __name__ == '__main__':
    main()
