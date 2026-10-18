#!/usr/bin/env python3
"""Resolves a docs/questions.md landing conflict: agents each add cards and
bump the "N provisional cards." line, so every land conflicted (2026-10-12).

Keeps main's side of each conflict, deletions included, and adds the cards
the commit made new (absent from the merge base), then recounts the count
line if there is one. Needs diff3 conflicts (merge.conflictstyle) to tell a
new card from one main deleted; without a base, a card main deleted comes back. Usage: resolve_questions.py docs/questions.md (land.sh runs it; land.conf:
resolve=docs/questions.md:questions)
"""
import re
import sys

ONES = ['Zero', 'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven', 'Eight', 'Nine', 'Ten', 'Eleven',
        'Twelve', 'Thirteen', 'Fourteen', 'Fifteen', 'Sixteen', 'Seventeen', 'Eighteen', 'Nineteen']
TENS = ['', '', 'Twenty', 'Thirty', 'Forty', 'Fifty', 'Sixty', 'Seventy', 'Eighty', 'Ninety']


def words(n):
    """A count as the card line writes it: Nineteen, Twenty-two."""
    if n < 20:
        return ONES[n]
    t, o = divmod(n, 10)
    return TENS[t] + ('-' + ONES[o].lower() if o else '')


HUNK = re.compile(r'<<<<<<< [^\n]*\n(.*?)(?:\|\|\|\|\|\|\| [^\n]*\n(.*?))?=======\n(.*?)>>>>>>> [^\n]*\n', re.S)


def cards(text):
    """A hunk's cards, by their number (a title may change on either side)."""
    return {re.match(r'### Q(\S+?)\.', b).group(1): b for b in re.split(r'(?=^### Q)', text, flags=re.M) if re.match(r'### Q\S+?\.', b)}


def resolve(text):
    # Main's side stands, deletions included (main deleted answered cards on
    # 2026-10-12, and an older version of this tool brought them all back).
    # From the commit's side only cards new to it are added: not in the base.
    def both(m):
        ours, base, theirs = m.group(1), m.group(2) or '', m.group(3)
        out, have, old = ours, cards(ours), cards(base)
        for head, block in cards(theirs).items():
            if head not in have and head not in old:
                out += ('' if out.endswith('\n\n') or not out else '\n') + block
        return out
    text = HUNK.sub(both, text)
    # Keep one count line (both sides may have bumped it), then recount.
    lines, seen = [], False
    for line in text.split('\n'):
        if re.match(r'^[\w-]+ provisional cards?\.', line):
            if seen:
                continue
            seen = True
        lines.append(line)
    text = '\n'.join(lines)
    n = len(re.findall(r'^### Q\d+\.', text, flags=re.M))
    return re.sub(r'^[\w-]+ provisional cards?\.', f'{words(n)} provisional card{"s" if n != 1 else ""}.', text, count=1, flags=re.M)


if __name__ == '__main__':
    path = sys.argv[1]
    with open(path, encoding='utf-8') as f:
        text = f.read()
    out = resolve(text)
    if '<<<<<<<' in out or '>>>>>>>' in out:
        sys.exit(f'🛑 {path}: a conflict this tool cannot resolve; do it by hand')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(out)
    print(f'✅ {path}: cards kept from both sides, count line recounted')
