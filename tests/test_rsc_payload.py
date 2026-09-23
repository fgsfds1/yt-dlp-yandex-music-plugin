#!/usr/bin/env python3
"""
Unit tests for the RSC payload parsing helpers
(``_rsc_payload`` / ``_preloaded_object``).

These two pure functions underpin the playlist, liked, users-playlist and
album extractors. They are pinned here because a regression would surface
as "playlist data not found in page" with no obvious cause:

* chunks are JSON-encoded string fragments that must be decoded and joined,
* the preloaded object is located by key and decoded with ``raw_decode``
  (manual brace counting breaks on {/} inside JSON string values, e.g. a
  playlist description ending in ``}``),
* missing keys / ``null`` / non-object values / truncated payloads must all
  yield ``None`` (never an exception).

Run:  python3 tests/test_rsc_payload.py   (or: pytest tests/)
"""

import importlib.util
import json
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


def page(*fragments):
    """Wrap payload fragments in a minimal Next.js RSC page (each fragment
    is the inner content of a JSON string, so it gets JSON-escaped)."""
    out = ['<html><body>']
    for f in fragments:
        inner = json.dumps(f, ensure_ascii=False)[1:-1]
        out.append(f'self.__next_f.push([1,"{inner}"])')
    out.append('</body></html>')
    return ''.join(out)


def test_rsc_payload_joins_chunks(mod):
    html = page('{"preloadedA', 'lbum":', '{"id":1}')
    assert mod._rsc_payload(html) == '{"preloadedAlbum":{"id":1}'
    print('  ok: multiple chunks are decoded and joined')


def test_rsc_payload_escapes(mod):
    # a fragment containing a quote and a backslash must survive the
    # JSON string round-trip
    html = page('say "hi" and a backslash \\ here')
    assert mod._rsc_payload(html) == 'say "hi" and a backslash \\ here'
    print('  ok: escaped quotes / backslashes round-trip')


def test_rsc_payload_unicode(mod):
    html = page('«Танцы» — playlist')
    assert mod._rsc_payload(html) == '«Танцы» — playlist'
    print('  ok: unicode (Cyrillic) survives')


def test_rsc_payload_no_chunks(mod):
    assert mod._rsc_payload('<html>nothing</html>') == ''
    print('  ok: no chunks -> empty payload')


def test_preloaded_object_basic(mod):
    payload = '{"other":1,"preloadedAlbum":{"id":42,"title":"x"},"after":2}'
    obj = mod._preloaded_object(payload, 'preloadedAlbum')
    assert obj == {'id': 42, 'title': 'x'}
    print('  ok: object extracted by key')


def test_preloaded_object_braces_in_strings(mod):
    # the documented raw_decode rationale: {/} inside JSON string values
    payload = ('{"preloadedPlaylist":{"title":"ends with a brace }",'
               '"description":"{ and } everywhere","tracks":[]}}')
    obj = mod._preloaded_object(payload, 'preloadedPlaylist')
    assert obj['title'] == 'ends with a brace }'
    assert obj['description'] == '{ and } everywhere'
    print('  ok: braces inside JSON string values (raw_decode)')


def test_preloaded_object_missing_key(mod):
    assert mod._preloaded_object('{"a":1}', 'preloadedAlbum') is None
    print('  ok: missing key -> None')


def test_preloaded_object_null_value(mod):
    assert mod._preloaded_object('{"preloadedAlbum":null}', 'preloadedAlbum') is None
    print('  ok: null value -> None')


def test_preloaded_object_non_object_value(mod):
    assert mod._preloaded_object('{"preloadedAlbum":123}', 'preloadedAlbum') is None
    assert mod._preloaded_object('{"preloadedAlbum":[1,2]}', 'preloadedAlbum') is None
    print('  ok: non-object value -> None')


def test_preloaded_object_whitespace(mod):
    # whitespace after the colon (the RSC payload is compact JSON, but be
    # lenient anyway)
    payload = '{"preloadedAlbum":   {"id":1}}'
    assert mod._preloaded_object(payload, 'preloadedAlbum') == {'id': 1}
    print('  ok: whitespace after the colon')


def test_preloaded_object_truncated(mod):
    # regression: the value position fell off the end of the payload
    # (truncated page) and raised IndexError instead of returning None
    assert mod._preloaded_object('{"preloadedAlbum":', 'preloadedAlbum') is None
    assert mod._preloaded_object('{"preloadedAlbum": ', 'preloadedAlbum') is None
    print('  ok: truncated payload -> None (no IndexError)')


def test_preloaded_object_malformed(mod):
    assert mod._preloaded_object('{"preloadedAlbum":{"id":}', 'preloadedAlbum') is None
    print('  ok: malformed JSON -> None')


def main():
    print('RSC payload tests:')
    mod = load_plugin()
    test_rsc_payload_joins_chunks(mod)
    test_rsc_payload_escapes(mod)
    test_rsc_payload_unicode(mod)
    test_rsc_payload_no_chunks(mod)
    test_preloaded_object_basic(mod)
    test_preloaded_object_braces_in_strings(mod)
    test_preloaded_object_missing_key(mod)
    test_preloaded_object_null_value(mod)
    test_preloaded_object_non_object_value(mod)
    test_preloaded_object_whitespace(mod)
    test_preloaded_object_truncated(mod)
    test_preloaded_object_malformed(mod)
    print('all tests passed')


if __name__ == '__main__':
    main()
