"""resolve_words.py: conflicts inside the docs' long lines, merged word by word."""

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools'))
import resolve_words  # noqa: E402


def block(ours, base, theirs):
    return f'<<<<<<< HEAD\n{ours}||||||| parent\n{base}=======\n{theirs}>>>>>>> abc (fix)\n'


class ResolveWords(unittest.TestCase):
    def run_on(self, text):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'guide.md')
            with open(p, 'w') as f:
                f.write(text)
            left = resolve_words.resolve(p)
            with open(p) as f:
                return left, f.read()

    def test_edits_to_different_sentences_of_one_line_both_land(self):
        base = 'One sentence here. Two sentence there. Three.\n'
        text = 'before\n' + block('One sentence HERE. Two sentence there. Three.\n', base,
                                  'One sentence here. Two sentence there. Three, now longer.\n') + 'after\n'
        left, out = self.run_on(text)
        self.assertEqual(left, 0)
        self.assertEqual(out, 'before\nOne sentence HERE. Two sentence there. Three, now longer.\nafter\n')

    def test_line_breaks_inside_a_block_are_kept(self):
        # 2026-10-18: the first version joined the merged paragraph onto the next line.
        base = 'a long line\n  and the next one\n'
        left, out = self.run_on(block('a LONG line\n  and the next one\n', base, 'a long line\n  and the NEXT one\n') + 'tail\n')
        self.assertEqual(left, 0)
        self.assertEqual(out, 'a LONG line\n  and the NEXT one\ntail\n')

    def test_the_same_words_changed_both_ways_are_left_for_a_person(self):
        text = block('one X three\n', 'one two three\n', 'one Y three\n')
        left, out = self.run_on(text)
        self.assertEqual(left, 1)
        self.assertEqual(out, text)

    def test_the_command_exits_1_while_a_conflict_is_left(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'g.md')
            with open(p, 'w') as f:
                f.write(block('one X\n', 'one two\n', 'one Y\n'))
            r = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools', 'resolve_words.py'), p], capture_output=True)
            self.assertEqual(r.returncode, 1)


if __name__ == '__main__':
    unittest.main()
