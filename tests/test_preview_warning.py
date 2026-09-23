#!/usr/bin/env python3
"""
Unit tests for the silent-preview-mode guard in the track extractor.

With a stale/missing Yandex session the API silently serves
`smart_preview` (13–30 s) streams for every quality level. The extractor
must warn about this, via two signals (see
`YandexMusicTrackIE._warn_if_preview_served`):

  1. the served `quality` field is a preview level,
  2. the served stream (HEAD Content-Length / served bitrate) is far
     shorter than the track's metadata duration.

Both signals must be best-effort: any network problem in the check
itself must never break the extraction.

Run:  python3 tests/test_preview_warning.py   (or: pytest tests/)
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


class FakeResponse:
    def __init__(self, headers):
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeDownloader:
    def __init__(self, headers=None, exc=None, params=None):
        self._headers = headers
        self._exc = exc
        self.params = params or {}
        self.requests = []

    def urlopen(self, req):
        self.requests.append(req)
        if self._exc is not None:
            raise self._exc
        return FakeResponse(self._headers or {})


def make_ie(mod, downloader):
    """A track-IE instance with a fake downloader and captured warnings
    (skips InfoExtractor.__init__ — the method under test only needs
    _downloader and report_warning)."""
    ie = mod.YandexMusicTrackIE.__new__(mod.YandexMusicTrackIE)
    ie._downloader = downloader
    ie.warnings = []
    ie.report_warning = lambda msg: ie.warnings.append(msg)
    return ie


# Firesuite / Doves: 276.06 s; a smart_preview of it is ~14 s at 192 kbps
META = {'durationMs': 276060}
DI_PREVIEW = {'quality': 'smart_preview', 'url': 'https://strm.example/p',
              'bitrate': 192}
DI_FULL = {'quality': 'nq', 'url': 'https://strm.example/full',
           'bitrate': 192}
PREVIEW_BYTES = 333047     # ~13.9 s @ 192 kbps
FULL_BYTES = 6626742       # ~276 s @ 192 kbps


def test_served_quality_preview_warns():
    mod = load_plugin()
    ie = make_ie(mod, FakeDownloader())
    ie._warn_if_preview_served('306559', META, DI_PREVIEW)
    assert len(ie.warnings) == 1, ie.warnings
    assert 'smart_preview' in ie.warnings[0]
    # the fast path must not issue the HEAD request
    assert ie._downloader.requests == []
    print('  ok: served quality smart_preview -> warning (no HEAD needed)')


def test_served_quality_full_no_warning():
    mod = load_plugin()
    ie = make_ie(mod, FakeDownloader(headers={'Content-Length': str(FULL_BYTES)}))
    ie._warn_if_preview_served('306559', META, DI_FULL)
    assert ie.warnings == [], ie.warnings
    print('  ok: full-length stream -> no warning')


def test_short_stream_warns():
    mod = load_plugin()
    ie = make_ie(mod, FakeDownloader(headers={'Content-Length': str(PREVIEW_BYTES)}))
    ie._warn_if_preview_served('306559', META, DI_FULL)
    assert len(ie.warnings) == 1, ie.warnings
    assert '14 s' in ie.warnings[0] and '276 s' in ie.warnings[0], ie.warnings[0]
    print('  ok: stream far shorter than metadata duration -> warning')


def test_missing_content_length_no_warning():
    mod = load_plugin()
    ie = make_ie(mod, FakeDownloader(headers={}))
    ie._warn_if_preview_served('306559', META, DI_FULL)
    assert ie.warnings == []
    # case-insensitive header lookup
    ie2 = make_ie(mod, FakeDownloader(headers={'content-length': str(FULL_BYTES)}))
    ie2._warn_if_preview_served('306559', META, DI_FULL)
    assert ie2.warnings == []
    print('  ok: missing/lowercase Content-Length handled')


def test_preview_check_disabled():
    mod = load_plugin()
    for args in (
        {'yandexmusicv2': {'preview_check': ['off']}},
        {'yandexmusic': {'track:preview_check': ['off']}},  # CLI per-IE form
        {'yandexmusic:track': {'preview_check': ['false']}},  # SDK form
    ):
        ie = make_ie(mod, FakeDownloader(params={'extractor_args': args}))
        ie._warn_if_preview_served('306559', META, DI_FULL)
        assert ie.warnings == []
        assert ie._downloader.requests == [], 'HEAD must be skipped when disabled'
    # default (no arg) still checks
    ie = make_ie(mod, FakeDownloader(headers={'Content-Length': str(PREVIEW_BYTES)}))
    ie._warn_if_preview_served('306559', META, DI_FULL)
    assert len(ie.warnings) == 1
    print('  ok: preview_check=off disables the HEAD check (all arg forms)')


def test_head_failure_never_fatal():
    mod = load_plugin()
    for exc in (OSError('boom'),):
        ie = make_ie(mod, FakeDownloader(exc=exc))
        ie._warn_if_preview_served('306559', META, DI_FULL)
        assert ie.warnings == []
    # missing meta/di fields -> silently skipped
    for meta, di in [
        ({}, DI_FULL),
        (META, {'quality': 'nq'}),
        (META, {'quality': 'nq', 'url': 'https://x', 'bitrate': None}),
    ]:
        ie = make_ie(mod, FakeDownloader())
        ie._warn_if_preview_served('306559', meta, di)
        assert ie.warnings == []
        assert ie._downloader.requests == []
    print('  ok: HEAD failure / missing fields never fatal')


def main():
    print('preview-mode guard tests:')
    test_served_quality_preview_warns()
    test_served_quality_full_no_warning()
    test_short_stream_warns()
    test_missing_content_length_no_warning()
    test_preview_check_disabled()
    test_head_failure_never_fatal()
    print('all tests passed')


if __name__ == '__main__':
    main()
