#!/usr/bin/env python3
"""
Verify the yt-dlp plugin is installed and actually taking effect.

Note: `yt-dlp --list-extractors` does NOT show plugin extractors — in
yt-dlp the list-extractors path runs before `load_all_plugins()`, so
plugins are invisible there. This script loads the plugins explicitly
and checks:

  1. the shadowed built-ins (track, album, artist:tracks, playlist,
     artist:albums) are served by the plugin,
  2. the plugin-only extractors (shared playlist, liked playlist,
     artist page) are present.

Usage:  python3 tools/check_install.py
Exit:   0 = OK, 1 = plugin missing/broken
"""

import sys

PLUGIN_MODULE = 'yt_dlp_plugins.extractor.yandex_music_v2'

# (IE_NAME, class name, note)
EXPECTED = [
    ('yandexmusic:track', 'YandexMusicTrackIE',
     'shadows broken built-in'),
    ('yandexmusic:album', 'YandexMusicAlbumIE',
     'shadows broken built-in'),
    ('yandexmusic:artist:tracks', 'YandexMusicArtistTracksIE',
     'shadows broken built-in'),
    ('yandexmusic:playlist', 'YandexMusicPlaylistIE',
     'shadows broken built-in (users/<login>/playlists URLs)'),
    ('yandexmusic:artist:albums', 'YandexMusicArtistAlbumsIE',
     'shadows broken built-in (artist/<id>/albums URLs)'),
    ('yandexmusicv2:playlist', 'YandexMusicV2PlaylistIE',
     'shared playlists + charts'),
    ('yandexmusicv2:liked', 'YandexMusicV2LikedPlaylistIE',
     'liked/favorites playlists'),
    ('yandexmusicv2:artist', 'YandexMusicArtistIE',
     'artist pages (all tracks)'),
]


def main():
    try:
        from yt_dlp.plugins import load_all_plugins
    except ImportError as e:
        print(f'FAIL: yt-dlp not importable ({e})')
        return 1

    load_all_plugins()
    from yt_dlp.extractor import gen_extractor_classes
    classes = list(gen_extractor_classes())
    by_name = {c.IE_NAME: c for c in classes}

    ok = True
    for ie_name, class_name, note in EXPECTED:
        cls = by_name.get(ie_name)
        if cls is not None and cls.__module__ == PLUGIN_MODULE:
            print(f'OK:  {ie_name:<28} -> plugin ({note})')
        else:
            mod = f'{cls.__module__}/{cls.__name__}' if cls else 'missing'
            print(f'FAIL: {ie_name} not served by the plugin (got: {mod})')
            ok = False

    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
