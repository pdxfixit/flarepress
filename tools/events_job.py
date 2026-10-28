#!/usr/bin/env python3
"""The telemetry runner's job for an app's event batches in R2 (the shared wire format, AGENTS.md "Telemetry events").

    events_job.py pull --app APP --bucket BUCKET [--events FILE]
    events_job.py ack --bucket BUCKET

The runner (pdxfixit/telemetry, README "The job contract") runs it from this repo's root in a fresh container:

  pull  lists v1/ in the bucket, downloads each object not stored yet (at most MAX_OBJECTS a run; the rest wait for
        the next run), checks every line against the standard's record (§3 of the telemetry repo's docs/standard.md),
        and writes:
          $TELEMETRY_OUT/events.sqlite3   table `events`: this run's new events, key (app, session, seq)
          $TELEMETRY_OUT/data.sqlite3     the job's archive, added to: `daily_events` (counts by day, app, platform,
                                          form, version, event and the event's summary keys), `day_players` (who
                                          played each day), `daily_players` (how many), and `event_batches` (one row
                                          per object, so none is counted twice)
          $TELEMETRY_OUT/raw/<key>        each object as it came; one with a bad line under raw/rejected/<key>
          $TELEMETRY_OUT/manifest.json    "delete": the objects stored, "left": the objects kept in the bucket
  ack   deletes exactly the objects manifest.json lists under "delete", and nothing else. The runner calls it only
        after it stored the run.

--events is the event list, events.toml in the telemetry repo: each event's `summary` keys say what daily_events
counts it by, and its `details` keys which keys are expected. Default: ../telemetry/events.toml beside this repo
(the runner's {src}/telemetry/events.toml). An event or details key the list does not have is stored and counted,
and named in the output: the list is fixed, not the data.

The check. A line is a JSON object with exactly the record's columns, of the right types, for --app. A line that
fails is not stored; its object is kept in the bucket for good (never in "delete"), and its bytes go under
raw/rejected/. The object's good lines are stored. An object not named as the Worker names it is left unread.

Duplicates. The client sends a batch again with the same id until it gets a 2xx, and a retry across midnight UTC lands
under the next day's name. A batch id already stored is not counted again; the copy is deleted when its bytes match.
Within a run, an event key seen twice is stored and counted once.

Environment: TELEMETRY_OUT; R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY (an R2 token, Object Read & Write on this
bucket only); R2_ACCOUNT_ID, the Cloudflare account (kept out of this public repo). R2_ENDPOINT replaces the
Cloudflare endpoint (the tests' stand-in R2).

Exit codes: 0 pulled (an empty bucket too), 75 R2 unreachable, 1 failed, 2 usage. Stdlib only, Python 3.8+: on 3.11+
events.toml is read with tomllib, before that with read_toml below, which takes the subset events.toml uses.
"""
import argparse
import datetime
import hashlib
import hmac
import json
import os
import re
import socket
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

PREFIX = 'v1/'
UNREACHABLE = 75
MAX_OBJECTS = 1000   # per run, so one run's work and the runner's writes stay bounded
S3 = '{http://s3.amazonaws.com/doc/2006-03-01/}'
UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
# the Worker's object name: v1/<UTC day>/<batch>.jsonl (events/events-worker.js objectKey)
KEY = re.compile(r'^v1/(\d{4}-\d{2}-\d{2})/(' + UUID + r')\.jsonl$')
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_EVENTS = os.path.join(os.path.dirname(os.path.dirname(HERE)), 'telemetry', 'events.toml')

COLUMNS = ('app', 'platform', 'form', 'version', 'player', 'session', 'seq', 'at', 'tz', 'event', 'screen', 'details')
PLATFORMS = ('web', 'android', 'ci')   # ci: a GitHub Actions job (tools/ci_report.py)
FORMS = ('phone', 'tablet', 'watch', 'desktop', 'runner')
MAX_AT = 253402300800000   # 10000-01-01 UTC in ms: past it a time is not a date
MAX_TEXT = 200             # longest version, session, event or screen name taken

EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS events (
    app      TEXT NOT NULL,
    platform TEXT NOT NULL,
    form     TEXT NOT NULL,
    version  TEXT NOT NULL,
    player   TEXT,
    session  TEXT NOT NULL,
    seq      INTEGER NOT NULL,
    at       INTEGER NOT NULL,   -- UTC, epoch ms
    tz       INTEGER NOT NULL,   -- UTC offset, minutes
    event    TEXT NOT NULL,
    screen   TEXT,
    details  TEXT NOT NULL,      -- a JSON object
    PRIMARY KEY (app, session, seq)
);
"""

DATA_DDL = """
CREATE TABLE IF NOT EXISTS daily_events (
    day      TEXT NOT NULL,      -- UTC YYYY-MM-DD of the event's `at`
    app      TEXT NOT NULL,
    platform TEXT NOT NULL,
    form     TEXT NOT NULL,
    version  TEXT NOT NULL,
    event    TEXT NOT NULL,
    key      TEXT NOT NULL,      -- JSON object of the event's summary keys (null when absent), sorted
    n        INTEGER NOT NULL,
    PRIMARY KEY (day, app, platform, form, version, event, key)
);
CREATE TABLE IF NOT EXISTS day_players (
    day      TEXT NOT NULL,
    app      TEXT NOT NULL,
    platform TEXT NOT NULL,
    form     TEXT NOT NULL,
    player   TEXT NOT NULL,
    PRIMARY KEY (day, app, platform, form, player)
);
CREATE TABLE IF NOT EXISTS daily_players (
    day      TEXT NOT NULL,
    app      TEXT NOT NULL,
    platform TEXT NOT NULL,
    form     TEXT NOT NULL,
    players  INTEGER NOT NULL,   -- rows of day_players
    PRIMARY KEY (day, app, platform, form)
);
CREATE TABLE IF NOT EXISTS event_batches (
    key      TEXT PRIMARY KEY,   -- the R2 object's name
    batch    TEXT NOT NULL,      -- its batch id (X-Batch)
    day      TEXT NOT NULL,      -- the day in its name
    bytes    INTEGER NOT NULL,
    sha256   TEXT NOT NULL,
    lines    INTEGER NOT NULL,
    good     INTEGER NOT NULL,   -- lines that passed the check
    added    INTEGER NOT NULL,   -- events stored and counted from it
    bad      INTEGER NOT NULL,
    status   TEXT NOT NULL,      -- stored, duplicate or rejected
    reason   TEXT,
    pulled   TEXT NOT NULL       -- when this job first read it, UTC
);
"""


class Unreachable(Exception):
    """R2 did not answer, or answered that it cannot right now: exit 75, and the next run tries again."""


class Fail(Exception):
    """Anything else: the run fails and nothing is stored or deleted."""


def now():
    return datetime.datetime.now(datetime.timezone.utc)


# --- events.toml ---------------------------------------------------------------------------------------------------


def read_toml(text):
    """The TOML events.toml uses: [tables] with dotted and quoted names, key = value, values that are strings,
    integers, true/false, arrays (across lines, with comments and a trailing comma) and one-line inline tables.
    Anything else is a ValueError."""
    p = {'i': 0}
    n = len(text)

    def err(msg):
        line = text.count('\n', 0, p['i']) + 1
        raise ValueError('events.toml line %d: %s' % (line, msg))

    def peek():
        return text[p['i']] if p['i'] < n else ''

    def skip(newlines):
        while p['i'] < n:
            c = text[p['i']]
            if c in ' \t\r' or (newlines and c == '\n'):
                p['i'] += 1
            elif c == '#':
                while p['i'] < n and text[p['i']] != '\n':
                    p['i'] += 1
            else:
                break

    def string():
        p['i'] += 1
        out = []
        escapes = {'"': '"', '\\': '\\', 'n': '\n', 't': '\t', 'r': '\r', 'b': '\b', 'f': '\f'}
        while True:
            if p['i'] >= n or text[p['i']] == '\n':
                err('a string is not closed')
            c = text[p['i']]
            p['i'] += 1
            if c == '"':
                return ''.join(out)
            if c == '\\':
                e = text[p['i']:p['i'] + 1]
                p['i'] += 1
                if e in escapes:
                    out.append(escapes[e])
                elif e == 'u' and re.match(r'[0-9A-Fa-f]{4}$', text[p['i']:p['i'] + 4]):
                    out.append(chr(int(text[p['i']:p['i'] + 4], 16)))
                    p['i'] += 4
                else:
                    err('an escape it does not read: \\%s' % e)
            else:
                out.append(c)

    def key():
        if peek() == '"':
            return string()
        m = re.compile(r'[A-Za-z0-9_-]+').match(text, p['i'])
        if not m:
            err('expected a key')
        p['i'] = m.end()
        return m.group(0)

    def dotted():
        parts = [key()]
        skip(False)
        while peek() == '.':
            p['i'] += 1
            skip(False)
            parts.append(key())
            skip(False)
        return parts

    def put(table, parts, value):
        for part in parts[:-1]:
            table = table.setdefault(part, {})
            if not isinstance(table, dict):
                err('%s is not a table' % part)
        if parts[-1] in table:
            err('%s is set twice' % parts[-1])
        table[parts[-1]] = value

    def value():
        c = peek()
        if c == '"':
            return string()
        if c == '[':
            p['i'] += 1
            items = []
            while True:
                skip(True)
                if peek() == ']':
                    p['i'] += 1
                    return items
                items.append(value())
                skip(True)
                if peek() == ',':
                    p['i'] += 1
                elif peek() != ']':
                    err('expected , or ] in an array')
        if c == '{':
            p['i'] += 1
            table = {}
            skip(False)
            if peek() == '}':
                p['i'] += 1
                return table
            while True:
                skip(False)
                parts = dotted()
                if peek() != '=':
                    err('expected = in an inline table')
                p['i'] += 1
                skip(False)
                put(table, parts, value())
                skip(False)
                if peek() == ',':
                    p['i'] += 1
                elif peek() == '}':
                    p['i'] += 1
                    return table
                else:
                    err('expected , or } in an inline table')
        m = re.compile(r'(true|false|[+-]?[0-9]+)(?=[\s,\]}#]|$)').match(text, p['i'])
        if not m:
            err('a value it does not read')
        p['i'] = m.end()
        word = m.group(1)
        if word in ('true', 'false'):
            return word == 'true'
        return int(word)

    root = {}
    table = root
    while True:
        skip(True)
        if p['i'] >= n:
            return root
        if peek() == '[':
            if text.startswith('[[', p['i']):
                err('arrays of tables are not read')
            p['i'] += 1
            skip(False)
            parts = dotted()
            if peek() != ']':
                err('expected ] after a table name')
            p['i'] += 1
            table = root
            for part in parts:
                table = table.setdefault(part, {})
                if not isinstance(table, dict):
                    err('%s is not a table' % part)
        else:
            parts = dotted()
            if peek() != '=':
                err('expected =')
            p['i'] += 1
            skip(False)
            put(table, parts, value())
        skip(False)
        if p['i'] < n and peek() != '\n':
            err('more on the line than one key = value')


def load_events(path):
    """{event name: (set of details keys, list of summary keys)}, and the apps table, from events.toml."""
    try:
        with open(path, 'rb') as f:
            raw = f.read()
    except OSError as e:
        raise Fail('cannot read the event list %s: %s' % (path, e))
    try:
        import tomllib   # Python 3.11+
        parse = tomllib.loads
    except ImportError:
        parse = read_toml
    try:
        doc = parse(raw.decode('utf-8'))
    except ValueError as e:   # tomllib.TOMLDecodeError and UnicodeDecodeError are ValueErrors
        raise Fail('the event list %s does not parse: %s' % (path, e))
    events = {}
    for name, spec in (doc.get('events') or {}).items():
        if not isinstance(spec, dict):
            raise Fail('events.%s in %s is not a table' % (name, path))
        details, summary = spec.get('details') or [], spec.get('summary') or []
        if not all(isinstance(x, list) and all(isinstance(k, str) for k in x) for x in (details, summary)):
            raise Fail('events.%s in %s: details and summary must be lists of names' % (name, path))
        events[name] = (set(details), list(summary))
    return events, doc.get('apps') or {}


# --- the check -----------------------------------------------------------------------------------------------------


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def is_text(v, empty=False):
    return isinstance(v, str) and (empty or v != '') and len(v) <= MAX_TEXT


def check(record, app, platform=None):
    """None when the record has the standard's shape for this app, else why not."""
    if not isinstance(record, dict):
        return 'not a JSON object'
    missing = [c for c in COLUMNS if c not in record]
    extra = sorted(k for k in record if k not in COLUMNS)
    if missing:
        return 'no %s' % ', '.join(missing)
    if extra:
        return 'unknown columns %s' % ', '.join(extra)
    r = record
    tests = (
        ('app', r['app'] == app),
        ('platform', r['platform'] in PLATFORMS and (platform is None or r['platform'] == platform)),
        ('form', r['form'] in FORMS),
        ('version', is_text(r['version'])),
        ('player', r['player'] is None or is_text(r['player'])),
        ('session', is_text(r['session'])),
        ('seq', is_int(r['seq']) and r['seq'] >= 0),
        ('at', is_int(r['at']) and 0 < r['at'] < MAX_AT),
        ('tz', is_int(r['tz']) and -1440 <= r['tz'] <= 1440),
        ('event', is_text(r['event'])),
        ('screen', r['screen'] is None or is_text(r['screen'], empty=True)),
        ('details', isinstance(r['details'], dict)),
    )
    bad = [name for name, ok in tests if not ok]
    return 'bad %s' % ', '.join(bad) if bad else None


def utc_day(ms):
    return datetime.datetime.fromtimestamp(ms / 1000.0, datetime.timezone.utc).strftime('%Y-%m-%d')


def summary_key(details, keys):
    return json.dumps({k: details.get(k) for k in keys}, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


# --- S3 signing (the telemetry runner's sigv4, with a query string for ListObjectsV2) ---------------------------------


def stamp(t):
    return t.strftime('%Y%m%dT%H%M%SZ')


def quote(s, safe='-_.~'):
    return urllib.parse.quote(s, safe=safe)


def canonical_query(query):
    return '&'.join('%s=%s' % (quote(k), quote(v)) for k, v in sorted(query.items()))


def sigv4(method, host, path, extra, body, key_id, secret_key, region, when, query=None):
    """AWS Signature Version 4 headers for one S3 request (R2 uses region 'auto'). path is already URI-encoded."""
    amzdate = stamp(when)
    day = amzdate[:8]
    payload = hashlib.sha256(body).hexdigest()
    headers = {'host': host, 'x-amz-content-sha256': payload, 'x-amz-date': amzdate}
    headers.update({k.lower(): v for k, v in extra.items()})
    names = sorted(headers)
    canonical = '\n'.join([
        method, path, canonical_query(query or {}),
        ''.join('%s:%s\n' % (h, headers[h].strip()) for h in names),
        ';'.join(names), payload,
    ])
    scope = '%s/%s/s3/aws4_request' % (day, region)
    to_sign = '\n'.join(['AWS4-HMAC-SHA256', amzdate, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    k = ('AWS4' + secret_key).encode()
    for part in (day, region, 's3', 'aws4_request'):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    headers['authorization'] = ('AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s'
                                % (key_id, scope, ';'.join(names), signature))
    del headers['host']
    return headers


class R2:
    """The three S3 calls the job makes, against one bucket only."""

    def __init__(self, env, bucket):
        self.bucket = bucket
        self.key_id = env.get('R2_ACCESS_KEY_ID', '').strip()
        self.secret = env.get('R2_SECRET_ACCESS_KEY', '').strip()
        if not self.key_id or not self.secret:
            raise Fail('R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY must both be set')
        endpoint = env.get('R2_ENDPOINT', '').strip()
        if not endpoint:
            account = env.get('R2_ACCOUNT_ID', '').strip()
            if not re.match(r'^[0-9a-f]{32}$', account):
                raise Fail('R2_ACCOUNT_ID must be the 32-character Cloudflare account id')
            endpoint = 'https://' + account + '.r2.cloudflarestorage.com'
        u = urllib.parse.urlsplit(endpoint)
        self.scheme, self.host = u.scheme, u.netloc

    def call(self, method, key='', query=None):
        path = '/' + self.bucket + ('/' + quote(key, safe='/-_.~') if key else '')
        headers = sigv4(method, self.host, path, {}, b'', self.key_id, self.secret, 'auto', now(), query)
        url = '%s://%s%s' % (self.scheme, self.host, path) + ('?' + canonical_query(query) if query else '')
        req = urllib.request.Request(url, method=method, headers=headers)
        what = '%s %s' % (method, key or 'list')
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            body = e.read()[:300].decode(errors='replace')
            if e.code == 404 and method == 'DELETE':
                return 404, b''
            if e.code >= 500 or e.code == 429:
                raise Unreachable('R2 %d on %s: %s' % (e.code, what, body))
            raise Fail('R2 %d on %s: %s' % (e.code, what, body))
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
            raise Unreachable('R2 unreachable on %s: %s' % (what, e))

    def list(self, limit):
        """[(key, size)] under PREFIX, in name order, at most limit."""
        found, token = [], None
        while len(found) < limit:
            query = {'list-type': '2', 'prefix': PREFIX, 'max-keys': str(min(1000, limit - len(found)))}
            if token:
                query['continuation-token'] = token
            _, body = self.call('GET', query=query)
            root = ET.fromstring(body)
            for c in root.findall(S3 + 'Contents'):
                found.append((c.findtext(S3 + 'Key'), int(c.findtext(S3 + 'Size') or -1)))
            token = root.findtext(S3 + 'NextContinuationToken')
            if root.findtext(S3 + 'IsTruncated') != 'true' or not token:
                break
        return found[:limit]

    def get(self, key):
        return self.call('GET', key)[1]

    def delete(self, key):
        return self.call('DELETE', key)[0]


# --- pull and ack --------------------------------------------------------------------------------------------------


def out_dir(env):
    out = env.get('TELEMETRY_OUT', '').strip()
    if not out:
        raise Fail('TELEMETRY_OUT is not set')
    os.makedirs(out, exist_ok=True)
    return out


def keep(out, sub, key, data):
    path = os.path.join(out, sub, *key.split('/'))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)


def read_lines(data, app, platform):
    """([good records], [(line number, reason)], line count); a body that is not UTF-8 is one reason for line 0."""
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError as e:
        return [], [(0, 'not UTF-8: %s' % e)], 0
    lines = text.split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    good, bad = [], []
    for n, line in enumerate(lines, 1):
        try:
            record = json.loads(line)
        except ValueError:
            bad.append((n, 'not JSON'))
            continue
        why = check(record, app, platform)
        if why:
            bad.append((n, why))
        else:
            good.append(record)
    return good, bad, len(lines)


class Counts:
    def __init__(self):
        self.events = 0
        self.unlisted = {}   # 'event' or 'event.key' -> count

    def note(self, name):
        self.unlisted[name] = self.unlisted.get(name, 0) + 1


def store(records, events_db, data_db, listed, counts):
    """Store the records not stored yet this run and count them; returns how many were new."""
    added = 0
    days = set()
    for r in records:
        details = json.dumps(r['details'], sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        cur = events_db.execute('INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                                tuple(r[c] if c != 'details' else details for c in COLUMNS))
        if cur.rowcount != 1:
            continue
        added += 1
        spec = listed.get(r['event'])
        if spec is None:
            counts.note(r['event'])
            summary = []
        else:
            summary = spec[1]
            for k in sorted(r['details']):
                if k not in spec[0]:
                    counts.note('%s.%s' % (r['event'], k))
        day = utc_day(r['at'])
        days.add(day)
        data_db.execute(
            'INSERT INTO daily_events VALUES (?, ?, ?, ?, ?, ?, ?, 1) '
            'ON CONFLICT (day, app, platform, form, version, event, key) DO UPDATE SET n = n + 1',
            (day, r['app'], r['platform'], r['form'], r['version'], r['event'], summary_key(r['details'], summary)))
        if r['player']:
            data_db.execute('INSERT OR IGNORE INTO day_players VALUES (?, ?, ?, ?, ?)',
                            (day, r['app'], r['platform'], r['form'], r['player']))
    for day in sorted(days):
        data_db.execute('INSERT OR REPLACE INTO daily_players SELECT day, app, platform, form, COUNT(*) '
                        'FROM day_players WHERE day = ? GROUP BY day, app, platform, form', (day,))
    counts.events += added
    return added


def pull(env, app, bucket, events_path):
    listed, apps = load_events(events_path)
    platform = (apps.get(app) or {}).get('platform') if isinstance(apps.get(app), dict) else None
    out = out_dir(env)
    r2 = R2(env, bucket)
    data_db = sqlite3.connect(os.path.join(out, 'data.sqlite3'))
    events_db = sqlite3.connect(os.path.join(out, 'events.sqlite3'))
    try:
        data_db.executescript(DATA_DDL)
        events_db.executescript(EVENTS_DDL)
        manifest = run_pull(r2, out, app, bucket, listed, platform, data_db, events_db)
    finally:
        data_db.close()
        events_db.close()
    tmp = os.path.join(out, 'manifest.json.tmp')
    with open(tmp, 'w') as f:
        json.dump(manifest, f, indent=1, sort_keys=True)
        f.write('\n')
    os.replace(tmp, os.path.join(out, 'manifest.json'))
    return 0


def run_pull(r2, out, app, bucket, listed, platform, data_db, events_db):
    stored = {row[0]: row[1:] for row in data_db.execute(
        'SELECT key, batch, sha256, status, reason FROM event_batches')}
    by_batch = {}
    for key, (batch, sha, status, _) in stored.items():
        if status != 'duplicate':
            by_batch.setdefault(batch, (key, sha))
    objects = r2.list(MAX_OBJECTS)
    pulled = now().strftime('%Y-%m-%dT%H:%M:%SZ')
    delete, left, counts = [], [], Counts()
    new = again = 0
    for key, size in objects:
        m = KEY.match(key or '')
        if not m:
            left.append({'key': key, 'bytes': size, 'reason': 'not named as the Worker names objects'})
            continue
        if key in stored:
            _, sha, status, reason = stored[key]
            if status == 'rejected':
                left.append({'key': key, 'bytes': size, 'reason': reason})
            else:
                delete.append({'key': key, 'sha256': sha, 'bytes': size, 'stored': 'earlier run'})
                again += 1
            continue
        day, batch = m.groups()
        data = r2.get(key)
        sha = hashlib.sha256(data).hexdigest()
        if len(data) != size:
            keep(out, 'raw/rejected', key, data)
            left.append({'key': key, 'sha256': sha, 'bytes': len(data),
                         'reason': 'got %d bytes, the listing said %d' % (len(data), size)})
            continue
        if batch in by_batch:
            first, first_sha = by_batch[batch]
            if first_sha == sha:
                status, reason = 'duplicate', 'a copy of %s' % first
            else:
                status, reason = 'rejected', 'batch id of %s, with other bytes' % first
            with data_db:
                data_db.execute('INSERT INTO event_batches VALUES (?, ?, ?, ?, ?, 0, 0, 0, 0, ?, ?, ?)',
                                (key, batch, day, len(data), sha, status, reason, pulled))
            if status == 'duplicate':
                delete.append({'key': key, 'sha256': sha, 'bytes': len(data), 'stored': 'copy of ' + first})
            else:
                keep(out, 'raw/rejected', key, data)
                left.append({'key': key, 'sha256': sha, 'bytes': len(data), 'reason': reason})
            continue
        good, bad, nlines = read_lines(data, app, platform)
        reason = None
        if bad:
            shown = '; '.join('line %d: %s' % b for b in bad[:3])
            reason = '%d bad line%s (%s)' % (len(bad), '' if len(bad) == 1 else 's', shown)
        with data_db:
            added = store(good, events_db, data_db, listed, counts)
            data_db.execute('INSERT INTO event_batches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                            (key, batch, day, len(data), sha, nlines, len(good), added, len(bad),
                             'rejected' if bad else 'stored', reason, pulled))
            events_db.commit()
        by_batch[batch] = (key, sha)
        if bad:
            keep(out, 'raw/rejected', key, data)
            left.append({'key': key, 'sha256': sha, 'bytes': len(data), 'reason': reason})
        else:
            keep(out, 'raw', key, data)
            delete.append({'key': key, 'sha256': sha, 'bytes': len(data), 'stored': 'this run'})
            new += 1
    print('⬇️  %s: %d objects listed, %d new (%d events), %d stored by an earlier run, %d left in the bucket'
          % (bucket, len(objects), new, counts.events, again, len(left)))
    for o in left:
        print('⚠️  left %s: %s' % (o['key'], o['reason']))
    for name in sorted(counts.unlisted):
        print('📋 not in the event list: %s (%d)' % (name, counts.unlisted[name]))
    if len(objects) == MAX_OBJECTS:
        print('⏳ stopped at %d objects: the rest come next run' % MAX_OBJECTS)
    return {'bucket': bucket, 'app': app, 'pulled': pulled, 'events': counts.events,
            'unlisted': counts.unlisted, 'delete': delete, 'left': left}


def ack(env, bucket):
    out = out_dir(env)
    try:
        with open(os.path.join(out, 'manifest.json')) as f:
            manifest = json.load(f)
    except (OSError, ValueError) as e:
        raise Fail('no readable manifest.json in %s: %s' % (out, e))
    if manifest.get('bucket') != bucket:
        raise Fail('manifest.json names bucket %r, not %s' % (manifest.get('bucket'), bucket))
    keys = [o.get('key') if isinstance(o, dict) else None for o in manifest.get('delete', [])]
    bad = [k for k in keys if not isinstance(k, str) or not KEY.match(k)]
    if bad:
        raise Fail('manifest.json lists keys the Worker never writes, so nothing was deleted: %s' % bad[:5])
    r2 = R2(env, bucket)
    gone = 0
    for k in keys:
        if r2.delete(k) != 404:
            gone += 1
    print('🧹 %s: %d of %d objects deleted%s' % (bucket, gone, len(keys),
                                                ', %d already gone' % (len(keys) - gone) if gone < len(keys) else ''))
    return 0


class Usage(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Usage(message)


def main(argv, env=None):
    env = os.environ if env is None else env
    parser = Parser(prog='events_job.py', description='The telemetry runner job for event batches in R2.')
    sub = parser.add_subparsers(dest='cmd')
    p = sub.add_parser('pull')
    p.add_argument('--app', required=True, dest='app_name')
    p.add_argument('--bucket', required=True)
    p.add_argument('--events', default=DEFAULT_EVENTS)
    a = sub.add_parser('ack')
    a.add_argument('--bucket', required=True)
    try:
        args = parser.parse_args(argv[1:])
        if not args.cmd:
            raise Usage('pull or ack')
        if not re.match(r'^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$', args.bucket):
            raise Usage('%r is not an R2 bucket name' % args.bucket)
    except Usage as e:
        sys.stderr.write(__doc__ + '\n🛑 %s\n' % e)
        return 2
    try:
        if args.cmd == 'pull':
            return pull(env, args.app_name, args.bucket, args.events)
        return ack(env, args.bucket)
    except Unreachable as e:
        print('📵 %s' % e, file=sys.stderr)
        return UNREACHABLE
    except (Fail, ValueError, sqlite3.Error, ET.ParseError, OSError) as e:
        print('❌ %s' % e, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
