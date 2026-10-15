"""Tests for tools/harvest.py against a fake Cloudflare API on localhost.

An in-memory SQLite database stands in for D1, and canned GraphQL answers for
the analytics. CF_API points harvest at the fake; nothing reaches Cloudflare.
"""
import contextlib
import datetime
import http.server
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.parse
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools'))
import harvest as H  # noqa: E402

TOKEN = 'test-token'
ACCOUNT, ZONE, DBID = 'acct1', 'zone1', 'db-1234'
TODAY = datetime.date(2026, 10, 15)


class Fake(http.server.BaseHTTPRequestHandler):
    """GET /zones, POST /accounts/../d1/database/../query and POST /graphql."""

    def log_message(self, *a):
        pass

    def answer(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def forced(self):
        if self.server.force:
            self.answer(self.server.force, {'success': False, 'errors': [{'message': 'forced %d' % self.server.force}]})
            return True
        return False

    def do_GET(self):
        s = self.server
        if self.forced():
            return
        if self.headers.get('Authorization') != 'Bearer ' + TOKEN:
            return self.answer(403, {'success': False, 'errors': [{'message': 'Authentication error'}]})
        url = urllib.parse.urlparse(self.path)
        if url.path != '/zones':
            return self.answer(404, {'success': False, 'errors': [{'message': 'no route'}]})
        name = urllib.parse.parse_qs(url.query).get('name', [''])[0]
        s.log.append(('zones', name))
        found = [{'id': ZONE, 'name': name, 'account': {'id': ACCOUNT}}] if name in s.zones else []
        self.answer(200, {'success': True, 'result': found})

    def do_POST(self):
        s = self.server
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])).decode())
        if self.forced():
            return
        if self.headers.get('Authorization') != 'Bearer ' + TOKEN:
            return self.answer(403, {'success': False, 'errors': [{'message': 'Authentication error'}]})
        if self.path == '/accounts/%s/d1/database/%s/query' % (ACCOUNT, DBID):
            s.log.append(('d1', body['sql'], body.get('params')))
            try:
                cur = s.d1.execute(body['sql'], body.get('params') or [])
                cols = [d[0] for d in cur.description or []]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            except sqlite3.Error as e:
                return self.answer(400, {'result': [], 'success': False, 'messages': [],
                                         'errors': [{'code': 7500, 'message': '%s: SQLITE_ERROR' % e}]})
            return self.answer(200, {'success': True, 'errors': [], 'result': [{'results': rows, 'success': True}]})
        if self.path == '/graphql':
            q = body['query']
            op = q.split('query ', 1)[1].split('(', 1)[0]
            s.log.append(('graphql', op, body['variables']))
            if op in s.gql_errors:
                return self.answer(200, {'data': None, 'errors': [{'message': s.gql_errors[op]}]})
            if 'workersInvocationsAdaptive' in q:
                return self.answer(200, {'data': {'viewer': {'accounts': [{'workersInvocationsAdaptive': s.workers}]}}})
            return self.answer(200, {'data': {'viewer': {'zones': [{'httpRequests1dGroups': s.http}]}}})
        self.answer(404, {'success': False, 'errors': [{'message': 'no route'}]})


def http_group(day, requests, country_requests):
    return {'dimensions': {'date': day},
            'sum': {'requests': requests, 'cachedRequests': 1, 'bytes': 1000, 'cachedBytes': 100, 'pageViews': 5,
                    'threats': 0,
                    'countryMap': [{'clientCountryName': 'US', 'requests': country_requests, 'bytes': 900, 'threats': 0}],
                    'responseStatusMap': [{'edgeResponseStatus': 200, 'requests': requests}],
                    'browserMap': [{'uaBrowserFamily': 'Safari', 'pageViews': 5}]},
            'uniq': {'uniques': 3}}


class Base(unittest.TestCase):
    def setUp(self):
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fake)
        self.server.d1 = sqlite3.connect(':memory:', check_same_thread=False)
        self.server.zones = {'example.com'}
        self.server.log, self.server.gql_errors, self.server.force = [], {}, 0
        self.server.http, self.server.workers = [], []
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.db = os.path.join(self.root, 'archive', 'telemetry.sqlite3')
        env = {'CF_API': 'http://127.0.0.1:%d' % self.server.server_address[1], 'HARVEST_TOKEN': TOKEN}
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.server.shutdown()
        self.server.server_close()
        self.server.d1.close()
        self.tmp.cleanup()

    def site(self, conf, toml=None, domain='example.com'):
        folder = os.path.join(self.root, domain)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, 'wrangler.toml'), 'w') as f:
            f.write(toml if toml is not None else (
                'name = "example-com"\nmain = "worker/index.js"\n\nroutes = [\n'
                '  { pattern = "example.com", custom_domain = true }\n]\n\n[assets]\ndirectory = "./public"\n\n'
                '[[d1_databases]]\nbinding = "DB"\ndatabase_name = "ex"\ndatabase_id = "%s"\n' % DBID))
        with open(os.path.join(folder, 'harvest.conf'), 'w') as f:
            f.write('db=%s\n%s' % (self.db, conf))

    def run_harvest(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = H.main(list(args), root=self.root, today=TODAY)
        return code, out.getvalue()

    def local(self, sql):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def d1(self, *sql):
        for s in sql:
            self.server.d1.execute(s)
        self.server.d1.commit()


class Tables(Base):
    def test_append_pages_and_is_idempotent(self):
        self.d1('CREATE TABLE activity (id INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER NOT NULL, kind TEXT)')
        for i in range(7):
            self.d1("INSERT INTO activity (at, kind) VALUES (%d, 'open')" % (100 + i))
        self.site('analytics=0\naccount=%s\ntable=activity append id\n' % ACCOUNT)
        with mock.patch.object(H, 'PAGE', 3):
            code, out = self.run_harvest()
            self.assertEqual(code, 0, out)
            self.assertIn('📦 activity +7 rows', out)
            pages = [e for e in self.server.log if e[0] == 'd1' and e[1].startswith('SELECT')]
            self.assertEqual(len(pages), 3)
            self.assertEqual(pages[1][2], [3])
            self.assertEqual(self.local('SELECT COUNT(*), MAX(id) FROM activity'), [(7, 7)])
            self.server.log.clear()
            code, out = self.run_harvest()
            self.assertEqual(code, 0, out)
            self.assertIn('📦 activity +0 rows', out)
            self.assertEqual(self.local('SELECT COUNT(*) FROM activity'), [(7,)])
            self.d1("INSERT INTO activity (at, kind) VALUES (200, 'push')")
            code, out = self.run_harvest()
            self.assertIn('📦 activity +1 rows', out)
        self.assertEqual(self.local('SELECT kind FROM activity WHERE id = 8'), [('push',)])
        # No zone lookup: account= was given and analytics are off.
        self.assertFalse([e for e in self.server.log if e[0] == 'zones'])

    def test_upsert_min_max_cols_and_as(self):
        self.d1('CREATE TABLE devices (hash TEXT PRIMARY KEY, first_seen INTEGER, last_seen INTEGER, app TEXT)',
                "INSERT INTO devices VALUES ('a', 50, 60, 'v1')",
                'CREATE TABLE uses (profile_id TEXT, feature TEXT, day TEXT, n INTEGER, PRIMARY KEY (profile_id, feature, day))',
                "INSERT INTO uses VALUES ('p1', 'pads', '2026-10-14', 4)",
                'CREATE TABLE profiles (id TEXT PRIMARY KEY, name TEXT, avatar TEXT, pin_hash TEXT, created INTEGER)',
                "INSERT INTO profiles VALUES ('p1', 'Pip', 'x', 'SECRET', 1)")
        # The archive already holds older copies, as Noodle Pads' does.
        os.makedirs(os.path.dirname(self.db))
        conn = sqlite3.connect(self.db)
        conn.executescript("""
            CREATE TABLE devices (hash TEXT PRIMARY KEY, first_seen INTEGER, last_seen INTEGER, app TEXT);
            INSERT INTO devices VALUES ('a', 10, 20, 'v0');
            CREATE TABLE uses (profile_id TEXT, feature TEXT, day TEXT, n INTEGER, PRIMARY KEY (profile_id, feature, day));
            INSERT INTO uses VALUES ('p1', 'pads', '2026-10-14', 9);
            CREATE TABLE players (id TEXT PRIMARY KEY, name TEXT, avatar TEXT, created INTEGER);
            INSERT INTO players VALUES ('gone', 'Juno', 'y', 0);
        """)
        conn.close()
        self.site('analytics=0\naccount=%s\n'
                  'table=devices upsert hash min=first_seen\n'
                  'table=uses upsert profile_id,feature,day max=n\n'
                  'table=profiles upsert id cols=id,name,avatar,created as=players\n' % ACCOUNT)
        code, out = self.run_harvest()
        self.assertEqual(code, 0, out)
        self.assertIn('📦 players ← profiles 1 rows', out)
        # first_seen keeps the older 10; last_seen and app take D1's.
        self.assertEqual(self.local('SELECT * FROM devices'), [('a', 10, 60, 'v1')])
        # n keeps the larger count.
        self.assertEqual(self.local('SELECT n FROM uses'), [(9,)])
        # Only cols= were read: no pin_hash locally, nor in any query sent.
        self.assertNotIn('pin_hash', [c[1] for c in self.local('PRAGMA table_info(players)')])
        self.assertFalse([e for e in self.server.log if e[0] == 'd1' and 'pin_hash' in e[1]])
        self.assertEqual(self.local('SELECT id, name FROM players ORDER BY id'), [('gone', 'Juno'), ('p1', 'Pip')])
        self.assertEqual(self.local("SELECT name FROM sqlite_master WHERE name = 'profiles'"), [])
        # A smaller first_seen in D1 wins; a later run with fewer uses keeps the max.
        self.d1("UPDATE devices SET first_seen = 5", "UPDATE uses SET n = 12")
        self.run_harvest()
        self.assertEqual(self.local('SELECT first_seen FROM devices'), [(5,)])
        self.assertEqual(self.local('SELECT n FROM uses'), [(12,)])

    def test_creates_table_from_pragma_then_adds_new_columns(self):
        self.d1('CREATE TABLE visits (id INTEGER PRIMARY KEY, at INTEGER NOT NULL, path TEXT)',
                'INSERT INTO visits VALUES (1, 10, "/")')
        self.site('analytics=0\naccount=%s\ntable=visits append id\n' % ACCOUNT)
        code, out = self.run_harvest()
        self.assertEqual(code, 0, out)
        self.assertIn('🆕 visits: new local table', out)
        info = self.local('PRAGMA table_info(visits)')
        self.assertEqual([(c[1], c[2], c[5]) for c in info], [('id', 'INTEGER', 1), ('at', 'INTEGER', 0), ('path', 'TEXT', 0)])
        self.d1('ALTER TABLE visits ADD COLUMN country TEXT', "INSERT INTO visits VALUES (2, 20, '/b', 'NZ')")
        code, out = self.run_harvest()
        self.assertEqual(code, 0, out)
        self.assertIn('➕ visits: new column country', out)
        self.assertEqual(self.local('SELECT id, country FROM visits ORDER BY id'), [(1, None), (2, 'NZ')])

    def test_local_columns_are_never_dropped(self):
        self.d1('CREATE TABLE devices (hash TEXT PRIMARY KEY, app TEXT)', "INSERT INTO devices VALUES ('a', 'v2')")
        os.makedirs(os.path.dirname(self.db))
        conn = sqlite3.connect(self.db)
        conn.executescript("CREATE TABLE devices (hash TEXT PRIMARY KEY, app TEXT, width INTEGER);"
                           "INSERT INTO devices VALUES ('a', 'v1', 400);")
        conn.close()
        self.site('analytics=0\naccount=%s\ntable=devices upsert hash\n' % ACCOUNT)
        code, out = self.run_harvest()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.local('SELECT * FROM devices'), [('a', 'v2', 400)])

    def test_table_missing_on_d1_is_skipped(self):
        self.d1('CREATE TABLE activity (id INTEGER PRIMARY KEY, at INTEGER)', 'INSERT INTO activity VALUES (1, 1)')
        self.site('analytics=0\naccount=%s\ntable=visits append id\ntable=activity append id\n' % ACCOUNT)
        code, out = self.run_harvest()
        self.assertEqual(code, 0, out)
        self.assertIn('⏭️ visits: not in D1 yet, skipped', out)
        self.assertIn('📦 activity +1 rows', out)
        self.assertEqual(self.local("SELECT name FROM sqlite_master WHERE name = 'visits'"), [])
        self.assertEqual(self.local("SELECT value FROM harvest_meta WHERE key = 'step:table:visits'"), [('skipped: not in D1',)])

    def test_only_reads_d1(self):
        cf = H.Cloudflare(TOKEN)
        with self.assertRaises(ValueError):
            cf.d1(ACCOUNT, DBID, 'DELETE FROM activity')
        self.assertFalse([e for e in self.server.log if e[0] == 'd1'])


class Analytics(Base):
    def test_graphql_error_exits_1_and_d1_is_still_committed(self):
        self.d1('CREATE TABLE activity (id INTEGER PRIMARY KEY, at INTEGER)', 'INSERT INTO activity VALUES (1, 1)')
        self.server.http = [http_group('2026-10-14', 10, 8)]
        self.server.gql_errors = {'HarvestWorkersDaily': 'not authorized for that account'}
        self.site('table=activity append id\n')
        code, out = self.run_harvest()
        self.assertEqual(code, 1, out)
        self.assertIn('⚠️ cf_workers_daily: token lacks Account → Account Analytics: Read (not authorized for that account)', out)
        self.assertIn('🌐 cf_http_daily 1 days', out)
        self.assertIn('❌ harvest: 1 site, 5 steps worked, 1 failed', out)
        self.assertEqual(self.local('SELECT COUNT(*) FROM activity'), [(1,)])
        self.assertEqual(self.local('SELECT requests FROM cf_http_daily'), [(10,)])
        self.assertEqual(self.local("SELECT value FROM harvest_meta WHERE key = 'last_result'"), [('1 failed',)])
        # Quiet: only the problem, named with its site, and the summary.
        code, out = self.run_harvest('--quiet')
        self.assertEqual(out.splitlines(), [
            'example.com: ⚠️ cf_workers_daily: token lacks Account → Account Analytics: Read (not authorized for that account)',
            '❌ harvest: 1 site, 5 steps worked, 1 failed'])

    def test_rows_are_upserted_on_a_rerun(self):
        self.server.http = [http_group('2026-10-14', 10, 8), http_group('2026-10-15', 2, 2)]
        self.server.workers = [{'dimensions': {'date': '2026-10-15'}, 'sum': {'requests': 4, 'errors': 1, 'subrequests': 0},
                                'quantiles': {'cpuTimeP50': 1.5, 'cpuTimeP99': 7.25}}]
        self.site('analytics=1\n')
        code, out = self.run_harvest('--days', '2')
        self.assertEqual(code, 0, out)
        for line in ('🌐 cf_http_daily 2 days', '🌐 cf_http_country 2 rows', '🌐 cf_http_status 2 rows',
                     '🌐 cf_http_browser 2 rows', '⚙️ cf_workers_daily 1 days'):
            self.assertIn(line, out)
        # Today's partial day grows.
        self.server.http = [http_group('2026-10-14', 10, 8), http_group('2026-10-15', 9, 7)]
        self.server.workers[0]['sum']['requests'] = 11
        code, out = self.run_harvest('--days', '2')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.local('SELECT day, requests, uniques FROM cf_http_daily ORDER BY day'),
                         [('2026-10-14', 10, 3), ('2026-10-15', 9, 3)])
        self.assertEqual(self.local("SELECT requests FROM cf_http_country WHERE day = '2026-10-15'"), [(7,)])
        self.assertEqual(self.local('SELECT day, script, requests, errors, cpu_p50, cpu_p99 FROM cf_workers_daily'),
                         [('2026-10-15', 'example-com', 11, 1, 1.5, 7.25)])
        gql = [e for e in self.server.log if e[0] == 'graphql']
        daily = [e for e in gql if e[1] == 'HarvestHttpDaily'][0][2]
        self.assertEqual(daily, {'zone': ZONE, 'start': '2026-10-14', 'end': '2026-10-15'})
        workers = [e for e in gql if e[1] == 'HarvestWorkersDaily'][0][2]
        self.assertEqual(workers, {'account': ACCOUNT, 'script': 'example-com',
                                   'start': '2026-10-14T00:00:00Z', 'end': '2026-10-15T23:59:59Z'})

    def test_static_site_skips_workers(self):
        self.site('', toml='name = "ex"\nroutes = [ { pattern = "example.com", custom_domain = true } ]\n'
                           '[assets]\ndirectory = "./public"\n')
        code, out = self.run_harvest()
        self.assertEqual(code, 0, out)
        self.assertNotIn('cf_workers_daily', out)
        self.assertFalse([e for e in self.server.log if e[0] == 'graphql' and e[1] == 'HarvestWorkersDaily'])


class ExitCodes(Base):
    def test_cloudflare_down_exits_75(self):
        for code in (503, 429):
            with self.subTest(code=code):
                self.server.force = code
                self.site('analytics=1\n')
                status, out = self.run_harvest()
                self.assertEqual(status, H.UNREACHABLE, out)
                self.assertIn('(Cloudflare unreachable)', out)

    def test_no_connection_exits_75(self):
        self.site('analytics=1\n')
        with mock.patch.dict(os.environ, {'CF_API': 'http://127.0.0.1:9'}):
            status, out = self.run_harvest()
        self.assertEqual(status, H.UNREACHABLE, out)
        self.assertIn('cannot reach', out)

    def test_bad_token_exits_1(self):
        self.site('analytics=1\n')
        with mock.patch.dict(os.environ, {'HARVEST_TOKEN': 'wrong'}):
            status, out = self.run_harvest()
        self.assertEqual(status, 1, out)

    def test_down_plus_a_real_failure_exits_1(self):
        self.d1('CREATE TABLE activity (id INTEGER PRIMARY KEY, at INTEGER)')
        self.site('table=activity append id\n')
        self.server.gql_errors = {'HarvestWorkersDaily': 'not authorized for that account'}
        real = H.Cloudflare.graphql

        def flaky(cf, query, variables):
            if 'HarvestHttpDaily' in query:
                raise H.ApiError('cannot reach api: timed out', unreachable=True)
            return real(cf, query, variables)

        with mock.patch.object(H.Cloudflare, 'graphql', flaky):
            status, out = self.run_harvest()
        self.assertEqual(status, 1, out)


class Seeded(Base):
    """The telemetry runner hands harvest the archive from its last stored run via --db."""

    def setUp(self):
        super().setUp()
        self.d1('CREATE TABLE activity (id INTEGER PRIMARY KEY, at INTEGER)',
                'INSERT INTO activity VALUES (1, 1), (2, 2)',
                'CREATE TABLE devices (hash TEXT PRIMARY KEY, first_seen INTEGER, last_seen INTEGER)',
                "INSERT INTO devices VALUES ('a', 50, 60)",
                'CREATE TABLE uses (profile_id TEXT, feature TEXT, day TEXT, n INTEGER, PRIMARY KEY (profile_id, feature, day))',
                "INSERT INTO uses VALUES ('p1', 'pads', '2026-10-14', 4)")
        self.server.http = [http_group('2026-10-14', 10, 8), http_group('2026-10-15', 2, 2)]
        self.site('table=activity append id\n'
                  'table=devices upsert hash min=first_seen\n'
                  'table=uses upsert profile_id,feature,day max=n\n')

    def dump(self, path):
        conn = sqlite3.connect(path)
        try:
            return [line for line in conn.iterdump() if 'harvest_meta' not in line]
        finally:
            conn.close()

    def test_seeded_file_ends_as_the_mac_archive_does(self):
        self.run_harvest('--days', '2')
        seeded = os.path.join(self.root, 'runner', 'data.sqlite3')
        os.makedirs(os.path.dirname(seeded))
        with open(self.db, 'rb') as src, open(seeded, 'wb') as dst:
            dst.write(src.read())
        # D1 moves on: a new activity row, an older first_seen, a smaller count, a fuller day.
        self.d1('INSERT INTO activity VALUES (3, 3)', 'UPDATE devices SET first_seen = 70',
                'UPDATE uses SET n = 2')
        self.server.http = [http_group('2026-10-14', 10, 8), http_group('2026-10-15', 9, 7)]
        self.server.log.clear()
        code, out = self.run_harvest('--days', '2')
        self.assertEqual(code, 0, out)
        code, out = self.run_harvest('--days', '2', '--db', seeded)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.dump(seeded), self.dump(self.db))
        # append asked D1 only for ids above the archive's max, both times.
        appends = [e for e in self.server.log if e[0] == 'd1' and 'FROM "activity"' in e[1] and e[2]]
        self.assertTrue(appends and all(e[2][0] == 2 for e in appends), appends)
        conn = sqlite3.connect(seeded)
        self.assertEqual(conn.execute('SELECT first_seen FROM devices').fetchall(), [(50,)])
        self.assertEqual(conn.execute('SELECT n FROM uses').fetchall(), [(4,)])
        self.assertEqual(conn.execute("SELECT requests FROM cf_http_daily WHERE day = '2026-10-15'").fetchall(), [(9,)])
        conn.close()

    def test_a_second_run_changes_nothing(self):
        seeded = os.path.join(self.root, 'runner', 'data.sqlite3')
        self.run_harvest('--days', '2', '--db', seeded)
        before = self.dump(seeded)
        code, out = self.run_harvest('--days', '2', '--db', seeded)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.dump(seeded), before)

    def test_every_table_meets_the_runners_rules(self):
        """Primary key, rowid table, plain identifiers, no _changed column, under 99 columns."""
        self.site('analytics=1\ntable=activity append id\n'
                  'table=devices upsert hash min=first_seen\n'
                  'table=uses upsert profile_id,feature,day max=n\n')
        self.run_harvest('--days', '2')
        conn = sqlite3.connect(self.db)
        plain = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
        tables = conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'").fetchall()
        self.assertGreaterEqual(len(tables), 8)
        for name, sql in tables:
            with self.subTest(table=name):
                info = conn.execute('PRAGMA table_info("%s")' % name).fetchall()
                self.assertRegex(name, plain)
                self.assertNotIn('WITHOUT ROWID', sql.upper())
                self.assertTrue([r for r in info if r[5]], 'no primary key')
                self.assertLess(len(info), 99)
                for r in info:
                    self.assertRegex(r[1], plain)
                    self.assertNotEqual(r[1], '_changed')
        conn.close()


class Config(Base):
    def test_unknown_key_fails_the_site(self):
        self.site('dbx=/tmp/x\n')
        code, out = self.run_harvest()
        self.assertEqual(code, 1)
        self.assertIn("unknown key 'dbx'", out)
        self.assertFalse(self.server.log)

    def test_bad_table_lines(self):
        for line, want in (('table=activity copy id', 'want'), ('table=activity append', 'want'),
                           ('table=activity append id extra', 'one id column'),
                           ('table=devices upsert hash min=', 'unknown option'),
                           ('table=devices upsert hash newest=last_seen', 'unknown option'),
                           ('table=profiles upsert id cols=name', 'cols= leaves out id'),
                           ('table=act;drop append id', 'not a plain name')):
            with self.subTest(line=line):
                path = os.path.join(self.root, 'h.conf')
                with open(path, 'w') as f:
                    f.write('# a comment\n\n%s\n' % line)
                with self.assertRaises(H.ConfError) as e:
                    H.read_conf(path, 'example.com')
                self.assertIn(want, str(e.exception))

    def test_defaults_and_comments(self):
        path = os.path.join(self.root, 'h.conf')
        with open(path, 'w') as f:
            f.write('# notes\n\nanalytics=1\ntable=profiles upsert id cols=id,name as=players\n')
        conf = H.read_conf(path, 'app.example.com')
        self.assertEqual(conf['db'], '~/.local/state/app.example.com/telemetry.sqlite3')
        self.assertEqual(conf['keychain'], 'cloudflare-analytics-read')
        self.assertEqual(conf['tables'][0]['local'], 'players')
        self.assertEqual(conf['tables'][0]['cols'], ['id', 'name'])

    def test_zone_defaults_from_wrangler_route(self):
        self.assertEqual(H.zone_of('example.com'), 'example.com')
        self.assertEqual(H.zone_of('app.shop.example.com/*'), 'example.com')
        self.assertEqual(H.zone_of('*.example.com/api/*'), 'example.com')
        self.server.zones = {'example.com'}
        self.site('analytics=1\n', toml='name = "x"\nroutes = [ { pattern = "app.example.com/*", zone_name = "example.com" } ]\n')
        code, out = self.run_harvest()
        self.assertEqual(code, 0, out)
        self.assertIn(('zones', 'example.com'), self.server.log)

    def test_wrangler_parsing(self):
        folder = os.path.join(self.root, 'w')
        os.makedirs(folder)
        with open(os.path.join(folder, 'wrangler.toml'), 'w') as f:
            f.write('name = "app-example-com"\nmain = "worker/index.js"\nroutes = [ { pattern = "app.example.com" } ]\n'
                    '[assets]\ndirectory = "./public"\n[[d1_databases]]\nbinding = "DB"\n'
                    'database_name = "app-db"\ndatabase_id = "abc-123"\n')
        self.assertEqual(H.wrangler(folder), {'name': 'app-example-com', 'main': 'worker/index.js',
                                              'database_id': 'abc-123', 'route': 'app.example.com'})

    def test_wrangler_fills_a_named_database_id(self):
        # A public app repo writes ${NAME}; the Mac's environment holds the id.
        folder = os.path.join(self.root, 'w')
        os.makedirs(folder)
        with open(os.path.join(folder, 'wrangler.toml'), 'w') as f:
            f.write('name = "w"\n[[d1_databases]]\nbinding = "DB"\ndatabase_id = "${APP_DB_ID}"\n')
        with mock.patch.dict(os.environ, {'APP_DB_ID': 'abc-123'}):
            self.assertEqual(H.wrangler(folder)['database_id'], 'abc-123')
        with mock.patch.dict(os.environ, {'APP_DB_ID': ''}):
            self.assertEqual(H.wrangler(folder)['database_id'], '')

    def test_no_token_fails_cleanly(self):
        self.site('analytics=1\n')
        with mock.patch.dict(os.environ, {'HARVEST_TOKEN': '', 'PATH': ''}):
            code, out = self.run_harvest()
        self.assertEqual(code, 1)
        self.assertIn('no token in Keychain service cloudflare-analytics-read', out)


if __name__ == '__main__':
    unittest.main()
