#!/usr/bin/env python3
"""
Unit tests for the signing-key machinery:

* ``_extractor_arg`` / ``_user_key`` — the CLI passes
  ``--extractor-args "name:sub:key=value"`` as
  ``{'name': {'sub:key': [value]}}`` (split on the *first* colon); all
  documented name forms must be found (regression: per-IE forms were
  silently ignored),
* ``_get_download_info`` 403 handling — JSON error object (bare and
  ``result``-wrapped), non-JSON body (``_download_json`` raises before
  returning — the raw body must be re-fetched), JSON array body,
* the key-rejection / auto-refresh flow in the track extractor — pinned
  key (no refresh), successful refresh (save + warn + retry), failed
  refresh (negative cache: the frontend is NOT re-downloaded per track),
  refreshed-key-also-rejected, same-key-still-rejected (clock hint),
* ``_KEY_PATTERNS`` — both frontend layouts.

Run:  python3 tests/test_key_refresh.py   (or: pytest tests/)
"""

import importlib.util
import os
import sys
from contextlib import contextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

HERE = os.path.dirname(__file__)
PLUGIN_PATH = os.path.join(HERE, '..', 'yt_dlp_plugins', 'extractor',
                           'yandex_music_v2.py')


@contextmanager
def patched_mod(mod, **attrs):
    """Patch module-level functions, restoring them on exit (the pytest
    ``mod`` fixture is session-scoped — a leak would reorder the suite)."""
    old = {k: getattr(mod, k) for k in attrs}
    for k, v in attrs.items():
        setattr(mod, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(mod, k, v)


def load_plugin():
    spec = importlib.util.spec_from_file_location('ymv2', PLUGIN_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeCache:
    def __init__(self, data=None):
        self.data = data or {}
        self.stored = []

    def load(self, section, key):
        return self.data.get((section, key))

    def store(self, section, key, value):
        self.stored.append((section, key, value))
        self.data[(section, key)] = value


class FakeDownloader:
    def __init__(self, params=None, cache=None):
        self.params = params or {}
        # ie.cache delegates to _downloader.cache (read-only property)
        self.cache = cache if cache is not None else FakeCache()


def make_ie(mod, params=None, cache=None):
    ie = mod.YandexMusicTrackIE.__new__(mod.YandexMusicTrackIE)
    ie._url = 'https://music.yandex.ru/track/123'
    ie._downloader = FakeDownloader(params, cache)
    ie.warnings = []
    ie.report_warning = lambda msg: ie.warnings.append(msg)
    # the preview guard is not under test here
    ie._warn_if_preview_served = lambda *a: None
    return ie


def reset_key_cache(mod):
    mod._KEY_CACHE.update(key=None, refresh_failed=False)


def test_extractor_arg_cli_forms(mod):
    """Regression: per-IE --extractor-args forms were silently ignored."""
    cases = [
        # top-level form
        ({'yandexmusicv2': {'hmac_key': ['K1']}}, 'K1'),
        ({'yandexmusic': {'hmac_key': ['K1b']}}, 'K1b'),
        # CLI per-IE forms (split on the FIRST colon)
        ({'yandexmusicv2': {'liked:hmac_key': ['K2']}}, 'K2'),
        ({'yandexmusic': {'track:hmac_key': ['K3']}}, 'K3'),
        ({'yandexmusic': {'artist:tracks:hmac_key': ['K3b']}}, 'K3b'),
        ({'yandexmusic': {'playlist:hmac_key': ['K3c']}}, 'K3c'),
        ({'yandexmusicv2': {'artist:hmac_key': ['K3d']}}, 'K3d'),
        # SDK-style params (full IE name as top-level key)
        ({'yandexmusic:track': {'hmac_key': ['K4']}}, 'K4'),
        ({'yandexmusicv2:playlist': {'hmac_key': ['K5']}}, 'K5'),
        ({'yandexmusicv2:liked': {'hmac_key': ['K6']}}, 'K6'),
    ]
    for args, want in cases:
        ie = make_ie(mod, {'extractor_args': args})
        assert mod._user_key(ie) == want, f'{args} -> {mod._user_key(ie)!r}, want {want!r}'
    print('  ok: hmac_key found in every documented --extractor-args form')


def test_extractor_arg_edge_cases(mod):
    ie = make_ie(mod, {'extractor_args': {'other': {'hmac_key': ['K']}}})
    assert mod._user_key(ie) is None  # unknown extractor name
    ie = make_ie(mod, {'extractor_args': {'yandexmusicv2': 'garbage'}})
    assert mod._user_key(ie) is None  # non-dict sub-value: no AttributeError
    ie = make_ie(mod, {'extractor_args': {'yandexmusicv2': {'hmac_key': []}}})
    assert mod._user_key(ie) is None  # empty value list
    ie = make_ie(mod, {})
    assert mod._user_key(ie) is None  # no extractor_args at all
    ie = make_ie(mod, {'extractor_args': {'yandexmusicv2': {'hmac_key': ['K']}}})
    assert mod._extractor_arg(ie, 'other_arg') is None
    print('  ok: unknown names / non-dict / empty values handled')


def test_get_download_info_403_shapes(mod):
    """All 403 shapes must be classified correctly."""
    err_not_allowed = {'name': 'track-download-info-error',
                       'message': 'not-allowed'}

    def with_json(info):
        ie = make_ie(mod)
        ie._download_json = lambda *a, **k: info
        return ie

    def expect_key_rejected(info):
        ie = with_json(info)
        try:
            mod._get_download_info(ie, '123', 'nq')
        except mod._KeyRejected:
            return
        raise AssertionError(f'{info!r}: expected _KeyRejected')

    def expect_error(info, needle):
        ie = with_json(info)
        try:
            mod._get_download_info(ie, '123', 'nq')
        except mod._KeyRejected:
            raise AssertionError(f'{info!r}: unexpected _KeyRejected')
        except Exception as e:
            assert needle in str(e), f'{e!r} does not contain {needle!r}'
            return
        raise AssertionError(f'{info!r}: expected an error')

    # JSON error object (the shape the server actually uses today)
    expect_key_rejected(err_not_allowed)
    expect_key_rejected({'result': err_not_allowed})
    # JSON array body (non-dict): not-allowed inside -> rejected
    expect_key_rejected(['not-allowed'])
    expect_error([1, 2, 3], 'unexpected 403')
    # other track-download-info-error messages -> plain error
    expect_error({'name': 'track-download-info-error', 'message': 'boom'}, 'boom')
    # success path
    ie = with_json({'downloadInfo': {'url': 'https://strm/x', 'quality': 'nq'}})
    di = mod._get_download_info(ie, '123', 'nq')
    assert di['url'] == 'https://strm/x'
    # missing downloadInfo / url
    expect_error({'downloadInfo': {}}, 'no download URL')
    expect_error({}, 'no download URL')
    print('  ok: 403 shapes (JSON bare/wrapped/array, success, missing url)')


def test_get_download_info_non_json_403(mod):
    """Regression: a non-JSON 403 body makes _download_json raise BEFORE
    returning — the raw body must be re-fetched to detect not-allowed."""
    from yt_dlp.utils import ExtractorError

    ie = make_ie(mod)
    def dj(*a, **k):
        raise ExtractorError('123: Failed to parse JSON')
    ie._download_json = dj
    ie._download_webpage = lambda *a, **k: 'error: not-allowed'
    try:
        mod._get_download_info(ie, '123', 'nq')
        raise AssertionError('expected _KeyRejected')
    except mod._KeyRejected:
        pass

    # non-JSON body without not-allowed -> the original error is re-raised
    ie2 = make_ie(mod)
    ie2._download_json = dj
    ie2._download_webpage = lambda *a, **k: 'Internal Server Error'
    try:
        mod._get_download_info(ie2, '123', 'nq')
        raise AssertionError('expected ExtractorError')
    except mod._KeyRejected:
        raise AssertionError('unexpected _KeyRejected')
    except ExtractorError as e:
        assert 'Failed to parse JSON' in str(e)

    # re-fetch itself fails -> still the original error
    ie3 = make_ie(mod)
    ie3._download_json = dj
    def boom(*a, **k):
        raise OSError('network down')
    ie3._download_webpage = boom
    try:
        mod._get_download_info(ie3, '123', 'nq')
        raise AssertionError('expected ExtractorError')
    except mod._KeyRejected:
        raise AssertionError('unexpected _KeyRejected')
    except ExtractorError as e:
        assert 'Failed to parse JSON' in str(e)
    print('  ok: non-JSON 403 -> raw body re-fetched, _KeyRejected / re-raise')


def test_refresh_flow(mod):
    """The _KeyRejected handler in the track extractor."""
    from yt_dlp.utils import ExtractorError

    META = {'title': 't', 'artists': [], 'albums': [], 'durationMs': 1000}
    DI = {'url': 'https://strm/x', 'quality': 'nq', 'bitrate': 192, 'codec': 'mp3'}

    # setup() reassigns _fetch_key_from_frontend/_get_download_info inside
    # the block; passing them here makes patched_mod restore the real ones
    with patched_mod(mod, _get_track_meta=lambda ie_, tid: META,
                     _fetch_key_from_frontend=mod._fetch_key_from_frontend,
                     _get_download_info=mod._get_download_info):
        def setup(extractor_args, di_script, frontend_key, cache=None,
                  reset=True, set_failed_on_none=True):
            """di_script: list of (return-value | exception) per
            _get_download_info call (the last entry repeats).
            set_failed_on_none mimics the real _fetch_key_from_frontend,
            which negative-caches a *completed* scan that found nothing."""
            if reset:
                reset_key_cache(mod)
            ie = make_ie(mod, {'extractor_args': extractor_args}, cache)
            calls = {'di': 0, 'frontend': 0}

            def fetch(ie_):
                calls['frontend'] += 1
                if frontend_key is None and set_failed_on_none:
                    mod._KEY_CACHE['refresh_failed'] = True
                return frontend_key

            def di(ie_, tid, quality):
                action = di_script[min(calls['di'], len(di_script) - 1)]
                calls['di'] += 1
                if isinstance(action, Exception):
                    raise action
                return action

            mod._fetch_key_from_frontend = fetch
            mod._get_download_info = di
            return ie, calls

        def expect_error(ie, needle):
            try:
                ie._real_extract(ie._url)
            except ExtractorError as e:
                assert needle in str(e), f'{e!r} does not contain {needle!r}'
                return
            raise AssertionError(f'expected ExtractorError containing {needle!r}')

        # 1. happy path: lossless works
        ie, calls = setup({}, [DI], None)
        info = ie._real_extract(ie._url)
        assert info['formats'][0]['url'] == 'https://strm/x'
        assert calls['frontend'] == 0 and ie.warnings == []
        print('  ok: happy path — no refresh, no warnings')

        # 2. pinned key rejected -> specific error, NO frontend refresh
        ie, calls = setup({'yandexmusicv2': {'hmac_key': ['OLD']}},
                          [mod._KeyRejected()], 'NEWKEY')
        expect_error(ie, 'pinned')
        assert calls['frontend'] == 0, 'pinned key must not trigger auto-refresh'
        print('  ok: pinned key rejected -> specific error, no auto-refresh')

        # 3. unpinned: rejected -> frontend returns new key -> saved + warned + retry
        cache = FakeCache()
        ie, calls = setup({}, [mod._KeyRejected(), DI], 'NEWKEY', cache)
        info = ie._real_extract(ie._url)
        assert calls['frontend'] == 1
        assert mod._KEY_CACHE['key'] == 'NEWKEY'
        assert cache.stored and cache.stored[0][2] == {'key': 'NEWKEY'}
        assert len(ie.warnings) == 1 and 'refreshed' in ie.warnings[0]
        assert info['formats'][0]['url'] == 'https://strm/x'
        print('  ok: refresh success — key saved, warned, retry works')

        # 4. unpinned: rejected -> frontend finds nothing -> error + negative
        #    cache; the NEXT track fails immediately without re-fetching
        ie, calls = setup({}, [mod._KeyRejected()], None)
        expect_error(ie, 'could not be refreshed')
        assert mod._KEY_CACHE['refresh_failed'] is True
        assert calls['frontend'] == 1
        ie2, calls2 = setup({}, [mod._KeyRejected()], None, reset=False)
        expect_error(ie2, 'earlier refresh')
        assert calls2['frontend'] == 0, 'frontend re-downloaded despite negative cache'
        print('  ok: failed refresh — negative cache prevents per-track re-fetch')

        # 4b. a *failed* refresh (fetch error, not a completed scan) must NOT
        #     poison the process — the next track retries the frontend
        ie, calls = setup({}, [mod._KeyRejected()], None, set_failed_on_none=False)
        expect_error(ie, 'could not be refreshed')
        assert mod._KEY_CACHE['refresh_failed'] is False
        ie2, calls2 = setup({}, [mod._KeyRejected(), DI], 'NEWKEY', reset=False)
        info = ie2._real_extract(ie2._url)
        assert calls2['frontend'] == 1, 'failed refresh must stay retryable'
        print('  ok: failed fetch stays retryable (no negative cache)')

        # 5. unpinned: rejected -> refreshed key also rejected -> specific error
        ie, calls = setup({}, [mod._KeyRejected(), mod._KeyRejected()], 'NEWKEY')
        expect_error(ie, 'refreshed signing key was also')
        assert calls['frontend'] == 1
        print('  ok: refreshed key also rejected -> specific error')

        # 6. unpinned: rejected -> frontend still has the SAME key -> clock
        #    hint + negative cache (re-scanning cannot help)
        ie, calls = setup({}, [mod._KeyRejected()], mod._SECRET_KEY)
        expect_error(ie, 'clock')
        assert mod._KEY_CACHE['refresh_failed'] is True
        print('  ok: same key still rejected -> clock hint + negative cache')


def test_key_patterns(mod):
    new_layout = 'player:{secretKey:{web:"ABCD1234efgh5678",win32:"x",darwin:"y"}}'
    old_layout = 'var cfg={secretKey:"ABCD1234efgh5678",other:1}'
    assert mod._KEY_PATTERNS[0].search(new_layout).group(1) == 'ABCD1234efgh5678'
    # the per-platform pattern must NOT match the flat layout
    assert mod._KEY_PATTERNS[0].search(old_layout) is None
    assert mod._KEY_PATTERNS[1].search(old_layout).group(1) == 'ABCD1234efgh5678'
    # near-misses
    assert not any(p.search("secretKey: { web: 'ABCD1234' }") for p in mod._KEY_PATTERNS)
    assert not any(p.search('otherKey:"ABCD1234"') for p in mod._KEY_PATTERNS)
    print('  ok: key patterns match both frontend layouts, reject near-misses')


class FakeScanIE:
    """Minimal stand-in for the track IE in the frontend key scan."""

    def __init__(self, pages):
        self.pages = pages

    def _download_webpage(self, url, video_id, note=False, fatal=False,
                          errnote=False, **kwargs):
        return self.pages.get(url)

    def write_debug(self, msg):
        pass


SCAN_HOME = 'https://music.yandex.ru/'
SCAN_WEBPACK = 'https://cdn.example.com/static/chunks/webpack-abc.js'
SCAN_CHUNK = 'https://cdn.example.com/static/chunks/1-aaaa.js'


def test_frontend_chunk_urls(mod):
    """All three a.u entry styles must produce single-path URLs
    (regression: literal entries used to become
    static/chunks/static/chunks/… and 404)."""
    runtime = (
        'a.u=e=>3266===e?"static/chunks/3266-bdb947223b00b411.js":'
        '32732===e?"static/chunks/"+e+"-89f7fa7b5c3e398a.js":'
        '"static/chunks/"+(({4246:"7e641530"})[e]||e)+"."+'
        '({4246:"8a8c0bd886153ac7",999:"deadbeef"})[e]')
    ie = FakeScanIE({SCAN_HOME: f'<script src="{SCAN_WEBPACK}"></script>',
                     SCAN_WEBPACK: runtime})
    urls = mod._frontend_chunk_urls(ie, ie.pages[SCAN_HOME])
    base = SCAN_WEBPACK.rsplit('/static/chunks/', 1)[0] + '/static/chunks/'
    assert base + '3266-bdb947223b00b411.js' in urls      # literal
    assert base + '32732-89f7fa7b5c3e398a.js' in urls     # concat (id from the condition)
    assert base + '7e641530.8a8c0bd886153ac7.js' in urls  # tables, prefix mapped
    assert base + '999.deadbeef.js' in urls               # tables, id as prefix
    assert not any('static/chunks/static/chunks/' in u for u in urls)
    print('  ok: a.u literal/concat/table entries all resolved, no doubled paths')


def test_fetch_key_from_frontend(mod):
    reset_key_cache(mod)
    html = (f'<script src="{SCAN_WEBPACK}"></script>'
            f'<script src="{SCAN_CHUNK}"></script>')
    # 1) key found in a chunk
    ie = FakeScanIE({SCAN_HOME: html, SCAN_WEBPACK: '',
                     SCAN_CHUNK: 'cfg={secretKey:"KEY1234567890"}'})
    assert mod._fetch_key_from_frontend(ie) == 'KEY1234567890'
    assert mod._KEY_CACHE['refresh_failed'] is False
    # 2) completed scan, no match -> negative cache (layout change)
    reset_key_cache(mod)
    ie = FakeScanIE({SCAN_HOME: html, SCAN_WEBPACK: '', SCAN_CHUNK: 'nothing'})
    assert mod._fetch_key_from_frontend(ie) is None
    assert mod._KEY_CACHE['refresh_failed'] is True
    # 3) failed homepage fetch -> retryable, NO negative cache
    reset_key_cache(mod)
    ie = FakeScanIE({})
    assert mod._fetch_key_from_frontend(ie) is None
    assert mod._KEY_CACHE['refresh_failed'] is False
    print('  ok: frontend scan — found / no-match (cached) / fetch-fail (retryable)')


def test_load_cached_key_roundtrip(mod):
    """A key saved to the disk cache in one "process" is loaded in the next."""
    reset_key_cache(mod)
    cache = FakeCache()
    ie = make_ie(mod, cache=cache)
    mod._save_cached_key(ie, 'DISKKEY')
    assert mod._KEY_CACHE['key'] == 'DISKKEY'
    mod._KEY_CACHE['key'] = None  # fresh process: in-memory cache empty
    assert mod._load_cached_key(ie) == 'DISKKEY'
    assert mod._KEY_CACHE['key'] == 'DISKKEY'
    # no disk cache entry -> None
    reset_key_cache(mod)
    ie2 = make_ie(mod, cache=FakeCache())
    assert mod._load_cached_key(ie2) is None
    print('  ok: disk-cache round-trip (save in run N, load in run N+1)')


def main():
    print('key rotation / 403 handling tests:')
    mod = load_plugin()
    test_extractor_arg_cli_forms(mod)
    test_extractor_arg_edge_cases(mod)
    test_get_download_info_403_shapes(mod)
    test_get_download_info_non_json_403(mod)
    test_refresh_flow(mod)
    test_key_patterns(mod)
    test_frontend_chunk_urls(mod)
    test_fetch_key_from_frontend(mod)
    test_load_cached_key_roundtrip(mod)
    print('all tests passed')


if __name__ == '__main__':
    main()
