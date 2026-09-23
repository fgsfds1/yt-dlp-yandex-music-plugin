#!/usr/bin/env python3
"""
Extract the current Yandex Music web frontend signing key.

The key is an app-level secret hardcoded in the music-web frontend bundle.

In frontend v4.1603.1+ the layout is a per-platform config object:

    player:{secretKey:{web:"7tvSmFbyf5hJnIHhCimDDD",
                       win32:"...",darwin:"...",linux:"..."},...}

In frontend v4.1520.1 the layout is:

* a config module (e.g. 52708 in chunk 2708-*.js / 35616 in 5616-*.js)
  builds the player config:
      player: { ..., secretKey: (0, a.E)(), ... }
  where `a = n(25079)` — i.e. it imports webpack module 25079.
* module 25079 (in chunk 8290-*.js, lazy-loaded on playlist/track pages)
  is:
      25079:(e,t,a)=>{"use strict";function i(){return"7tvSmFbyf5hJnIHhCimDDD"}
                   a.d(t,{E:()=>i}),...}

The key module is NOT in the homepage bundle, so this tool:

1. fetches a page (default: the homepage; pass --url <playlist/track page>
   for a more complete chunk set),
2. collects chunk URLs from the page HTML (script tags + RSC payload),
3. fetches the webpack runtime and decodes its full lazy-chunk map
   (`a.u`: literal names + two hash tables -> "<prefix|id>.<suffix>.js"),
4. downloads all chunks,
5. resolves the consumer chain `secretKey:(0,VAR.E)()` -> import id ->
   module definition -> string literal returned by the `E` export,
   with a fallback scan for the distinctive key-module shape
   `function X(){return"KEY"}` exported as `E`, cross-checked against
   the consumer import ids.

Usage:
    python3 tools/extract_secret_key.py                # print the key
    python3 tools/extract_secret_key.py --check        # also compare with
                                                       # the plugin constant
    python3 tools/extract_secret_key.py --url https://music.yandex.ru/playlists/<uuid>
"""

import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36')

MODULE_DEF = re.compile(r'(\d+):\s*(?:function\s*)?\([^)]*\)\s*(?:=>\s*)?\{')


def fetch(url):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    return urllib.request.urlopen(req, timeout=30).read().decode('utf-8', 'replace')


def chunk_urls_from_html(html):
    urls = set(re.findall(r'https://[^"\s]+?/static/chunks/[A-Za-z0-9_./()%-]+\.js', html))
    m = re.search(r'"p":"(https://[^"]+/static/chunks/)"', html)
    if m:
        prefix = m.group(1)
        for rel in re.findall(r'static/chunks/([A-Za-z0-9_./()%-]+\.js)', html):
            urls.add(prefix + rel)
    return urls


def chunk_urls_from_runtime(html):
    """Decode the webpack runtime's lazy chunk map (a.u function)."""
    m = re.search(r'https://[^"\s]+?/static/chunks/webpack-[A-Za-z0-9._-]+\.js', html)
    if not m:
        return set()
    js = fetch(m.group(0))
    i = js.find('a.u=')
    if i == -1:
        return set()
    j = js.find(',a.', i + 10)
    seg = js[i:j if j != -1 else i + 6000]
    base = m.group(0).rsplit('/static/chunks/', 1)[0] + '/static/chunks/'
    urls = set()
    for name in re.findall(r'"(static/chunks/[^"]+\.js)"', seg):
        urls.add(base + name)
    # generated names: "static/chunks/"+(prefixTable[e]||e)+"."+(suffixTable[e])
    tables = re.findall(r'\(\{([^}]+)\}\)\[e\]', seg)
    if len(tables) >= 2:
        t_prefix = dict(re.findall(r'(\d+):"([a-f0-9]+)"', tables[-2]))
        t_suffix = dict(re.findall(r'(\d+):"([a-f0-9]+)"', tables[-1]))
        for cid, suffix in t_suffix.items():
            prefix = t_prefix.get(cid, cid)
            urls.add(f'{base}{prefix}.{suffix}.js')
    return urls


def module_at(js, pos):
    """Return (module_id, body_start) of the webpack module definition
    enclosing position pos (searches backwards for `NNNN:(a,b,c)=>{`)."""
    best = None
    for dm in MODULE_DEF.finditer(js[:pos]):
        best = dm
    if best:
        return int(best.group(1)), best.start()
    return None, None


def find_consumer_imports(js):
    """Return the set of webpack module ids whose `E` export is used as a
    secretKey producer:  secretKey:(0,<var>.E)()  with  <var>=o(<id>)
    (the import is resolved inside the consumer's own module body)."""
    ids = set()
    for m in re.finditer(r'secretKey\s*:\s*\(\s*0\s*,\s*([A-Za-z_$][\w$]*)\.E\s*\)\s*\(\s*\)', js):
        var = m.group(1)
        _, body_start = module_at(js, m.start())
        if body_start is None:
            continue
        body = js[body_start:m.start() + 3000]
        im = re.search(re.escape(var) + r'\s*=\s*[A-Za-z_$][\w$]*\(\s*(\d+)\s*\)', body)
        if im:
            ids.add(int(im.group(1)))
    return ids


def extract_key_from_module(js, module_id):
    # (?<!\d): avoid matching a longer id (125079 contains 25079)
    m = re.search(rf'(?<!\d){module_id}\s*:\s*\(', js)
    if not m:
        return None
    body = js[m.start(): m.start() + 3000]
    em = re.search(r'\.d\s*\(\s*\w+\s*,\s*\{([^}]*)\}\s*\)', body)
    if not em:
        return None
    for part in em.group(1).split(','):
        pm = re.match(r'\s*E\s*:\s*\(\s*\)\s*=>\s*([A-Za-z_$][\w$]*)', part)
        if not pm:
            continue
        fn = pm.group(1)
        fm = re.search(rf'function\s+{re.escape(fn)}\s*\(\s*\)\s*\{{\s*return\s*"([^"]+)"\s*\}}', body)
        if fm:
            return fm.group(1)
    return None


def fallback_key_scan(js):
    """Return {key: set(module_ids)} for the distinctive key-module shape:
    a module containing function X(){return"KEY"} with X exported as E."""
    found = {}
    for m in MODULE_DEF.finditer(js):
        mod_id = int(m.group(1))
        body = js[m.start(): m.start() + 3000]
        for fm in re.finditer(
                r'function\s+([A-Za-z_$][\w$]*)\s*\(\s*\)\s*\{\s*return\s*"([A-Za-z0-9]{16,32})"\s*\}',
                body):
            fn, key = fm.group(1), fm.group(2)
            if re.search(rf'E\s*:\s*\(\s*\)\s*=>\s*{re.escape(fn)}\b', body):
                found.setdefault(key, set()).add(mod_id)
    return found


# key locations in the bundle, newest layout first:
#   v4.1603.1+:  player:{secretKey:{web:"KEY",win32:"...",...}}
#   older:       player:{...,secretKey:"KEY",...}
KEY_PATTERNS = (
    re.compile(r'secretKey\s*:\s*\{\s*web\s*:\s*"([A-Za-z0-9]{8,64})"'),
    re.compile(r'secretKey\s*:\s*"([A-Za-z0-9]{8,64})"'),
)


def main():
    args = sys.argv[1:]
    check = '--check' in args
    page_url = 'https://music.yandex.ru/'
    if '--url' in args:
        i = args.index('--url')
        if i + 1 >= len(args):
            print('ERROR: --url requires a value', file=sys.stderr)
            sys.exit(1)
        page_url = args[i + 1]

    html = fetch(page_url)
    urls = chunk_urls_from_html(html) | chunk_urls_from_runtime(html)
    print(f'{len(urls)} chunk URLs (page: {page_url})', file=sys.stderr)

    def _get(u):
        # one retry: a transient 5xx on the key-bearing chunk used to yield
        # the scariest false alarm ("frontend layout may have changed")
        for attempt in (1, 2):
            try:
                return u, fetch(u)
            except Exception:
                if attempt == 2:
                    return u, None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_get, urls))
    chunks = [(u, js) for u, js in results if js]

    # pass 0: literal key in the player config (v4.1603.1+ per-platform
    # object, or an older plain literal)
    key = None
    for url, js in chunks:
        for pat in KEY_PATTERNS:
            m = pat.search(js)
            if m:
                key = m.group(1)
                print(f'  key found via config literal ({url.rsplit("/", 1)[-1]})',
                      file=sys.stderr)
                break
        if key:
            break

    # pass 1: consumer chain  secretKey:(0,VAR.E)()  ->  VAR=o(ID)  -> module ID
    imported_ids = set()
    if not key:
        for url, js in chunks:
            ids = find_consumer_imports(js)
            if ids:
                print(f'  consumer in {url.rsplit("/", 1)[-1]} imports modules {sorted(ids)}',
                      file=sys.stderr)
                imported_ids |= ids

        for mod_id in sorted(imported_ids):
            for url, js in chunks:
                key = extract_key_from_module(js, mod_id)
                if key:
                    print(f'  key found in module {mod_id} ({url.rsplit("/", 1)[-1]})',
                          file=sys.stderr)
                    break
            if key:
                break

    # pass 2: fallback — distinctive key-module shape, cross-checked against
    # the consumer import ids when available
    if not key:
        candidates = {}
        for url, js in chunks:
            for k, mods in fallback_key_scan(js).items():
                candidates.setdefault(k, set()).update(mods)
        if imported_ids:
            candidates = {k: mods for k, mods in candidates.items()
                          if mods & imported_ids}
            print(f'  consumer-verified candidates: {sorted(candidates)}', file=sys.stderr)
        if len(candidates) == 1:
            key = next(iter(candidates))
            print('  key found via fallback module-shape scan', file=sys.stderr)
        elif candidates:
            print('multiple candidates:', *sorted(candidates), sep='\n  ', file=sys.stderr)
            sys.exit(1)

    if not key:
        print('ERROR: key not found — frontend layout may have changed', file=sys.stderr)
        sys.exit(1)

    print(key)
    if check:
        import importlib.util
        import os
        here = os.path.dirname(os.path.abspath(__file__))
        spec = importlib.util.spec_from_file_location(
            'ymv2', os.path.join(here, '..', 'yt_dlp_plugins', 'extractor',
                                 'yandex_music_v2.py'))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if mod._SECRET_KEY == key:
            print('check: OK — matches plugin constant', file=sys.stderr)
        else:
            print(f'check: MISMATCH — plugin has {mod._SECRET_KEY!r}', file=sys.stderr)
            sys.exit(2)


if __name__ == '__main__':
    main()
