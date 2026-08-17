#!/usr/bin/env python3
"""
Verify the yt-dlp plugin is installed and actually taking effect.

Note: `yt-dlp --list-extractors` does NOT show plugin extractors — in
yt-dlp the list-extractors path runs before `load_all_plugins()`, so
plugins are invisible there. This script loads the plugins explicitly
and checks:

  1. YandexMusicTrackIE is served by the plugin (shadows the broken
     built-in extractor),
  2. YandexMusicV2PlaylistIE is present (shared-playlist URL support).

Usage:  python3 tools/check_install.py
Exit:   0 = OK, 1 = plugin missing/broken
"""

import sys


def main():
    try:
        from yt_dlp.plugins import load_all_plugins
    except ImportError as e:
        print(f'FAIL: yt-dlp not importable ({e})')
        return 1

    load_all_plugins()
    from yt_dlp.extractor import gen_extractor_classes
    classes = list(gen_extractor_classes())

    ok = True
    track = [c for c in classes if c.__name__ == 'YandexMusicTrackIE']
    if track and track[0].__module__ == 'yt_dlp_plugins.extractor.yandex_music_v2':
        print('OK:  yandexmusic:track      -> plugin (shadows broken built-in)')
    else:
        mod = track[0].__module__ if track else 'missing'
        print(f'FAIL: yandexmusic:track not served by the plugin (module: {mod})')
        ok = False

    v2 = [c for c in classes if c.__name__ == 'YandexMusicV2PlaylistIE']
    if v2:
        print('OK:  yandexmusicv2:playlist -> plugin (shared playlists)')
    else:
        print('FAIL: YandexMusicV2PlaylistIE not found (shared-playlist support missing)')
        ok = False

    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
