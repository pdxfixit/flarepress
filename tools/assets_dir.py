#!/usr/bin/env python3
"""Print a site's assets folder: assets_dir.py path/to/wrangler.toml

The [assets] table's directory (or an inline assets = { directory = ... }),
joined to the folder wrangler.toml is in;
<that folder>/public when there is none. Stdlib only, Python 3.8+.
"""
import os
import re
import sys

TABLE = re.compile(r'^\s*\[assets\]\s*(?:#.*)?$(.*?)(?=^\s*\[|\Z)', re.M | re.S)
# wrangler also takes the inline form at the top level: assets = { directory = "./dist" }
INLINE = re.compile(r'^\s*assets\s*=\s*\{([^}]*)\}', re.M)
# A TOML basic ("...") or literal ('...') string.
DIRECTORY = re.compile(r"""(?:^|[\s,{])directory\s*=\s*(?:"([^"]+)"|'([^']+)')""", re.M)


def assets_dir(toml_path):
    with open(toml_path, encoding='utf-8') as f:
        text = f.read()
    top = re.split(r'^\s*\[', text, maxsplit=1, flags=re.M)[0]
    table = TABLE.search(text) or INLINE.search(top)
    found = DIRECTORY.search(table.group(1)) if table else None
    # realpath: a site reached through a link resolves from where it really is, as wrangler does
    return os.path.normpath(os.path.join(os.path.dirname(os.path.realpath(toml_path)),
                                         (found.group(1) or found.group(2)) if found else 'public'))


if __name__ == '__main__':
    print(assets_dir(sys.argv[1]))
