"""tools/events_job.py, the telemetry runner's job for event batches in R2.

The R2 here is a stand-in: a small S3 server on 127.0.0.1 holding objects in memory. It checks every request's
signature with events_job.sigv4, answers ListObjectsV2 two objects a page, and counts each call, so the job runs as
it would against Cloudflare. Stdlib only, Python 3.8+.
"""
import datetime
import hashlib
import http.server
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from xml.sax.saxutils import escape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOB = os.path.join(ROOT, 'tools', 'events_job.py')
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import events_job as job  # noqa: E402

KEY_ID, SECRET = 'test-key-id', 'test-secret'
BUCKET = 'isopix-events'
PAGE = 2   # the stand-in's page size, so a listing always pages

# The event list's shapes, as the telemetry repo's events.toml writes them.
EVENTS_TOML = '''# a comment
[apps.isopix]
platform = "web"   # trailing comment
forms = ["phone", "tablet", "desktop"]
backlog = "pdxfixit/isopix TODO.md"

[apps.orbit]
platform = "android"
forms = ["watch"]

[events."app.open"]
category = "lifecycle"
why = "How often each app \\"starts\\""
details = ["first_run", "vw", "vh"]
summary = ["first_run"]
apps = ["isopix", "orbit"]

[events."control.use"]
category = "controls"
details = [
  "control", "group",   # where it sits
  "layout", "input",
]
summary = ["control", "layout"]
apps = ["isopix"]
skip = { orbit = "no toolbar", other = "x" }

[events."screen.view"]
details = ["from"]
summary = []
apps = ["isopix"]
'''


def record(seq, event='control.use', session='s-1', player='p-1', at=1792022400000, **details):
    """A record as events-client.js sends it; at is 2026-10-15 00:00 UTC."""
    return {'app': 'isopix', 'platform': 'web', 'form': 'phone', 'version': '1.2.3', 'player': player,
            'session': session, 'seq': seq, 'at': at, 'tz': -420, 'event': event, 'screen': 'canvas',
            'details': details}


def body(*records):
    return ''.join(json.dumps(r) + '\n' for r in records).encode('utf-8')


def okey(batch, day='2026-10-15'):
    return 'v1/%s/%s.jsonl' % (day, batch)


# built from parts: tests/test_public.py reads any whole UUID as an account id
B1, B2, B3, B4 = ('-'.join(['0b9e4c2a', '7d31', '4f5e', '9a8b', '1c2d3e4f5a6%d' % n]) for n in (1, 2, 3, 4))


class FakeR2(http.server.BaseHTTPRequestHandler):
    objects = {}
    calls = []
    fail_with = None

    def log_message(self, *args):
        pass

    def answer(self, code, data=b''):
        self.send_response(code)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def signed(self, path, query):
        when = datetime.datetime.strptime(self.headers['x-amz-date'], '%Y%m%dT%H%M%SZ').replace(
            tzinfo=datetime.timezone.utc)
        want = job.sigv4(self.command, self.headers['host'], path, {}, b'', KEY_ID, SECRET, 'auto', when, query)
        return self.headers.get('authorization') == want['authorization']

    def route(self):
        url = urllib.parse.urlsplit(self.path)
        query = dict(urllib.parse.parse_qsl(url.query, keep_blank_values=True))
        if FakeR2.fail_with:
            return self.answer(FakeR2.fail_with)
        if not self.signed(url.path, query):
            return self.answer(403, b'<Error><Code>SignatureDoesNotMatch</Code></Error>')
        bucket, _, k = urllib.parse.unquote(url.path).lstrip('/').partition('/')
        if bucket != BUCKET:
            return self.answer(404)
        FakeR2.calls.append((self.command, k or 'list'))
        if self.command == 'GET' and not k:
            return self.answer(200, self.listing(query))
        if self.command == 'GET':
            return self.answer(200, FakeR2.objects[k]) if k in FakeR2.objects else self.answer(404)
        if self.command == 'DELETE':
            if k not in FakeR2.objects:
                return self.answer(404)
            del FakeR2.objects[k]
            return self.answer(204)
        return self.answer(405)

    def listing(self, query):
        keys = sorted(k for k in FakeR2.objects if k.startswith(query.get('prefix', '')))
        start = int(query.get('continuation-token', '0'))
        size = min(PAGE, int(query.get('max-keys', '1000')))
        page = keys[start:start + size]
        more = start + size < len(keys)
        xml = ['<?xml version="1.0" encoding="UTF-8"?>',
               '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">',
               '<IsTruncated>%s</IsTruncated>' % ('true' if more else 'false')]
        if more:
            xml.append('<NextContinuationToken>%d</NextContinuationToken>' % (start + size))
        for k in page:
            xml.append('<Contents><Key>%s</Key><Size>%d</Size></Contents>' % (escape(k), len(FakeR2.objects[k])))
        xml.append('</ListBucketResult>')
        return ''.join(xml).encode()

    do_GET = do_DELETE = route


class JobTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), FakeR2)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.endpoint = 'http://127.0.0.1:%d' % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeR2.objects, FakeR2.calls, FakeR2.fail_with = {}, [], None
        self.dir = tempfile.mkdtemp(prefix='events-job.')
        self.out = os.path.join(self.dir, 'out')
        self.events = os.path.join(self.dir, 'events.toml')
        with open(self.events, 'w') as f:
            f.write(EVENTS_TOML)
        FakeR2.objects[okey(B1)] = body(
            record(0, 'app.open', first_run=True, vw=390),
            record(1, control='pencil', group='tools', layout='rail', input='touch'),
            record(2, control='pencil', group='tools', layout='rail', input='touch'))
        FakeR2.objects[okey(B2)] = body(
            record(0, session='s-2', player='p-2', control='pencil', group='tools', layout='rail', input='touch'),
            record(1, 'screen.view', session='s-2', player='p-2', at=1792022400000 - 1, **{'from': 'home'}))

    def tearDown(self):
        shutil.rmtree(self.dir)

    def env(self, **extra):
        e = {'PATH': os.environ.get('PATH', ''), 'TELEMETRY_OUT': self.out, 'TELEMETRY_JOB': 'isopix-events',
             'TELEMETRY_SINCE': '', 'R2_ACCESS_KEY_ID': KEY_ID, 'R2_SECRET_ACCESS_KEY': SECRET,
             'R2_ENDPOINT': self.endpoint}
        e.update(extra)
        return e

    def run_job(self, *args, **extra):
        argv = list(args)
        if argv[0] == 'pull':
            argv += ['--app', 'isopix']
            if '--events' not in argv:
                argv += ['--events', self.events]
        if '--bucket' not in argv:
            argv += ['--bucket', BUCKET]
        r = subprocess.run([sys.executable, JOB] + argv, env=self.env(**extra), cwd=ROOT,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        return r.returncode, r.stdout

    def query(self, sql, db='data.sqlite3', args=()):
        c = sqlite3.connect(os.path.join(self.out, db))
        try:
            return c.execute(sql, args).fetchall()
        finally:
            c.close()

    def manifest(self):
        with open(os.path.join(self.out, 'manifest.json')) as f:
            return json.load(f)

    def seed_next_run(self):
        """A second run's folder, seeded with this run's data.sqlite3 as the runner does."""
        seeded = os.path.join(self.dir, 'next')
        os.makedirs(seeded)
        shutil.copy(os.path.join(self.out, 'data.sqlite3'), os.path.join(seeded, 'data.sqlite3'))
        self.out = seeded

    def test_pull_writes_events_summaries_players_raw_and_a_manifest(self):
        code, out = self.run_job('pull')
        self.assertEqual(code, 0, out)
        self.assertIn('2 new (5 events)', out)
        rows = self.query('SELECT app, session, seq, event, details, player FROM events ORDER BY session, seq',
                          'events.sqlite3')
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0][:4], ('isopix', 's-1', 0, 'app.open'))
        self.assertEqual(json.loads(rows[0][4]), {'first_run': True, 'vw': 390})
        self.assertEqual(
            self.query('SELECT day, app, platform, form, version, event, key, n FROM daily_events ORDER BY day, event'),
            [('2026-10-14', 'isopix', 'web', 'phone', '1.2.3', 'screen.view', '{}', 1),
             ('2026-10-15', 'isopix', 'web', 'phone', '1.2.3', 'app.open', '{"first_run":true}', 1),
             ('2026-10-15', 'isopix', 'web', 'phone', '1.2.3', 'control.use', '{"control":"pencil","layout":"rail"}', 3)])
        self.assertEqual(self.query('SELECT day, player FROM day_players ORDER BY day, player'),
                         [('2026-10-14', 'p-2'), ('2026-10-15', 'p-1'), ('2026-10-15', 'p-2')])
        self.assertEqual(self.query('SELECT day, app, platform, form, players FROM daily_players ORDER BY day'),
                         [('2026-10-14', 'isopix', 'web', 'phone', 1), ('2026-10-15', 'isopix', 'web', 'phone', 2)])
        for k in (okey(B1), okey(B2)):
            with open(os.path.join(self.out, 'raw', *k.split('/')), 'rb') as f:
                self.assertEqual(f.read(), FakeR2.objects[k], 'the raw object is kept byte for byte')
        m = self.manifest()
        self.assertEqual((m['bucket'], m['app'], m['events']), (BUCKET, 'isopix', 5))
        self.assertEqual(sorted(o['key'] for o in m['delete']), [okey(B1), okey(B2)])
        self.assertEqual(m['left'], [])
        self.assertEqual(len(FakeR2.objects), 2, 'pull deletes nothing')

    def test_every_table_is_one_the_runner_takes(self):
        self.run_job('pull')
        for db in ('data.sqlite3', 'events.sqlite3'):
            c = sqlite3.connect(os.path.join(self.out, db))
            try:
                for name, sql in c.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'"):
                    self.assertRegex(name, r'^[A-Za-z_][A-Za-z0-9_]*$')
                    self.assertNotIn('WITHOUT ROWID', sql.upper())
                    info = c.execute('PRAGMA table_info("%s")' % name).fetchall()
                    self.assertTrue(any(r[5] for r in info), '%s has a primary key' % name)
                    self.assertNotIn('_changed', [r[1] for r in info])
                    self.assertLess(len(info), 99)
            finally:
                c.close()
        self.assertEqual(self.query("SELECT name FROM sqlite_master WHERE type = 'table'", 'events.sqlite3'),
                         [('events',)])

    def test_a_second_run_adds_its_counts_and_recounts_players(self):
        self.run_job('pull')
        self.run_job('ack')
        self.seed_next_run()
        FakeR2.objects[okey(B3)] = body(
            record(0, session='s-3', player='p-3', control='pencil', group='tools', layout='rail', input='touch'),
            record(1, session='s-3', player='p-1', control='pencil', group='tools', layout='rail', input='touch'))
        FakeR2.calls = []
        code, out = self.run_job('pull')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.query("SELECT n FROM daily_events WHERE event = 'control.use'"), [(5,)])
        self.assertEqual(self.query("SELECT players FROM daily_players WHERE day = '2026-10-15'"), [(3,)])
        self.assertEqual(self.query('SELECT COUNT(*) FROM events', 'events.sqlite3'), [(2,)],
                         "events.sqlite3 holds this run's events only")
        self.assertEqual([c for c in FakeR2.calls if c[0] == 'GET' and c[1] != 'list'], [('GET', okey(B3))])

    def test_repull_without_ack_downloads_and_counts_nothing_twice(self):
        self.run_job('pull')
        self.seed_next_run()
        FakeR2.calls = []
        code, out = self.run_job('pull')
        self.assertEqual(code, 0, out)
        self.assertEqual([c for c in FakeR2.calls if c[1] != 'list'], [])
        self.assertEqual(self.query("SELECT n FROM daily_events WHERE event = 'control.use'"), [(3,)])
        self.assertEqual(sorted(o['key'] for o in self.manifest()['delete']), [okey(B1), okey(B2)],
                         'listed for deletion again')
        self.assertEqual(self.query('SELECT COUNT(*) FROM events', 'events.sqlite3'), [(0,)])

    def test_a_batch_retried_across_midnight_is_counted_once_and_its_copy_deleted(self):
        FakeR2.objects[okey(B1, day='2026-10-16')] = FakeR2.objects[okey(B1)]
        code, out = self.run_job('pull')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.query("SELECT n FROM daily_events WHERE event = 'control.use'"), [(3,)])
        self.assertIn(okey(B1, day='2026-10-16'), [o['key'] for o in self.manifest()['delete']])
        self.assertEqual(self.query('SELECT status FROM event_batches WHERE key = ?', args=(okey(B1, '2026-10-16'),)),
                         [('duplicate',)])
        # and in a later run, after the original was stored and acked
        self.run_job('ack')
        self.seed_next_run()
        FakeR2.objects[okey(B2, day='2026-10-17')] = body(
            record(0, session='s-2', player='p-2', control='pencil', group='tools', layout='rail', input='touch'),
            record(1, 'screen.view', session='s-2', player='p-2', at=1792022400000 - 1, **{'from': 'home'}))
        self.run_job('pull')
        self.assertEqual(self.query("SELECT n FROM daily_events WHERE event = 'control.use'"), [(3,)])
        self.assertEqual([o['key'] for o in self.manifest()['delete']], [okey(B2, day='2026-10-17')])

    def test_a_batch_id_reused_with_other_bytes_is_left(self):
        FakeR2.objects[okey(B1, day='2026-10-16')] = body(record(9))
        self.run_job('pull')
        reasons = {o['key']: o['reason'] for o in self.manifest()['left']}
        self.assertIn('other bytes', reasons[okey(B1, day='2026-10-16')])
        self.assertEqual(self.query("SELECT n FROM daily_events WHERE event = 'control.use'"), [(3,)])

    def test_an_event_seen_twice_in_one_run_is_stored_once(self):
        FakeR2.objects[okey(B3)] = body(record(1, control='pencil', group='tools', layout='rail', input='touch'))
        self.run_job('pull')
        self.assertEqual(self.query("SELECT n FROM daily_events WHERE event = 'control.use'"), [(3,)])
        self.assertEqual(self.query('SELECT good, added FROM event_batches WHERE key = ?', args=(okey(B3),)), [(1, 0)])

    def test_bad_lines_are_not_stored_and_their_object_is_never_deleted(self):
        bad = dict(record(5), extra=1)
        lines = body(record(3, control='eraser', group='tools', layout='rail', input='pen')) + b'not json\n' + body(bad)
        lines += body(dict(record(6), app='noodlepads'), dict(record(7), seq=-1), dict(record(8), seq=True),
                      dict(record(9), tz=2000), dict(record(10), details=[]), dict(record(11), platform='android'))
        FakeR2.objects[okey(B3)] = lines
        FakeR2.objects[okey(B4)] = b'\xff\xfe not utf-8\n'
        FakeR2.objects['v1/stray.txt'] = b'not a batch'
        code, out = self.run_job('pull')
        self.assertEqual(code, 0, out)
        m = self.manifest()
        reasons = {o['key']: o['reason'] for o in m['left']}
        self.assertIn('8 bad lines', reasons[okey(B3)])
        self.assertIn('line 2: not JSON', reasons[okey(B3)])
        self.assertIn('unknown columns extra', reasons[okey(B3)])
        self.assertIn('not UTF-8', reasons[okey(B4)])
        self.assertIn('not named', reasons['v1/stray.txt'])
        self.assertNotIn(okey(B3), [o['key'] for o in m['delete']])
        self.assertTrue(os.path.exists(os.path.join(self.out, 'raw', 'rejected', *okey(B3).split('/'))))
        self.assertEqual(self.query("SELECT details FROM events WHERE seq = 3", 'events.sqlite3'),
                         [('{"control":"eraser","group":"tools","input":"pen","layout":"rail"}',)],
                         'the good line of a rejected object is stored')
        self.run_job('ack')
        self.assertIn(okey(B3), FakeR2.objects, 'ack never deletes a rejected object')
        self.seed_next_run()
        FakeR2.calls = []
        self.run_job('pull')
        self.assertNotIn(('GET', okey(B3)), FakeR2.calls, 'not read again')
        self.assertIn(okey(B3), [o['key'] for o in self.manifest()['left']], 'and still left')

    def test_a_short_download_is_left(self):
        class Short(FakeR2):
            def listing(self, query):
                return super().listing(query).replace(b'<Size>', b'<Size>9')
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Short)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            code, out = self.run_job('pull', R2_ENDPOINT='http://127.0.0.1:%d' % server.server_address[1])
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.manifest()['left']), 2)
        self.assertIn('the listing said', self.manifest()['left'][0]['reason'])

    def test_events_and_keys_the_list_lacks_are_stored_and_named(self):
        FakeR2.objects[okey(B3)] = body(record(0, 'isopix.secret_tool', session='s-9'),
                                        record(1, session='s-9', control='x', colour='red'))
        code, out = self.run_job('pull')
        self.assertEqual(code, 0, out)
        self.assertIn('not in the event list: control.use.colour (1)', out)
        self.assertIn('not in the event list: isopix.secret_tool (1)', out)
        self.assertEqual(self.manifest()['unlisted'], {'control.use.colour': 1, 'isopix.secret_tool': 1})
        self.assertEqual(self.query("SELECT key, n FROM daily_events WHERE event = 'isopix.secret_tool'"), [('{}', 1)])
        self.assertEqual(self.query("SELECT key FROM daily_events WHERE event = 'control.use' AND n = 1"),
                         [('{"control":"x","layout":null}',)], 'an absent summary key counts as null')

    def test_a_record_with_no_player_counts_no_player(self):
        FakeR2.objects = {okey(B3): body(record(0, player=None, control='x'))}
        self.run_job('pull')
        self.assertEqual(self.query('SELECT COUNT(*) FROM day_players'), [(0,)])
        self.assertEqual(self.query('SELECT COUNT(*) FROM daily_events'), [(1,)])

    def test_ack_deletes_only_what_the_manifest_lists(self):
        self.run_job('pull')
        FakeR2.objects[okey(B3)] = body(record(0, session='late'))   # arrived after the pull
        FakeR2.objects['v1/stray.txt'] = b'x'
        del FakeR2.objects[okey(B2)]   # already gone: a 404 is fine
        code, out = self.run_job('ack')
        self.assertEqual(code, 0, out)
        self.assertIn('1 of 2 objects deleted, 1 already gone', out)
        self.assertEqual(sorted(FakeR2.objects), [okey(B3), 'v1/stray.txt'])

    def test_ack_refuses_a_manifest_naming_other_keys_or_another_bucket(self):
        os.makedirs(self.out)
        for manifest in ({'bucket': BUCKET, 'delete': [{'key': 'v1/anything'}]},
                         {'bucket': 'other-events', 'delete': []},
                         {'bucket': BUCKET, 'delete': ['v1/2026-10-15/%s.jsonl' % B1]}):
            with open(os.path.join(self.out, 'manifest.json'), 'w') as f:
                json.dump(manifest, f)
            code, out = self.run_job('ack')
            self.assertEqual(code, 1, out)
        self.assertEqual(len(FakeR2.objects), 2)
        self.assertEqual(FakeR2.calls, [])

    def test_ack_with_no_manifest_fails(self):
        code, out = self.run_job('ack')
        self.assertEqual(code, 1, out)
        self.assertIn('manifest.json', out)

    def test_empty_bucket_is_a_pull(self):
        FakeR2.objects = {}
        code, out = self.run_job('pull')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.query('SELECT COUNT(*) FROM daily_events'), [(0,)])
        self.assertTrue(os.path.exists(os.path.join(self.out, 'events.sqlite3')))
        self.assertEqual(self.manifest()['delete'], [])

    def test_the_cap_stops_a_run(self):
        old = job.MAX_OBJECTS
        job.MAX_OBJECTS = 1
        try:
            env = self.env()
            code = job.main(['events_job.py', 'pull', '--app', 'isopix', '--bucket', BUCKET, '--events', self.events],
                            env)
        finally:
            job.MAX_OBJECTS = old
        self.assertEqual(code, 0)
        self.assertEqual(len(self.manifest()['delete']), 1)

    def test_unreachable_exits_75(self):
        s = socket.socket()
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
        s.close()
        code, out = self.run_job('pull', R2_ENDPOINT='http://127.0.0.1:%d' % port)
        self.assertEqual(code, 75, out)
        FakeR2.fail_with = 503
        self.assertEqual(self.run_job('pull')[0], 75)
        FakeR2.fail_with = 429
        self.assertEqual(self.run_job('pull')[0], 75)

    def test_a_refused_token_fails(self):
        code, out = self.run_job('pull', R2_SECRET_ACCESS_KEY='wrong')
        self.assertEqual(code, 1, out)
        self.assertIn('403', out)

    def test_missing_settings_fail(self):
        code, out = self.run_job('pull', R2_ACCESS_KEY_ID='')
        self.assertEqual((code, 'R2_ACCESS_KEY_ID' in out), (1, True), out)
        code, out = self.run_job('pull', R2_ENDPOINT='', R2_ACCOUNT_ID='')
        self.assertEqual((code, 'R2_ACCOUNT_ID' in out), (1, True), out)
        code, out = self.run_job('pull', TELEMETRY_OUT='')
        self.assertEqual((code, 'TELEMETRY_OUT' in out), (1, True), out)
        code, out = self.run_job('pull', '--events', os.path.join(self.dir, 'none.toml'))
        self.assertEqual((code, 'event list' in out), (1, True), out)

    def test_the_account_makes_the_endpoint(self):
        r2 = job.R2({'R2_ACCESS_KEY_ID': 'a', 'R2_SECRET_ACCESS_KEY': 'b', 'R2_ACCOUNT_ID': 'f' * 32}, BUCKET)
        self.assertEqual((r2.scheme, r2.host), ('https', 'f' * 32 + '.' + 'r2.cloudflarestorage.com'))

    def test_usage(self):
        for argv in ([], ['pull'], ['pull', '--bucket', BUCKET], ['ack'], ['ack', '--bucket', 'Bad_Name'], ['nope']):
            r = subprocess.run([sys.executable, JOB] + argv, env=self.env(), stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, universal_newlines=True)
            self.assertEqual(r.returncode, 2, (argv, r.stdout))


class FormatTest(unittest.TestCase):
    def test_aws_published_get_object_example(self):
        # "Example: GET Object" from AWS's Signature Version 4 header-auth docs.
        when = datetime.datetime(2013, 5, 24, tzinfo=datetime.timezone.utc)
        headers = job.sigv4('GET', 'examplebucket.s3.amazonaws.com', '/test.txt', {'Range': 'bytes=0-9'}, b'',
                            'AKIAIOSFODNN7EXAMPLE', 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY', 'us-east-1', when)
        self.assertTrue(headers['authorization'].endswith(
            'Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41'))

    def test_aws_published_list_objects_example(self):
        when = datetime.datetime(2013, 5, 24, tzinfo=datetime.timezone.utc)
        headers = job.sigv4('GET', 'examplebucket.s3.amazonaws.com', '/', {}, b'',
                            'AKIAIOSFODNN7EXAMPLE', 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY', 'us-east-1', when,
                            {'max-keys': '2', 'prefix': 'J'})
        self.assertTrue(headers['authorization'].endswith(
            'Signature=34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7'))

    def test_the_check(self):
        self.assertIsNone(job.check(record(0), 'isopix', 'web'))
        self.assertIsNone(job.check(dict(record(0), player=None, screen=None), 'isopix', None))
        self.assertEqual(job.check([], 'isopix'), 'not a JSON object')
        r = record(0)
        del r['tz']
        self.assertEqual(job.check(r, 'isopix'), 'no tz')
        self.assertEqual(job.check(dict(record(0), at=0, form='car', session=''), 'isopix'), 'bad form, session, at')
        self.assertEqual(job.check(dict(record(0), version='v' * 201), 'isopix'), 'bad version')

    def test_the_check_takes_a_ci_report(self):
        sys.path.insert(0, os.path.join(ROOT, 'tools'))
        import ci_report
        env = {'GITHUB_REPOSITORY': 'o/app', 'GITHUB_RUN_ID': '7', 'GITHUB_WORKFLOW': 'test', 'JOB_STATUS': 'success'}
        rec = json.loads(json.dumps(ci_report.record(env, None, 1792022400000)))
        self.assertIsNone(job.check(rec, 'ci', 'ci'))
        self.assertEqual(job.check(dict(rec, form='phone'), 'ci', 'ci'), None)   # forms are not tied to a platform
        self.assertEqual(job.check(rec, 'ci', 'web'), 'bad platform')

    def test_the_web_worker_and_the_job_agree_on_object_names(self):
        with open(os.path.join(ROOT, 'events', 'events-worker.js')) as f:
            src = f.read()
        self.assertIn('return `v1/${now.toISOString().slice(0, 10)}/${batch}.jsonl`;', src)
        self.assertTrue(job.KEY.match('v1/2026-10-15/%s.jsonl' % B1))


class TomlTest(unittest.TestCase):
    def test_reads_the_event_lists_shapes(self):
        doc = job.read_toml(EVENTS_TOML)
        self.assertEqual(doc['apps']['isopix']['forms'], ['phone', 'tablet', 'desktop'])
        self.assertEqual(doc['events']['app.open']['why'], 'How often each app "starts"')
        self.assertEqual(doc['events']['control.use']['details'], ['control', 'group', 'layout', 'input'])
        self.assertEqual(doc['events']['control.use']['skip'], {'orbit': 'no toolbar', 'other': 'x'})
        self.assertEqual(doc['events']['screen.view']['summary'], [])
        self.assertEqual(job.read_toml('a = 3\nb = true\nc = false\nd = -2\ne = {}\nf = "\\u00e9\\t"\n'),
                         {'a': 3, 'b': True, 'c': False, 'd': -2, 'e': {}, 'f': '\u00e9\t'})

    def test_agrees_with_tomllib(self):
        try:
            import tomllib
        except ImportError:
            self.skipTest('tomllib is Python 3.11+')
        self.assertEqual(job.read_toml(EVENTS_TOML), tomllib.loads(EVENTS_TOML))
        real = os.path.join(os.path.dirname(ROOT), 'telemetry', 'events.toml')
        if os.path.exists(real):   # the telemetry repo checked out beside this one
            with open(real, encoding='utf-8') as f:
                text = f.read()
            self.assertEqual(job.read_toml(text), tomllib.loads(text))

    def test_refuses_what_it_does_not_read(self):
        for text in ('[[a]]\n', 'a = 1.5\n', 'a = "x\n', 'a = 1 b = 2\n', 'a = 1\na = 2\n', 'a = [1 2]\n',
                     'a = { b = 1 c = 2 }\n', 'a = "\\q"\n', 'a 1\n', '[a\n', '= 1\n', 'a = 1\n[a]\n',
                     'a = 1\n[a.b]\n', 'a = { b = 1 }\nb = 2\n[a.b.c]\n', 'a = { b }\n', 'a = { a.b = 1, a = 2 }\n'):
            with self.assertRaises(ValueError, msg=text):
                job.read_toml(text)

    def test_load_events_falls_back_without_tomllib(self):
        d = tempfile.mkdtemp(prefix='events-toml.')
        try:
            path = os.path.join(d, 'events.toml')
            with open(path, 'w') as f:
                f.write(EVENTS_TOML)
            real_import = __builtins__['__import__'] if isinstance(__builtins__, dict) else __builtins__.__import__

            def no_tomllib(name, *args, **kwargs):
                if name == 'tomllib':
                    raise ImportError(name)
                return real_import(name, *args, **kwargs)
            import builtins
            builtins.__import__ = no_tomllib
            try:
                events, apps = job.load_events(path)
            finally:
                builtins.__import__ = real_import
            self.assertEqual(events['control.use'], ({'control', 'group', 'layout', 'input'}, ['control', 'layout']))
            self.assertEqual(apps['orbit']['platform'], 'android')
            with open(path, 'w') as f:
                f.write('[events."x"]\nsummary = 1.5\n')
            with self.assertRaises(job.Fail):
                job.load_events(path)
            with open(path, 'w') as f:
                f.write('[events]\nx = 1\n')
            with self.assertRaises(job.Fail):
                job.load_events(path)
        finally:
            shutil.rmtree(d)


if __name__ == '__main__':
    unittest.main()
