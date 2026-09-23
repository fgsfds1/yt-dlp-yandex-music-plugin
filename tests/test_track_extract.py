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
import re
import sys
from contextlib import contextmanager

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


@contextmanager
def make_ie(mod, meta, di_script, params=None):
    """di_script: list of (return-value | exception) per quality attempt.
    Context manager: the module-level patches are restored on exit (the
    pytest ``mod`` fixture is session-scoped — a leak would reorder the
    suite)."""
    ie = mod.YandexMusicTrackIE.__new__(mod.YandexMusicTrackIE)
    ie._url = 'https://music.yandex.ru/track/123'
    ie._downloader = FakeDownloader(params)
    ie.warnings = []
    ie.report_warning = lambda msg: ie.warnings.append(msg)
    ie._warn_if_preview_served = lambda *a: None

    old_meta, old_di = mod._get_track_meta, mod._get_download_info
    calls = {'n': 0}

    def di(ie_, tid, quality):
        action = di_script[min(calls['n'], len(di_script) - 1)]
        calls['n'] += 1
        if isinstance(action, Exception):
            raise action
        return action

    mod._get_track_meta = lambda ie_, tid: meta
    mod._get_download_info = di
    try:
        yield ie, calls
    finally:
        mod._get_track_meta, mod._get_download_info = old_meta, old_di


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
    with make_ie(mod, META,
                 [ExtractorError('lossless unavailable', expected=True),
                  DI_MP3]) as (ie, calls):
        info = ie._real_extract(ie._url)
    assert calls['n'] == 2
    assert len(ie.warnings) == 1 and 'falling back to nq' in ie.warnings[0]
    # the warning carries the underlying reason (debugging aid)
    assert 'lossless unavailable' in ie.warnings[0]
    assert info['formats'][0]['url'] == 'https://strm/mp3'
    assert info['formats'][0]['ext'] == 'mp3'
    print('  ok: lossless unavailable -> warn (with reason) + nq fallback')


def test_both_qualities_fail(mod):
    """both fail -> the nq error propagates (not swallowed)."""
    from yt_dlp.utils import ExtractorError
    with make_ie(mod, META,
                 [ExtractorError('lossless gone', expected=True),
                  ExtractorError('nq gone', expected=True)]) as (ie, calls):
        try:
            ie._real_extract(ie._url)
            raise AssertionError('expected ExtractorError')
        except ExtractorError as e:
            assert 'nq gone' in str(e)
    assert calls['n'] == 2
    print('  ok: both qualities fail -> nq error re-raised')


def test_metadata_mapping(mod):
    with make_ie(mod, META, [DI_FLAC]) as (ie, calls):
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
    with make_ie(mod, dict(META, available=False), [DI_FLAC]) as (ie, calls):
        try:
            ie._real_extract(ie._url)
            raise AssertionError('expected ExtractorError')
        except ExtractorError as e:
            assert e.expected is True
            assert 'not available' in str(e)
    assert calls['n'] == 0, 'no stream requests for an unavailable track'
    print('  ok: unavailable track -> clean ExtractorError(expected)')


def test_track_not_found(mod):
    """A nonexistent track: the API returns 200 + error: not-found —
    must be a clean 'not found' error, not a stream-URL 404."""
    from yt_dlp.utils import ExtractorError
    with make_ie(mod, dict(META, error='not-found'), [DI_FLAC]) as (ie, calls):
        try:
            ie._real_extract(ie._url)
            raise AssertionError('expected ExtractorError')
        except ExtractorError as e:
            assert e.expected is True
            assert 'not found' in str(e)
    assert calls['n'] == 0, 'no stream requests for a nonexistent track'
    print('  ok: nonexistent track -> clean "not found" (no misleading 404)')


def test_get_track_meta(mod):
    """The real multipart POST: body/headers construction + response
    validation (the response is a bare JSON list)."""
    from yt_dlp.utils import ExtractorError
    ie = mod.YandexMusicTrackIE.__new__(mod.YandexMusicTrackIE)
    captured = {}

    def dj(url, video_id, note=None, data=None, headers=None, errnote=None,
           **kwargs):
        captured.update(url=url, data=data, headers=headers)
        return [{'id': '123', 'title': 't'}]

    ie._download_json = dj
    meta = mod._get_track_meta(ie, '123')
    assert meta == {'id': '123', 'title': 't'}
    assert captured['url'].endswith('/tracks')
    body = captured['data'].decode()
    assert 'name="trackIds"' in body and '123' in body
    m = re.search(r'boundary=([^;]+)', captured['headers']['Content-Type'])
    assert m and body.startswith(f'--{m.group(1)}')
    # empty / non-list responses -> clean error
    for resp in (None, [], 'garbage'):
        ie._download_json = (lambda r: (lambda *a, **k: r))(resp)
        try:
            mod._get_track_meta(ie, '123')
            raise AssertionError(f'{resp!r}: expected ExtractorError')
        except ExtractorError:
            pass
    print('  ok: _get_track_meta multipart body/headers + response validation')


def test_webpage_handle_fatal_false(mod):
    """A failed fatal=False fetch must return False, not a raw TypeError
    (the built-in override does `'…' in webpage` without a type check —
    regression: a 404 homepage crashed key refresh with a TypeError)."""
    from yt_dlp.extractor.common import InfoExtractor
    orig = InfoExtractor._download_webpage_handle
    InfoExtractor._download_webpage_handle = lambda self, *a, **k: False
    try:
        ie = mod.YandexMusicTrackIE.__new__(mod.YandexMusicTrackIE)
        assert ie._download_webpage_handle('u', 'v', fatal=False) is False
        if mod._BuiltinYandexMusicTrackIE is not InfoExtractor:
            # built-in in the MRO: its `in` check raises, the override must
            # re-raise for fatal=True (only fatal=False is swallowed)
            try:
                ie._download_webpage_handle('u', 'v', fatal=True)
                raise AssertionError('expected TypeError')
            except TypeError:
                pass
    finally:
        InfoExtractor._download_webpage_handle = orig
    print('  ok: fatal=False fetch failure -> False (no raw TypeError)')


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
    test_track_not_found(mod)
    test_get_track_meta(mod)
    test_webpage_handle_fatal_false(mod)
    test_normalize_cover(mod)
    print('all tests passed')


if __name__ == '__main__':
    main()
