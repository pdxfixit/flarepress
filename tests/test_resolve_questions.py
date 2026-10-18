"""tools/resolve_questions.py: both sides' cards kept, the count recounted."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools'))
from resolve_questions import resolve, words  # noqa: E402

BASE = """# Questions

## Open

Two provisional cards. The owner's own open decision is in TODO.

### Q1. A (provisional)

**Answer:**

"""


class ResolveQuestions(unittest.TestCase):
    def test_words(self):
        self.assertEqual([words(n) for n in (0, 19, 20, 22, 40)], ['Zero', 'Nineteen', 'Twenty', 'Twenty-two', 'Forty'])

    def test_both_new_cards_kept_and_counted(self):
        text = BASE.replace('Two provisional', '<<<<<<< HEAD\nTwo provisional cards. x\n=======\nTwo provisional cards. x\n>>>>>>> c\nTwo provisional', 1)
        text += ("<<<<<<< HEAD\n### Q2. B (provisional)\n\n**Answer:**\n\n"
                 "||||||| parent\n=======\n### Q3. C (provisional)\n\n**Answer:**\n\n>>>>>>> c (feat)\n## Card template\n")
        out = resolve(text)
        self.assertNotIn('<<<<<<<', out)
        self.assertIn('### Q2.', out)
        self.assertIn('### Q3.', out)
        self.assertEqual(out.count('provisional cards.'), 1)
        self.assertIn('Three provisional cards.', out)


    def test_main_deleting_cards_is_kept(self):
        text = ("## Open\n\n<<<<<<< HEAD\nNone.\n\n||||||| base\n### Q1. A\n\n**Answer:**\n\n"
                "=======\n### Q1. A changed\n\n**Answer:**\n\n### Q2. New\n\n**Answer:**\n\n>>>>>>> c\n")
        out = resolve(text)
        self.assertNotIn('### Q1.', out, 'a card main deleted stays deleted')
        self.assertIn('### Q2. New', out, 'a card the commit added is kept')


if __name__ == '__main__':
    unittest.main()
