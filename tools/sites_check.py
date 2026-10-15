#!/usr/bin/env python3
"""Every site answers as its folder says.

Reads each <site>/wrangler.toml under $FLAREPRESS_SITES (default
~/.config/flarepress/sites) and works out what its routes must answer:
- an alias folder (main = "index.js" whose index.js names "https://<target>")
  must answer 301 with a Location on https://<target>/;
- a move folder (its index.js sets MOVES_TO = "https://<target>": a page at /
  that carries a browser's saved data to <target> before it goes) must answer
  200 at / and 301 to https://<target>/ at any other path;
- every other route must answer 200;
- a folder with a site.conf (an app site) must also serve /version.json.

Read-only GETs, redirects not followed, no secrets. Misses are retried 3 times,
20 s apart, so one blip is not an outage. Run by `just sites [domain ...]`. Stdlib only, Python 3.8+.
"""
import http.client
import os
import re
import sys
import time

# Site folders by name: a folder each, or a link to a site folder in an app repo.
ROOT = os.path.expanduser(os.environ.get('FLAREPRESS_SITES') or '~/.config/flarepress/sites')
ROUTE = re.compile(r'pattern\s*=\s*"([^"]+)"')
MAIN = re.compile(r'^main\s*=\s*"([^"]+)"', re.M)
TARGET = re.compile(r'"https://([^"/]+)')
MOVE = re.compile(r'MOVES_TO\s*=\s*"https://([^"/]+)')


def expectations(root=ROOT, only=()):
    """[(host, path, kind, target)] for every route of every site folder.
    kind is '200' or '301'; target is the redirect host for a 301."""
    out = []
    for name in sorted(os.listdir(root)):
        folder = os.path.join(root, name)
        toml = os.path.join(folder, 'wrangler.toml')
        if not os.path.isfile(toml) or (only and name not in only):
            continue
        with open(toml, encoding='utf-8') as f:
            text = f.read()
        main = MAIN.search(text)
        target = moves = None
        if main and main.group(1) == 'index.js' and os.path.isfile(os.path.join(folder, 'index.js')):
            with open(os.path.join(folder, 'index.js'), encoding='utf-8') as f:
                js = f.read()
            m, mv = TARGET.search(js), MOVE.search(js)
            target = m.group(1) if m else None
            moves = mv.group(1) if mv else None
        app = os.path.isfile(os.path.join(folder, 'site.conf'))
        for host in ROUTE.findall(text):
            if moves:
                out.append((host, '/', '200', None))
                out.append((host, '/moved', '301', moves))
            elif target:
                out.append((host, '/', '301', target))
            else:
                out.append((host, '/', '200', None))
                if app:
                    out.append((host, '/version.json', '200', None))
    return out


def fetch(host, path):
    """(status, location) for one GET; status 0 when the host does not answer."""
    try:
        c = http.client.HTTPSConnection(host, timeout=20)
        c.request('GET', path, headers={'User-Agent': 'flarepress-sites-check'})
        r = c.getresponse()
        r.read()
        return r.status, r.getheader('Location') or ''
    except (OSError, http.client.HTTPException):
        return 0, ''


def met(exp, got):
    _host, _path, kind, target = exp
    status, location = got
    if kind == '301':
        return status == 301 and location.startswith('https://%s/' % target)
    return status == 200


def run(exps, fetch=fetch, sleep=time.sleep, retries=3, wait=20):
    """{exp: (status, location)}, retrying only the misses."""
    results = {e: fetch(e[0], e[1]) for e in exps}
    for _ in range(retries):
        misses = [e for e in exps if not met(e, results[e])]
        if not misses:
            break
        sleep(wait)
        for e in misses:
            results[e] = fetch(e[0], e[1])
    return results


def decide(results):
    """(ok, lines): one line per expectation, ✅ or ❌."""
    lines, ok = [], True
    for exp in sorted(results):
        host, path, kind, target = exp
        status, location = results[exp]
        want = '301 to https://%s/' % target if kind == '301' else '200'
        good = met(exp, results[exp])
        ok = ok and good
        got = '%s%s' % (status or 'no answer', ' -> ' + location if location else '')
        lines.append('%s %s%s: %s (want %s)' % ('✅' if good else '❌', host, path, got, want))
    return ok, lines


def main(argv):
    exps = expectations(only=tuple(argv))
    if not exps:
        print('no site folders matched: %s' % ' '.join(argv))
        return 2
    ok, lines = decide(run(exps))
    for line in lines:
        print(line)
    if not ok and os.environ.get('GITHUB_ACTIONS'):
        print('::error::a flarepress domain does not answer as its folder says')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
