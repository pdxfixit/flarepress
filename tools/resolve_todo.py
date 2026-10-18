#!/usr/bin/env python3
"""Resolve a cherry-pick conflict in a list file like TODO.md or questions.md.

Agents land by cherry-pick, and each deletes its own TODO line while others
keep adding lines, so the same conflict comes up on almost every landing.
For each conflict hunk (diff3 style, merge.conflictStyle=diff3 or zdiff3):
keep main's side (HEAD) and drop the lines the incoming commit deleted from
the base; keep any line the incoming commit added. Stdlib only.

    python3 tools/resolve_todo.py TODO.md [more files...]   (land.sh runs it; land.conf: resolve=TODO.md:todo)
"""
import re
import sys
from collections import Counter
from pathlib import Path

HUNK = re.compile(r"<<<<<<< [^\n]*\n(.*?)\|\|\|\|\|\|\| [^\n]*\n(.*?)=======\n(.*?)>>>>>>> [^\n]*\n", re.S)


def resolve(text):
    def fix(m):
        ours, base, theirs = (g.splitlines(keepends=True) for g in m.groups())
        # Count lines, never treat them as a set: in CSS every "}" line looks
        # the same, and a set-based rule once dropped 12 braces (2026-10-11).
        dropped = Counter(base) - Counter(theirs)
        added = Counter(theirs) - Counter(base)
        out = []
        for l in ours:
            if l.strip() and dropped[l] > 0:
                dropped[l] -= 1
                continue
            out.append(l)
        new = []
        for l in theirs:
            if added[l] > 0:
                added[l] -= 1
                new.append(l)
        # The commit's additions go in as one block, unless main already holds
        # that exact block (both sides added the same TODO line). Comparing
        # line by line can't tell two different CSS blocks that both end in "}"
        # from the same one.
        if "".join(new) not in "".join(ours):
            out.extend(new)
        return "".join(out)
    out, n = HUNK.subn(fix, text)
    return out, n


def main(paths):
    status = 0
    for p in map(Path, paths):
        text, n = resolve(p.read_text())
        if "<<<<<<<" in text or ">>>>>>>" in text:
            print(f"🛑 {p}: a conflict without a base section (set merge.conflictStyle=zdiff3)", file=sys.stderr)
            status = 1
            continue
        p.write_text(text)
        print(f"✅ {p}: {n} conflict(s) resolved")
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
