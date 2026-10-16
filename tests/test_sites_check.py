"""Tests for tools/sites_check.py: what each folder must answer, and the verdict."""
import contextlib
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools'))
import sites_check as S  # noqa: E402


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


class Expectations(unittest.TestCase):
    def test_alias_static_and_app_folders(self):
        with tempfile.TemporaryDirectory() as root:
            write(os.path.join(root, 'old.com/wrangler.toml'), 'main = "index.js"\nroutes = [\n  { pattern = "old.com", custom_domain = true },\n  { pattern = "www.old.com", custom_domain = true }\n]\n')
            write(os.path.join(root, 'old.com/index.js'), 'const t = new URL(p, "https://new.com");')
            write(os.path.join(root, 'new.com/wrangler.toml'), 'routes = [ { pattern = "new.com", custom_domain = true } ]\n[assets]\ndirectory = "./public"\n')
            write(os.path.join(root, 'app.new.com/wrangler.toml'), 'main = "worker/index.js"\nroutes = [ { pattern = "app.new.com", custom_domain = true } ]\n')
            write(os.path.join(root, 'app.new.com/site.conf'), 'committed=web\n')
            write(os.path.join(root, 'notes/README.md'), 'not a site')
            got = S.expectations(root)
        self.assertEqual(got, [
            ('app.new.com', '/', '200', None), ('app.new.com', '/version.json', '200', None),
            ('new.com', '/', '200', None),
            ('old.com', '/', '301', 'new.com'), ('www.old.com', '/', '301', 'new.com'),
        ])

    def test_move_folder_serves_its_page_and_redirects_the_rest(self):
        with tempfile.TemporaryDirectory() as root:
            write(os.path.join(root, 'was.com/wrangler.toml'), 'main = "index.js"\nroutes = [ { pattern = "was.com", custom_domain = true } ]\n')
            write(os.path.join(root, 'was.com/index.js'), 'const MOVES_TO = "https://now.app";\nconst PAGE = `<a href="${MOVES_TO}/">`;')
            got = S.expectations(root)
        self.assertEqual(got, [('was.com', '/', '200', None), ('was.com', '/moved', '301', 'now.app')])

    def test_only_named_folders(self):
        with tempfile.TemporaryDirectory() as root:
            write(os.path.join(root, 'a.com/wrangler.toml'), 'routes = [ { pattern = "a.com" } ]\n')
            write(os.path.join(root, 'b.com/wrangler.toml'), 'routes = [ { pattern = "b.com" } ]\n')
            self.assertEqual([e[0] for e in S.expectations(root, only=('b.com',))], ['b.com'])


    def test_no_sites_folder_says_so(self):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(out):
            code = S.main([], root=os.path.join(root, 'missing'))
        self.assertEqual(code, 2)
        self.assertIn('no sites folder', out.getvalue())


class Decide(unittest.TestCase):
    A = ('old.com', '/', '301', 'new.com')
    B = ('new.com', '/', '200', None)

    def test_all_good(self):
        ok, lines = S.decide({self.A: (301, 'https://new.com/'), self.B: (200, '')})
        self.assertTrue(ok)
        self.assertTrue(all(l.startswith('✅') for l in lines))

    def test_redirect_to_the_wrong_place_fails(self):
        ok, _ = S.decide({self.A: (301, 'https://elsewhere.com/'), self.B: (200, '')})
        self.assertFalse(ok)

    def test_no_answer_fails(self):
        ok, lines = S.decide({self.A: (301, 'https://new.com/'), self.B: (0, '')})
        self.assertFalse(ok)
        self.assertIn('no answer', '\n'.join(lines))


class Run(unittest.TestCase):
    def test_retries_only_misses_and_recovers(self):
        a, b = ('a.com', '/', '200', None), ('b.com', '/', '200', None)
        calls, sleeps = [], []
        answers = {'a.com': [(200, '')], 'b.com': [(503, ''), (200, '')]}

        def fetch(host, path):
            calls.append(host)
            return answers[host].pop(0)
        results = S.run([a, b], fetch=fetch, sleep=sleeps.append)
        self.assertEqual(S.decide(results)[0], True)
        self.assertEqual(calls, ['a.com', 'b.com', 'b.com'])
        self.assertEqual(len(sleeps), 1)

    def test_gives_up_after_the_retries(self):
        a = ('a.com', '/', '200', None)
        sleeps = []
        results = S.run([a], fetch=lambda h, p: (0, ''), sleep=sleeps.append)
        self.assertFalse(S.decide(results)[0])
        self.assertEqual(len(sleeps), 3)


if __name__ == '__main__':
    unittest.main()
