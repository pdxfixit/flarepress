"""tools/events_vendor.py: copies the events files into an app with a header, and catches edited or stale copies."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, 'tools', 'events_vendor.py')


class Vendor(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='events-vendor.')
        self.web = os.path.join(self.dir, 'web')
        self.worker = os.path.join(self.dir, 'worker')
        os.makedirs(self.web)
        os.makedirs(self.worker)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def run_tool(self, *args):
        r = subprocess.run([sys.executable, TOOL] + list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True)
        return r.returncode, r.stdout

    def read(self, *parts):
        with open(os.path.join(self.dir, *parts), 'rb') as f:
            return f.read()

    def test_copies_with_a_header_and_checks_clean(self):
        code, out = self.run_tool('--client', self.web, '--worker', self.worker)
        self.assertEqual(code, 0, out)
        with open(os.path.join(ROOT, 'events', 'events-client.js'), 'rb') as f:
            original = f.read()
        copy = self.read('web', 'events-client.js')
        head, body = copy.split(b'\n', 3)[:3], copy.split(b'\n', 3)[3]
        self.assertEqual(body, original, 'the body is the file byte for byte')
        self.assertIn(b'Vendored from pdxfixit/flarepress events/events-client.js', head[0])
        self.assertTrue(all(h.startswith(b'// ') for h in head), 'the header is JavaScript comments')
        self.assertTrue(os.path.exists(os.path.join(self.worker, 'events-worker.js')))
        code, out = self.run_tool('--client', self.web, '--worker', self.worker, '--check')
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count('✅'), 2)

    def test_standalone_brings_the_worker_it_imports(self):
        code, out = self.run_tool('--standalone', self.worker)
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(os.listdir(self.worker)), ['events-standalone.js', 'events-worker.js'])
        self.assertEqual(self.run_tool('--standalone', self.worker, '--check')[0], 0)

    def test_check_catches_a_missing_an_edited_and_a_stale_copy(self):
        code, out = self.run_tool('--client', self.web, '--check')
        self.assertEqual((code, 'missing' in out), (1, True), out)
        self.run_tool('--client', self.web)
        path = os.path.join(self.web, 'events-client.js')
        copy = self.read('web', 'events-client.js')
        with open(path, 'wb') as f:
            f.write(copy.replace(b'export const MAX_BYTES', b'export const MAX_BYTES_X'))
        code, out = self.run_tool('--client', self.web, '--check')
        self.assertEqual((code, 'edited in the app' in out), (1, True), out)
        # a copy that was right once, of an older flarepress file: its own header still matches
        old = b'// old file\n'
        import hashlib
        with open(path, 'wb') as f:
            f.write(b'// Vendored from pdxfixit/flarepress events/events-client.js. Do not edit it here.\n'
                    b'// Update\n// sha256 of the original: ' + hashlib.sha256(old).hexdigest().encode() + b'\n' + old)
        code, out = self.run_tool('--client', self.web, '--check')
        self.assertEqual((code, 'behind flarepress' in out), (1, True), out)
        with open(path, 'wb') as f:
            f.write(b'export const x = 1;\n')
        code, out = self.run_tool('--client', self.web, '--check')
        self.assertEqual((code, 'no header' in out), (1, True), out)

    def test_refuses_no_target_and_a_missing_folder(self):
        self.assertEqual(self.run_tool()[0], 2)
        code, out = self.run_tool('--client', os.path.join(self.dir, 'nope'))
        self.assertEqual((code, 'not a folder' in out), (1, True), out)


if __name__ == '__main__':
    unittest.main()
