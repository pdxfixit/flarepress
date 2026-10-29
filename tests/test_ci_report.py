"""tools/ci_report.py against a local stand-in for the GitHub API, the events Worker and ntfy."""
import http.server
import json
import os
import re
import sys
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import ci_report  # noqa: E402

NOW = 1792022400000            # 2026-10-15T00:00:00Z
STARTED = '2026-10-14T23:58:20Z'  # 100 s before


class Fake(http.server.BaseHTTPRequestHandler):
    seen = []
    answers = {}      # path prefix -> list of statuses, popped in order (the last one stays)
    jobs = []

    def log_message(self, *args):
        pass

    def answer(self, default=204):
        for prefix, codes in self.answers.items():
            if self.path.startswith(prefix):
                return codes.pop(0) if len(codes) > 1 else codes[0]
        return default

    def do_GET(self):
        Fake.seen.append(('GET', self.path, dict(self.headers), b''))
        code = self.answer(200)
        body = json.dumps({'jobs': Fake.jobs}).encode() if code == 200 else b''
        self.send_response(code)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        Fake.seen.append(('POST', self.path, dict(self.headers), self.rfile.read(n)))
        self.send_response(self.answer())
        self.send_header('Content-Length', '0')
        self.end_headers()


class CiReportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fake)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = 'http://127.0.0.1:%d' % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        Fake.seen, Fake.answers = [], {}
        Fake.jobs = [
            {'id': 11, 'name': 'other', 'runner_name': 'r2', 'status': 'in_progress', 'started_at': STARTED},
            {'id': 12, 'name': 'build', 'runner_name': 'r1', 'status': 'in_progress', 'started_at': STARTED,
             'steps': [{'name': 'Checkout', 'conclusion': 'success'}, {'name': 'Unit tests', 'conclusion': 'failure'},
                       {'name': 'Report', 'conclusion': None}]},
        ]

    def env(self, **over):
        env = {
            'JOB_STATUS': 'failure', 'CI_ENDPOINT': self.base + '/ci/v1/events', 'CI_EVENTS_KEY': 'k' * 20,
            'NTFY_TOPIC': 'topic-x', 'NTFY_SERVER': self.base + '/ntfy', 'GITHUB_TOKEN': 't',
            'GITHUB_API_URL': self.base + '/api', 'GITHUB_SERVER_URL': 'https://github.com',
            'GITHUB_REPOSITORY': 'owner/app', 'GITHUB_RUN_ID': '99', 'GITHUB_RUN_ATTEMPT': '2', 'GITHUB_JOB': 'build',
            'GITHUB_WORKFLOW': 'Play', 'GITHUB_EVENT_NAME': 'schedule', 'GITHUB_REF_NAME': 'main',
            'GITHUB_SHA': 'abcdef0123456789', 'RUNNER_NAME': 'r1',
        }
        env.update(over)
        return {k: v for k, v in env.items() if v is not None}

    def run_main(self, **over):
        self.assertEqual(ci_report.main(self.env(**over), now_ms=NOW, sleep=lambda s: None), 0)
        return [(m, p, h, b) for m, p, h, b in Fake.seen]

    def posted(self, prefix):
        return [(h, b) for m, p, h, b in Fake.seen if m == 'POST' and p.startswith(prefix)]

    def test_a_failure_sends_the_record_and_pushes(self):
        self.run_main()
        [(headers, body)] = self.posted('/ci/v1/events')
        self.assertEqual(headers['Content-Type'], 'text/plain')
        self.assertEqual(headers['X-Events-Key'], 'k' * 20)
        self.assertRegex(headers['X-Batch'], r'^[0-9a-f-]{36}$')
        self.assertTrue(body.endswith(b'\n'))
        rec = json.loads(body)
        self.assertEqual((rec['app'], rec['platform'], rec['form'], rec['event'], rec['screen']),
                         ('ci', 'ci', 'runner', 'ci.run', 'Play'))
        self.assertEqual(rec['session'], 'app/99/2/12')
        self.assertIsNone(rec['player'])
        self.assertEqual(rec['details'], {
            'repo': 'app', 'workflow': 'Play', 'job': 'build', 'trigger': 'schedule', 'ref': 'main', 'sha': 'abcdef0',
            'outcome': 'failure', 'duration_ms': 100000, 'failed_step': 'Unit tests',
            'url': 'https://github.com/owner/app/actions/runs/99'})
        [(headers, body)] = self.posted('/ntfy/topic-x')
        self.assertEqual(headers['Title'], 'app: Play failed')
        self.assertEqual(headers['Click'], 'https://github.com/owner/app/actions/runs/99')
        self.assertEqual(headers['Priority'], 'high')
        self.assertEqual(body.decode(), 'Step "Unit tests" failed\nschedule on main at abcdef0')
        [get] = [p for m, p, h, b in Fake.seen if m == 'GET']
        self.assertEqual(get, '/api/repos/owner/app/actions/runs/99/attempts/2/jobs?per_page=100')

    def test_a_success_or_a_cancel_sends_the_record_and_pushes_nothing(self):
        for status in ('success', 'cancelled'):
            Fake.seen = []
            self.run_main(JOB_STATUS=status)
            self.assertEqual(len(self.posted('/ci/v1/events')), 1)
            self.assertEqual(self.posted('/ntfy'), [])

    def test_no_secrets_sends_nothing(self):
        self.run_main(CI_EVENTS_KEY='', NTFY_TOPIC='')
        self.assertEqual(Fake.seen, [])

    def test_a_topic_alone_still_pushes(self):
        self.run_main(CI_EVENTS_KEY='')
        self.assertEqual(self.posted('/ci/v1/events'), [])
        self.assertEqual(len(self.posted('/ntfy/topic-x')), 1)

    def test_no_api_access_still_reports_without_duration_or_step(self):
        Fake.answers = {'/api': [403]}
        self.run_main()
        [(_, body)] = self.posted('/ci/v1/events')
        d = json.loads(body)['details']
        self.assertEqual((d['duration_ms'], d['failed_step'], d['job']), (None, None, 'build'))
        self.assertEqual(json.loads(body)['session'], 'app/99/2/build')
        [(_, push)] = self.posted('/ntfy')
        self.assertEqual(push.decode().splitlines()[0], 'The job "build" failed')

    def test_no_token_asks_nothing_of_the_api(self):
        self.run_main(GITHUB_TOKEN='')
        self.assertEqual([p for m, p, h, b in Fake.seen if m == 'GET'], [])

    def test_two_jobs_on_one_runner_name_are_not_guessed(self):
        Fake.jobs[0]['runner_name'] = 'r1'
        self.run_main()
        [(_, body)] = self.posted('/ci/v1/events')
        self.assertIsNone(json.loads(body)['details']['duration_ms'])

    def test_a_5xx_is_tried_again_and_a_4xx_is_not(self):
        Fake.answers = {'/ci/': [503, 503, 204]}
        self.run_main(NTFY_TOPIC='')
        self.assertEqual(len(self.posted('/ci/v1/events')), 3)
        Fake.seen, Fake.answers = [], {'/ci/': [403]}
        self.run_main(NTFY_TOPIC='')
        self.assertEqual(len(self.posted('/ci/v1/events')), 1)

    def test_an_endpoint_that_never_answers_never_fails_the_job(self):
        self.run_main(CI_ENDPOINT='http://127.0.0.1:9/x', NTFY_SERVER='http://127.0.0.1:9')

    def test_a_key_with_no_endpoint_sends_no_record(self):
        self.run_main(CI_ENDPOINT='')
        self.assertEqual(self.posted('/ci'), [])

    def test_anything_unexpected_is_a_warning(self):
        Fake.jobs = [{'id': 12, 'runner_name': 'r1', 'status': 'in_progress', 'started_at': STARTED, 'steps': 'x'}]
        self.run_main()   # 'x' has no .get: caught, exit 0

    def test_a_title_is_plain_ascii(self):
        self.assertEqual(ci_report.ascii_line('café ✅ ok'), 'caf? ? ok')
        self.assertIsNone(ci_report.parse_time('yesterday'))
        self.assertIsNone(ci_report.parse_time(None))

    def test_every_action_manifest_is_valid_yaml(self):
        # 2026-10-29: an unquoted description holding "actions: read" made ci/action.yml unreadable, and every job that
        # used it failed at "Set up job". A plain (unquoted) scalar may not hold ": ", and a description holds no
        # expression. Stdlib only, so the YAML rule is checked by hand; with PyYAML present the files are parsed too.
        for rel in ('action.yml', os.path.join('ci', 'action.yml')):
            with open(os.path.join(ROOT, rel)) as f:
                text = f.read()
            for n, line in enumerate(text.splitlines(), 1):
                m = re.match(r'^\s*(?:- )?([A-Za-z0-9_-]+): (.+)$', line)
                if not m or line.lstrip().startswith('#'):
                    continue
                key, value = m.group(1), m.group(2)
                with self.subTest(file=rel, line=n):
                    if value[0] not in '\'"|>[{':
                        self.assertNotIn(': ', value, 'quote this value')
                    if key == 'description':
                        self.assertNotIn('${{', value)
            try:
                import yaml
            except ImportError:
                continue
            self.assertIsInstance(yaml.safe_load(text), dict, rel)

    def test_this_repos_own_workflows_can_always_start(self):
        # 2026-10-29: release.yml called test.yml granting contents only, while test.yml asked for actions too, so the
        # release did not start; and both used ci@v1, so a broken v1 could block the release that fixes it.
        wf = os.path.join(ROOT, '.github', 'workflows')

        def perms(block):
            return set(re.findall(r'^ +([a-z-]+): (?:read|write)', block, re.M))

        with open(os.path.join(wf, 'test.yml')) as f:
            test = f.read()
        with open(os.path.join(wf, 'release.yml')) as f:
            release = f.read()
        asked = perms(re.search(r'^permissions:\n((?: {2}.*\n)+)', test, re.M).group(1))
        call = re.search(r'\n  test:\n((?: {4}.*\n)+)', release).group(1)
        self.assertIn('uses: ./.github/workflows/test.yml', call)
        granted = perms(re.search(r' {4}permissions:\n((?: {6}.*\n)+)', call).group(1))
        self.assertLessEqual(asked, granted, 'release.yml must grant test.yml every permission it asks for')
        for text in (test, release):
            self.assertNotIn('flarepress/ci@', text)
            self.assertIn('uses: ./ci', text)

    def test_the_action_runs_this_file_and_never_fails(self):
        with open(os.path.join(ROOT, 'ci', 'action.yml')) as f:
            src = f.read()
        self.assertIn('python3 "$GITHUB_ACTION_PATH/../tools/ci_report.py" || true', src)
        for name in ('JOB_STATUS', 'CI_ENDPOINT', 'CI_EVENTS_KEY', 'NTFY_TOPIC', 'NTFY_SERVER', 'GITHUB_TOKEN'):
            self.assertIn('%s: ${{ inputs.' % name, src)


if __name__ == '__main__':
    unittest.main()
