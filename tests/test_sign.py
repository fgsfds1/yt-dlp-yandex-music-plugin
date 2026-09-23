#!/usr/bin/env python3
"""
Regression tests for the Yandex Music request-signing algorithm.

The vectors below were captured from live music.yandex.ru web app traffic
(2026-08-17, frontend v4.1520.1) by hooking TextEncoder/btoa in a headless
Chromium via CDP — see research/cdp/. The app's own computed signatures
must be reproduced exactly.

Run:  python3 tests/test_sign.py
"""

import base64
import hashlib
import hmac
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# key hardcoded in the frontend bundle (module 25079, chunk 8290-*.js)
SECRET_KEY = '7tvSmFbyf5hJnIHhCimDDD'

# app-default codec list sent by the web player
APP_CODECS = 'flac,aac,he-aac,mp3,flac-mp4,aac-mp4,he-aac-mp4'

# (ts, trackId, quality, codecs, transport, expected_sign)
VECTORS = [
    # single-track request, captured 2026-08-17
    ('1786984334', '138802868', 'nq', APP_CODECS, 'encraw',
     'GWBqpSgPNmFuN7omwYoxrQySsV6peL9tXil2YypY6nU'),
    ('1786984343', '143540328', 'nq', APP_CODECS, 'encraw',
     'Q0yu13/Laqr6iFPrbTY4r9cVfwPRVm203XN6jsniKM0'),
]

# The plugin's own parameter combination (codecs flac,mp3 + transport raw)
# pinned against the same key. Self-computed (not a live capture) — it pins
# the plugin's _CODECS/_TRANSPORT constants: a copy-paste drift in either
# changes this signature.
PLUGIN_PARAMS_VECTOR = (
    '1786984334', '138802868', 'nq', 'flac,mp3', 'raw',
    'yP53IBcSVrvRo1TVvcO88gpOkBRSAjng4Lrd7PbsBkg',
)


def make_sign(ts, track_id, quality, codecs, transport, key=SECRET_KEY):
    data = f'{ts}{track_id}{quality}{codecs.replace(",", "")}{transport}'
    digest = hmac.new(key.encode(), data.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode().rstrip('=')


def test_vectors():
    for ts, tid, q, codecs, tr, expected in VECTORS:
        got = make_sign(ts, tid, q, codecs, tr)
        assert got == expected, f'sign mismatch for track {tid}:\n  got  {got}\n  want {expected}'
        print(f'  ok: ts={ts} trackId={tid} -> {got}')


def test_codecs_comma_gotcha():
    """The signing data must use codecs WITHOUT commas.

    Verifies that using the comma-joined form (as in the URL) does NOT
    reproduce the captured signature — this was the original bug that
    blocked the whole project for a while.
    """
    ts, tid, q, codecs, tr, expected = VECTORS[0]
    wrong_data = f'{ts}{tid}{q}{codecs}{tr}'  # commas kept
    wrong = base64.b64encode(
        hmac.new(SECRET_KEY.encode(), wrong_data.encode(), hashlib.sha256).digest()
    ).decode().rstrip('=')
    assert wrong != expected, 'comma-joined codecs unexpectedly matched — vector invalid?'
    print('  ok: comma-joined codecs do not match (gotcha confirmed)')


def test_plugin_matches():
    """The plugin module's signer must agree with this reference."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'ymv2', os.path.join(os.path.dirname(__file__), '..',
                             'yt_dlp_plugins', 'extractor', 'yandex_music_v2.py'))
    # the plugin imports yt_dlp; skip gracefully if unavailable
    try:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except ImportError as e:
        print(f'  skip: yt_dlp not importable ({e})')
        return
    assert mod._SECRET_KEY == SECRET_KEY, 'plugin key out of sync'
    for ts, tid, q, codecs, tr, expected in VECTORS:
        assert mod._make_sign(ts, tid, q, codecs, tr, SECRET_KEY) == expected
    # the plugin's actual request parameters must produce the pinned sign
    ts, tid, q, codecs, tr, expected = PLUGIN_PARAMS_VECTOR
    assert (mod._CODECS, mod._TRANSPORT) == ('flac,mp3', 'raw'), \
        'plugin _CODECS/_TRANSPORT drifted from the pinned vector'
    assert mod._make_sign(ts, tid, q, mod._CODECS, mod._TRANSPORT,
                          SECRET_KEY) == expected
    print('  ok: plugin _make_sign matches reference (incl. its own params)')


def test_standalone_key_in_sync():
    """The standalone tool duplicates the key constant — a rotated key
    would silently break it while the plugin (auto-refresh) keeps working."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'ymstandalone', os.path.join(os.path.dirname(__file__), '..',
                                     'tools', 'standalone_download.py'))
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    assert tool.SECRET_KEY == SECRET_KEY, \
        'tools/standalone_download.py key out of sync with the test/plugin key'
    print('  ok: standalone tool key in sync')


def main():
    print('sign algorithm tests:')
    test_vectors()
    test_codecs_comma_gotcha()
    test_plugin_matches()
    test_standalone_key_in_sync()
    print('all tests passed')


if __name__ == '__main__':
    main()
