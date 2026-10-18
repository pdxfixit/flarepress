#!/usr/bin/env python3
"""Resolve conflicts inside long lines word by word (docs/guide.md, decisions.md).

The docs keep a paragraph on one long line, so two landings that change
different sentences of it conflict as whole lines. For each conflict block in
diff3 style (<<<<<<< ours, ||||||| base, ======= theirs, >>>>>>>), this splits
the three versions into words and runs `git merge-file` on them: when the
edits touch different words, the merged text replaces the block; when they
touch the same words, the block is left for a person.

Usage: resolve_words.py FILE...   Prints what it did; exits 1 if any conflict is left.
"""

import os
import re
import subprocess
import sys
import tempfile

BLOCK = re.compile(
    r'^<<<<<<< [^\n]*\n(?P<ours>.*?)^\|\|\|\|\|\|\| [^\n]*\n(?P<base>.*?)^=======\n(?P<theirs>.*?)^>>>>>>> [^\n]*\n',
    re.S | re.M)


NL = '\u2029'   # a line break (PARAGRAPH SEPARATOR, never in the docs), written as a token of its own so merging keeps it


def words(text):
    # One token per line: a word with the spaces after it, or a line break.
    return ''.join((NL if t == '\n' else t) + '\n' for t in re.findall(r'\n|[^\n ]+ *| +', text))


def unwords(merged):
    return ''.join(t.replace(NL, '\n') for t in merged.split('\n')[:-1])


def merge(ours, base, theirs):
    with tempfile.TemporaryDirectory() as d:
        paths = []
        for name, text in (('ours', ours), ('base', base), ('theirs', theirs)):
            p = os.path.join(d, name)
            with open(p, 'w') as f:
                f.write(words(text))
            paths.append(p)
        r = subprocess.run(['git', 'merge-file', '-p', *paths], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return unwords(r.stdout)


def resolve(path):
    with open(path) as f:
        text = f.read()
    done = 0

    def one(m):
        nonlocal done
        merged = merge(m['ours'], m['base'], m['theirs'])
        if merged is None:
            return m[0]
        done += 1
        return merged

    out = BLOCK.sub(one, text)
    if done:
        with open(path, 'w') as f:
            f.write(out)
    left = len(re.findall(r'^<<<<<<< ', out, re.M))
    print(f'{"✅" if not left else "⚠️ "} {path}: {done} conflict(s) merged word by word, {left} left')
    return left


if __name__ == '__main__':
    sys.exit(1 if sum(resolve(p) for p in sys.argv[1:]) else 0)
