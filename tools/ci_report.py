#!/usr/bin/env python3
"""Report one GitHub Actions job to the telemetry events wire, and push a failure to a phone (ci/action.yml).

    ci_report.py

Run as a job's last step (`if: always()`). It sends one `ci.run` record in the standard's shape (the telemetry repo's
docs/standard.md §3) to CI_ENDPOINT, the flarepress events wire with a key (AGENTS.md "Telemetry events"), and, when
the job failed and NTFY_TOPIC is set, one message to that ntfy topic. It never fails the job: every problem is a
warning and the exit is 0. With neither CI_EVENTS_KEY nor NTFY_TOPIC (a fork's pull request has no secrets) it says
so and sends nothing.

Environment, from the action's inputs:
  JOB_STATUS      the job's status so far: success, failure or cancelled (`${{ job.status }}`)
  CI_ENDPOINT     the events URL; CI_EVENTS_KEY its X-Events-Key
  NTFY_TOPIC      the ntfy topic (a long random name: whoever knows it can read it); NTFY_SERVER its server
  GITHUB_TOKEN    optional: reads the job's start time and its failed step (needs `actions: read`)
and GitHub's own GITHUB_* and RUNNER_NAME variables.

The record: app `ci`, platform `ci`, form `runner`, version `v1` (this report's format), no player, session
`<repo>/<run>/<attempt>/<job>`, seq 0, event `ci.run`, screen the workflow's name. Its details: repo, workflow, job,
trigger, ref, sha (7), outcome, duration_ms (null when the job's start is unknown), failed_step, url.

Stdlib only, Python 3.8+.
"""
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

FORMAT = 'v1'
TIMEOUT = 10
TRIES = 3


def warn(msg):
    print('::warning::ci report: %s' % msg)


def http(method, url, body=None, headers=None, tries=1, sleep=time.sleep):
    """(status, body bytes) or (None, why). Retries a network error or a 5xx."""
    why = None
    for i in range(tries):
        if i:
            sleep(2 ** i)
        req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            if e.code < 500:
                return e.code, e.read()
            why = 'HTTP %d' % e.code
        except (urllib.error.URLError, OSError) as e:
            why = str(getattr(e, 'reason', e))
    return None, why


def parse_time(s):
    """Epoch ms from GitHub's ISO time, or None."""
    if not s:
        return None
    try:
        t = datetime.datetime.strptime(s, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    return int(t.timestamp() * 1000)


def find_job(env, sleep=time.sleep):
    """This job's entry in the run's job list (matched by its runner), or None."""
    token = env.get('GITHUB_TOKEN')
    if not token:
        return None
    url = '%s/repos/%s/actions/runs/%s/attempts/%s/jobs?per_page=100' % (
        env.get('GITHUB_API_URL', 'https://api.github.com'), env.get('GITHUB_REPOSITORY', ''),
        env.get('GITHUB_RUN_ID', ''), env.get('GITHUB_RUN_ATTEMPT', '1'))
    status, body = http('GET', url, headers={'Authorization': 'Bearer %s' % token,
                                             'Accept': 'application/vnd.github+json'}, sleep=sleep)
    if status != 200:
        warn('could not read the job (%s); give the workflow `actions: read` for its start and failed step'
             % (status or body))
        return None
    try:
        jobs = json.loads(body).get('jobs') or []
    except ValueError:
        return None
    runner = env.get('RUNNER_NAME')
    mine = [j for j in jobs if j.get('runner_name') == runner and j.get('status') != 'completed']
    return mine[0] if len(mine) == 1 else None


def failed_step(job):
    for step in (job or {}).get('steps') or []:
        if step.get('conclusion') == 'failure':
            return step.get('name')
    return None


def record(env, job, now_ms):
    repo = env.get('GITHUB_REPOSITORY', '').rsplit('/', 1)[-1] or 'unknown'
    run, attempt = env.get('GITHUB_RUN_ID', '0'), env.get('GITHUB_RUN_ATTEMPT', '1')
    name = str((job or {}).get('id') or env.get('GITHUB_JOB') or 'job')
    started = parse_time((job or {}).get('started_at'))
    workflow = env.get('GITHUB_WORKFLOW') or None
    return {
        'app': 'ci', 'platform': 'ci', 'form': 'runner', 'version': FORMAT, 'player': None,
        'session': ('%s/%s/%s/%s' % (repo, run, attempt, name))[:200], 'seq': 0, 'at': now_ms, 'tz': 0,
        'event': 'ci.run', 'screen': workflow[:200] if workflow else None,
        'details': {
            'repo': repo,
            'workflow': workflow,
            'job': (job or {}).get('name') or env.get('GITHUB_JOB'),
            'trigger': env.get('GITHUB_EVENT_NAME'),
            'ref': env.get('GITHUB_REF_NAME'),
            'sha': (env.get('GITHUB_SHA') or '')[:7] or None,
            'outcome': env.get('JOB_STATUS') or 'unknown',
            'duration_ms': now_ms - started if started and started <= now_ms else None,
            'failed_step': failed_step(job),
            'url': '%s/%s/actions/runs/%s' % (env.get('GITHUB_SERVER_URL', 'https://github.com'),
                                              env.get('GITHUB_REPOSITORY', ''), run),
        },
    }


def send_record(env, rec, sleep=time.sleep):
    status, why = http('POST', env['CI_ENDPOINT'], body=(json.dumps(rec, separators=(',', ':')) + '\n').encode(),
                       headers={'Content-Type': 'text/plain', 'X-Batch': str(uuid.uuid4()),
                                'X-Events-Key': env['CI_EVENTS_KEY']}, tries=TRIES, sleep=sleep)
    if status is not None and 200 <= status < 300:
        print('ci report: sent %s %s' % (rec['event'], rec['details']['outcome']))
        return True
    warn('the events endpoint answered %s' % (status or why))
    return False


def ascii_line(s):
    return ''.join(c if 32 <= ord(c) < 127 else '?' for c in s)[:200]


def push(env, rec, sleep=time.sleep):
    d = rec['details']
    title = '%s: %s failed' % (d['repo'], d['workflow'] or d['job'] or 'a workflow')
    lines = ['%s failed' % (('Step "%s"' % d['failed_step']) if d['failed_step'] else 'The job "%s"' % d['job'])]
    lines.append('%s on %s at %s' % (d['trigger'], d['ref'], d['sha']))
    server = (env.get('NTFY_SERVER') or 'https://ntfy.sh').rstrip('/')
    status, why = http('POST', '%s/%s' % (server, env['NTFY_TOPIC']), body='\n'.join(lines).encode(),
                       headers={'Title': ascii_line(title), 'Priority': 'high', 'Tags': 'rotating_light',
                                'Click': d['url']}, tries=TRIES, sleep=sleep)
    if status is not None and 200 <= status < 300:
        print('ci report: pushed the failure')
        return True
    warn('ntfy answered %s' % (status or why))
    return False


def main(env=None, now_ms=None, sleep=time.sleep):
    env = os.environ if env is None else env
    send = bool(env.get('CI_EVENTS_KEY') and env.get('CI_ENDPOINT'))
    alert = bool(env.get('NTFY_TOPIC')) and env.get('JOB_STATUS') == 'failure'
    if not env.get('CI_EVENTS_KEY') and not env.get('NTFY_TOPIC'):
        print('ci report: no CI_EVENTS_KEY or NTFY_TOPIC here (a fork, or not set up): nothing sent')
        return 0
    if env.get('CI_EVENTS_KEY') and not env.get('CI_ENDPOINT'):
        warn('a key but no endpoint: the record is not sent')
    try:
        rec = record(env, find_job(env, sleep=sleep), int(time.time() * 1000) if now_ms is None else now_ms)
        if send:
            send_record(env, rec, sleep=sleep)
        if alert:
            push(env, rec, sleep=sleep)
    except Exception as e:  # never fail the job it reports on
        warn('%s: %s' % (type(e).__name__, e))
    return 0


if __name__ == '__main__':
    sys.exit(main())
