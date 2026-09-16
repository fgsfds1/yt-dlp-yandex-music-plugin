#!/usr/bin/env python3
"""
Standalone Yandex Music playlist downloader (no yt-dlp required).

Reference implementation of the API described in research/api-notes.md.
Downloads a shared playlist (https://music.yandex.ru/playlists/<uuid>) as
MP3 320 kbps (or FLAC where available) files named:

    NN - Artist - Title.ext

with an optional M3U.

Usage:
    python3 tools/standalone_download.py \
        --cookies /path/to/cookies.txt \
        --playlist https://music.yandex.ru/playlists/<uuid> \
        --out /path/to/music_dir \
        [--m3u /path/to/Playlist.m3u] [--wine-prefix Z:\\home\\lw\\Music]

Cookies: Netscape-format cookie file (e.g. exported from a browser or via
yt-dlp), must contain a logged-in Yandex Music session.
"""

import argparse
import base64
import hashlib
import hmac
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

API = 'https://api.music.yandex.ru'
# App-level secret from the music-web frontend bundle (see extract_secret_key.py)
SECRET_KEY = '7tvSmFbyf5hJnIHhCimDDD'
CODECS = 'flac,mp3'
TRANSPORT = 'raw'
UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36')

CYR = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo',
    'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
    'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
    'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'shch',
    'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
    'і': 'i', 'ї': 'yi', 'є': 'ye', 'ґ': 'g', 'ў': 'u',
    'А': 'A', 'Б': 'B', 'В': 'V', 'Г': 'G', 'Д': 'D', 'Е': 'E', 'Ё': 'Yo',
    'Ж': 'Zh', 'З': 'Z', 'И': 'I', 'Й': 'Y', 'К': 'K', 'Л': 'L', 'М': 'M',
    'Н': 'N', 'О': 'O', 'П': 'P', 'Р': 'R', 'С': 'S', 'Т': 'T', 'У': 'U',
    'Ф': 'F', 'Х': 'Kh', 'Ц': 'Ts', 'Ч': 'Ch', 'Ш': 'Sh', 'Щ': 'Shch',
    'Ъ': '', 'Ы': 'Y', 'Ь': '', 'Э': 'E', 'Ю': 'Yu', 'Я': 'Ya',
    'І': 'I', 'Ї': 'Yi', 'Є': 'Ye', 'Ґ': 'G', 'Ў': 'U',
}
INVALID = ':*?"<>|\\/'
RESERVED = {'CON', 'PRN', 'AUX', 'NUL'} | \
    {f'COM{i}' for i in range(1, 10)} | {f'LPT{i}' for i in range(1, 10)}


def sanitize(name):
    out = []
    for ch in name.strip():
        out.append(ch if ord(ch) < 128 else CYR.get(ch, ''))
    name = ''.join(out)
    for ch in INVALID:
        name = name.replace(ch, ' ')
    name = re.sub(r'\s+', ' ', name).strip(' .') or 'Unknown'
    if name.split('.')[0].upper() in RESERVED:
        name = '_' + name
    return name


def load_cookie_header(path):
    pairs = []
    for line in open(path, encoding='utf-8'):
        if not line.strip() or line.startswith('#'):
            continue
        parts = line.rstrip('\n').split('\t')
        if len(parts) >= 7:
            pairs.append(f'{parts[5]}={parts[6]}')
    if not pairs:
        sys.exit(f'no cookies found in {path}')
    return '; '.join(pairs)


def make_sign(ts, track_id, quality, codecs, transport):
    data = f'{ts}{track_id}{quality}{codecs.replace(",", "")}{transport}'
    digest = hmac.new(SECRET_KEY.encode(), data.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode().rstrip('=')


def api_get(url, cookie, note=''):
    headers = {
        'User-Agent': UA, 'Accept': 'application/json',
        'Origin': 'https://music.yandex.ru', 'Referer': 'https://music.yandex.ru/',
        'X-Requested-With': 'XMLHttpRequest', 'X-Retpath-Y': 'https://music.yandex.ru/',
        'x-request-id': str(uuid.uuid4()),
        'x-yandex-music-client': 'YandexMusicWebNext/1.0.0',
        'x-yandex-music-without-invocation-info': '1',
        'accept-language': 'en', 'Cookie': cookie,
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', 'replace')[:300]
        # RuntimeError (not SystemExit): SystemExit is a BaseException, so it
        # would skip the per-track `except Exception` handlers and abort the
        # whole run on a single 4xx/5xx
        raise RuntimeError(f'HTTP {e.code} for {note or url}\n{body}') from None


def get_track_meta(cookie, track_id):
    boundary = '----WebKitFormBoundary' + uuid.uuid4().hex[:16]
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="trackIds"\r\n\r\n'
            f'{track_id}\r\n--{boundary}--\r\n').encode()
    url = f'{API}/tracks'
    headers = {
        'User-Agent': UA, 'Accept': 'application/json',
        'Origin': 'https://music.yandex.ru', 'Referer': 'https://music.yandex.ru/',
        'X-Requested-With': 'XMLHttpRequest',
        'x-request-id': str(uuid.uuid4()),
        'x-yandex-music-client': 'YandexMusicWebNext/1.0.0',
        'x-yandex-music-without-invocation-info': '1',
        'Cookie': cookie,
        'Content-Type': f'multipart/form-data; boundary={boundary}',
    }
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())[0]


def get_download_info(cookie, track_id, quality):
    ts = str(int(time.time()))
    sign = make_sign(ts, track_id, quality, CODECS, TRANSPORT)
    url = (f'{API}/get-file-info?ts={ts}&trackId={track_id}&quality={quality}'
           f'&codecs={urllib.parse.quote(CODECS)}&transports={TRANSPORT}'
           f'&sign={urllib.parse.quote(sign)}')
    info = api_get(url, cookie, note=f'get-file-info {track_id} ({quality})')
    di = info.get('downloadInfo')
    if not di or not di.get('url'):
        raise RuntimeError(f'no downloadInfo for track {track_id} ({quality})')
    return di


def parse_playlist_page(html):
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', html)
    payload = ''.join(json.loads('"' + c + '"') for c in chunks)
    i = payload.find('"preloadedPlaylistByUuid":')
    if i == -1:
        raise SystemExit('playlist data not found in page (public playlist + valid cookies?)')
    j = payload.find('{', i)
    # raw_decode: handles {/} inside JSON string values (playlist
    # descriptions, track titles, ...) — manual brace counting does not
    try:
        return json.JSONDecoder().raw_decode(payload, j)[0]
    except json.JSONDecodeError:
        raise SystemExit('malformed playlist data')


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument('--cookies', required=True, help='Netscape cookie file')
    ap.add_argument('--playlist', required=True, help='https://music.yandex.ru/playlists/<uuid>')
    ap.add_argument('--out', required=True, help='output directory')
    ap.add_argument('--m3u', help='optional M3U file to write')
    ap.add_argument('--wine-prefix', default=None,
                    help='Wine-style prefix for M3U paths, e.g. Z:\\home\\lw\\Music')
    args = ap.parse_args()

    import os
    os.makedirs(args.out, exist_ok=True)
    cookie = load_cookie_header(args.cookies)

    req = urllib.request.Request(args.playlist, headers={'User-Agent': UA, 'Cookie': cookie})
    html = urllib.request.urlopen(req, timeout=60).read().decode('utf-8', 'replace')
    pl = parse_playlist_page(html)
    tracks = pl.get('tracks', [])
    print(f'playlist: {pl.get("title")!r} — {len(tracks)} tracks')

    m3u_lines = []
    failures = []
    for idx, t in enumerate(tracks, 1):
        track_id = t['id']
        try:
            meta = get_track_meta(cookie, track_id)
            if not meta.get('available', True):
                raise RuntimeError('track not available')
            try:
                di = get_download_info(cookie, track_id, 'lossless')
            except Exception:
                di = get_download_info(cookie, track_id, 'nq')
            codec = di.get('codec') or 'mp3'
            ext = 'flac' if codec == 'flac' else 'mp3'
            artist = ', '.join(a['name'] for a in meta.get('artists', []) if a.get('name'))
            title = meta.get('title') or f'track {track_id}'
            fname = f'{idx:02d} - {sanitize(artist)} - {sanitize(title)}.{ext}'
            path = os.path.join(args.out, fname)
            req = urllib.request.Request(di['url'], headers={'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=300) as r, open(path, 'wb') as f:
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
            size = os.path.getsize(path)
            print(f'[{idx:02d}/{len(tracks)}] {artist} — {title}  ({size / 1e6:.1f} MB, {codec} {di.get("bitrate")}k)')
            m3u_lines.append(fname)
        except Exception as e:
            print(f'[{idx:02d}/{len(tracks)}] FAILED track {track_id}: {e}', file=sys.stderr)
            failures.append((idx, track_id, str(e)))

    if args.m3u:
        if args.wine_prefix:
            prefix = args.wine_prefix.rstrip('\\')
            lines = [f'{prefix}\\{f}' for f in m3u_lines]
        else:
            lines = [os.path.join(args.out, f) for f in m3u_lines]
        # utf-8: --out may contain non-ASCII (ascii crashed after all
        # downloads had finished); M3U players handle UTF-8 fine
        with open(args.m3u, 'w', encoding='utf-8', newline='\n') as f:
            f.write('\n'.join(lines) + '\n')
        print(f'M3U written: {args.m3u} ({len(lines)} entries)')

    print(f'done: {len(m3u_lines)} ok, {len(failures)} failed')
    for idx, tid, err in failures:
        print(f'  failed [{idx:02d}] track {tid}: {err}', file=sys.stderr)
    sys.exit(1 if failures and not m3u_lines else 0)


if __name__ == '__main__':
    main()
