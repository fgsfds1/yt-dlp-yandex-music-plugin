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
        assert mod._make_sign(ts, tid, q, codecs, tr) == expected
    print('  ok: plugin _make_sign matches reference')


def main():
    print('sign algorithm tests:')
    test_vectors()
    test_codecs_comma_gotcha()
    test_plugin_matches()
    print('all tests passed')


if __name__ == '__main__':
    main()
