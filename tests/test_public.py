"""This repo is public: no file in it names a domain, a person or an account.

Site folders with real domains live in each app's repo or under
~/.config/flarepress/sites, never here. Tests and docs use reserved examples
(example.com, *.test, *.invalid) or the few placeholders listed below. The
family name is built from parts so this file does not contain it.
"""
import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DOMAIN = re.compile(r'\b(?:[a-z0-9-]+\.)+(?:com|net|org|app|io|dev|co|us)\b', re.I)
ALLOWED = {
    'example.com', 'github.com', 'api.github.com', 'api.cloudflare.com', 'dash.cloudflare.com',
    'developers.cloudflare.com', 'cloudflare.com', 'registry.npmjs.org', 'users.noreply.github.com',
    # placeholders in tests/test_sites_check.py and tools/sites_check.py
    'old.com', 'new.com', 'app.new.com', 'www.old.com', 'was.com', 'a.com', 'b.com', 'out.app',
    'elsewhere.com', 'now.app',
    # public endpoints in tools/events_job.py and its tests: R2's S3 API, and AWS's published SigV4 examples
    'r2.cloudflarestorage.com', 's3.amazonaws.com', 'examplebucket.s3.amazonaws.com',
}


def allowed(domain):
    return domain in ALLOWED or domain.endswith('.example.com')
PRIVATE = ('sand' + 'berg', 'family' + ' domains', '/users/' + 'ben')
HEX_ID = re.compile(r'\b[0-9a-f]{32}\b|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b')


def tracked():
    try:
        out = subprocess.run(['git', 'ls-files'], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        files = out.split()
    except (OSError, subprocess.CalledProcessError):
        files = []
    if not files:  # not a git checkout yet: every file but .git
        for d, dirs, names in os.walk(ROOT):
            dirs[:] = [x for x in dirs if x not in ('.git', '__pycache__')]
            files += [os.path.relpath(os.path.join(d, n), ROOT) for n in names]
    return [f for f in files if not f.endswith(('.pyc', '.png'))]


class Public(unittest.TestCase):
    def test_no_private_text(self):
        for rel in tracked():
            with open(os.path.join(ROOT, rel), encoding='utf-8', errors='replace') as f:
                text = f.read()
            low = text.lower()
            with self.subTest(file=rel):
                for word in PRIVATE:
                    self.assertNotIn(word, low, 'a private word in %s' % rel)
                stray = sorted(d for d in {d.lower() for d in DOMAIN.findall(text)} if not allowed(d))
                self.assertEqual(stray, [], 'domains in %s: use example.com or *.test' % rel)
                self.assertIsNone(HEX_ID.search(low), 'an account, zone or database id in %s' % rel)


if __name__ == '__main__':
    unittest.main()
