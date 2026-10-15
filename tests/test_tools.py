"""tools/fill_ids.py and tools/assets_dir.py: the small helpers wrangler.sh and site.sh use."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools'))
import assets_dir as A  # noqa: E402
import fill_ids as F  # noqa: E402


class FillIds(unittest.TestCase):
    def test_fills_every_name(self):
        self.assertEqual(F.fill('a = "${X}"\nb = "${Y_2}"\n', {'X': '1', 'Y_2': '2'}), 'a = "1"\nb = "2"\n')

    def test_lists_every_missing_or_empty_name(self):
        with self.assertRaises(KeyError) as e:
            F.fill('${A} ${B} ${C}', {'B': '', 'C': 'c'})
        self.assertEqual(e.exception.args[0], ['A', 'B'])

    def test_leaves_other_dollars_alone(self):
        self.assertEqual(F.fill('$X and ${not-a-name}', {}), '$X and ${not-a-name}')


class AssetsDir(unittest.TestCase):
    def check(self, toml, want):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'site', 'wrangler.toml')
            os.makedirs(os.path.dirname(p))
            with open(p, 'w') as f:
                f.write(toml)
            self.assertEqual(A.assets_dir(p), os.path.normpath(os.path.join(os.path.realpath(d), want)))

    def test_relative_directory(self):
        self.check('name = "x"\n\n[assets]\ndirectory = "../web"\nbinding = "ASSETS"\n', 'web')

    def test_public_when_none(self):
        self.check('name = "x"\nmain = "index.js"\n', 'site/public')

    def test_only_the_assets_table(self):
        self.check('[build]\ndirectory = "nope"\n\n[assets]\ndirectory = "./out"\n', 'site/out')


if __name__ == '__main__':
    unittest.main()
