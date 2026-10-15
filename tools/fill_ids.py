#!/usr/bin/env python3
"""Fill ${NAME} in a wrangler.toml from the environment: fill_ids.py SRC DST

A public app repo keeps IDs out of its wrangler.toml (a D1 database_id, say) by
writing ${NAME} there; tools/wrangler.sh writes the filled copy for wrangler.
Every name must have a non-empty value, or nothing is written and it exits 1
naming each missing one. Stdlib only, Python 3.8+.
"""
import os
import re
import sys

NAME = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}')


def fill(text, env):
    """text with each ${NAME} replaced from env; raises KeyError listing the missing names."""
    missing = sorted({m.group(1) for m in NAME.finditer(text) if not env.get(m.group(1))})
    if missing:
        raise KeyError(missing)
    return NAME.sub(lambda m: env[m.group(1)], text)


def main(argv):
    if len(argv) != 2:
        print('usage: fill_ids.py SRC DST', file=sys.stderr)
        return 2
    src, dst = argv
    with open(src, encoding='utf-8') as f:
        text = f.read()
    try:
        out = fill(text, os.environ)
    except KeyError as e:
        names = ' '.join(e.args[0])
        print('🛑 %s needs %s in the environment (a GitHub secret on CI; the app\'s config on a Mac)' % (src, names),
              file=sys.stderr)
        return 1
    with open(dst, 'w', encoding='utf-8') as f:
        f.write(out)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
