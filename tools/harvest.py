#!/usr/bin/env python3
"""Copy each site's D1 telemetry and Cloudflare stats into its local SQLite archive.

    harvest.py [domain ...] [--days N] [--db PATH] [--quiet]

With no domain it does every folder that has a harvest.conf. For each site it
copies the D1 tables the config names, then Cloudflare's daily analytics, into
one SQLite file on this Mac that is kept forever. Running it twice adds nothing
new: append tables copy only rows past the archive's highest id, and every
other table is upserted by its key.

Config: <domain>/harvest.conf, key=value lines, read and never run as shell
(like site.conf). `#` comments and blank lines are fine; an unknown key is an
error.
  db=        the archive (~ expands), default ~/.local/state/<domain>/telemetry.sqlite3
  keychain=  the macOS Keychain service holding a read-only Cloudflare API token,
             default cloudflare-analytics-read (HARVEST_TOKEN overrides it)
  account=   the account id (default: from the zone lookup)
  zone=      the zone name (default: the first route in wrangler.toml, cut to
             its last two labels)
  analytics= 0 or 1 (default 1): copy Cloudflare's daily analytics
  table=     one D1 table a line, either
               table=<name> append <idcol>
                 rows with idcol above the archive's highest, 5000 a page
               table=<name> upsert <pk1,pk2..> [min=c,..] [max=c,..] [cols=a,b,..] [as=local]
                 every row D1 has now; on a key conflict min= columns keep the
                 smaller value, max= the larger, every other column the new one.
                 cols= limits what is read (keep secrets out), as= names the
                 local table.

Local tables are made from D1's own columns (PRAGMA table_info) and gain any
column D1 adds later; a local column is never dropped. A table D1 does not have
yet is skipped with a note. harvest_meta keeps the last run and each step's
result.

Read-only against Cloudflare: D1 gets SELECT and PRAGMA table_info only (d1()
refuses anything else), analytics are GraphQL queries. CF_API points every call
at another server (tests). Exit 0 when every step worked; 75 when every failure
was Cloudflare not answering (no connection, a timeout, a 5xx or a 429); 1 for
any other failure. Whatever worked is committed either way. Stdlib only, Python 3.8+.
"""
import argparse
import http.client
import datetime
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

# Site folders by name: a folder each, or a link to a site folder in an app repo.
ROOT = os.path.expanduser(os.environ.get('FLAREPRESS_SITES') or '~/.config/flarepress/sites')
API = 'https://api.cloudflare.com/client/v4'
PAGE = 5000
KEYS = ('db', 'keychain', 'account', 'zone', 'analytics', 'table')
IDENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
ROUTE = re.compile(r'pattern\s*=\s*"([^"]+)"')


class ConfError(Exception):
    pass


UNREACHABLE = 75  # every failure was Cloudflare not answering: the telemetry runner logs it, no alarm


class ApiError(Exception):
    """A Cloudflare call that failed. unreachable: no answer, a 5xx or a 429, so a
    later run may well work; anything else (bad token, bad request) will not."""

    def __init__(self, msg, unreachable=False):
        super().__init__(msg)
        self.unreachable = unreachable


# ── config ──

def ident(name, what):
    if not IDENT.match(name):
        raise ConfError('%s %r is not a plain name (letters, digits, _)' % (what, name))
    return name


def names(value, what):
    return [ident(n, what) for n in value.split(',') if n] if value else []


def parse_table(value):
    """One table= line as a dict: name, mode, keys, min, max, cols, local."""
    words = value.split()
    if len(words) < 3 or words[1] not in ('append', 'upsert'):
        raise ConfError('table=%s: want "<name> append <idcol>" or "<name> upsert <pk,..> [options]"' % value)
    t = {'name': ident(words[0], 'table'), 'mode': words[1], 'min': [], 'max': [], 'cols': [], 'local': words[0]}
    if t['mode'] == 'append':
        if len(words) != 3:
            raise ConfError('table=%s: append takes one id column and no options' % value)
        t['keys'] = [ident(words[2], 'id column')]
        return t
    t['keys'] = names(words[2], 'key column')
    if not t['keys']:
        raise ConfError('table=%s: no key columns' % value)
    for opt in words[3:]:
        k, eq, v = opt.partition('=')
        if not eq or k not in ('min', 'max', 'cols', 'as') or not v:
            raise ConfError('table=%s: unknown option %r (min=, max=, cols=, as=)' % (value, opt))
        if k == 'as':
            t['local'] = ident(v, 'local table')
        else:
            t[k] = names(v, k + ' column')
    if t['cols']:
        missing = [c for c in t['keys'] + t['min'] + t['max'] if c not in t['cols']]
        if missing:
            raise ConfError('table=%s: cols= leaves out %s' % (value, ', '.join(missing)))
    return t


def read_conf(path, domain):
    """harvest.conf as a dict, with defaults filled in. Raises ConfError."""
    conf = {'db': '~/.local/state/%s/telemetry.sqlite3' % domain, 'keychain': 'cloudflare-analytics-read',
            'account': '', 'zone': '', 'analytics': '1', 'tables': []}
    with open(path, encoding='utf-8') as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            key, eq, value = line.partition('=')
            key, value = key.strip(), value.strip()
            if not eq:
                raise ConfError('%s line %d: not key=value: %s' % (path, n, line))
            if key not in KEYS:
                raise ConfError('%s line %d: unknown key %r (keys: %s)' % (path, n, key, ' '.join(KEYS)))
            if key == 'table':
                conf['tables'].append(parse_table(value))
            else:
                conf[key] = value
    if conf['analytics'] not in ('0', '1'):
        raise ConfError('%s: analytics=%s, want 0 or 1' % (path, conf['analytics']))
    locals_ = [t['local'] for t in conf['tables']]
    dup = sorted(set(n for n in locals_ if locals_.count(n) > 1))
    if dup:
        raise ConfError('%s: two table= lines write to %s' % (path, ', '.join(dup)))
    return conf


def wrangler(folder):
    """What harvest needs from wrangler.toml: name, main, database_id, first route.
    Plain regexes, no toml library (Python 3.8)."""
    with open(os.path.join(folder, 'wrangler.toml'), encoding='utf-8') as f:
        text = f.read()
    top = re.split(r'^\s*\[', text, maxsplit=1, flags=re.M)[0]
    name = re.search(r'^\s*name\s*=\s*"([^"]+)"', top, re.M)
    main = re.search(r'^\s*main\s*=\s*"([^"]+)"', top, re.M)
    d1 = re.search(r'^\s*\[\[\s*d1_databases\s*\]\](.*?)(?=^\s*\[|\Z)', text, re.M | re.S)
    dbid = re.search(r'^\s*database_id\s*=\s*"([^"]+)"', d1.group(1), re.M) if d1 else None
    route = ROUTE.search(text)
    dbid = dbid.group(1) if dbid else ''
    # A public app repo writes the id as ${NAME} (tools/fill_ids.py); its value comes from the environment.
    named = re.fullmatch(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}', dbid)
    if named:
        dbid = os.environ.get(named.group(1), '')
    return {'name': name.group(1) if name else '', 'main': main.group(1) if main else '',
            'database_id': dbid, 'route': route.group(1) if route else ''}


def zone_of(pattern):
    """The zone a route pattern lives in: its host's last two labels."""
    host = re.sub(r'^[a-z]+://', '', pattern).split('/')[0].split(':')[0].lstrip('*.')
    return '.'.join(host.split('.')[-2:])


def token_for(service):
    """HARVEST_TOKEN, else the Keychain item. ApiError when there is none."""
    tok = os.environ.get('HARVEST_TOKEN', '').strip()
    if tok:
        return tok
    try:
        tok = subprocess.run(['security', 'find-generic-password', '-s', service, '-w'],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False).stdout.decode().strip()
    except OSError:
        tok = ''
    if not tok:
        raise ApiError('no token in Keychain service %s: security add-generic-password -s %s -a cloudflare -w <TOKEN>'
                       ' (a read-only token; see AGENTS.md, Telemetry harvest), or set HARVEST_TOKEN' % (service, service))
    return tok


# ── Cloudflare (read-only) ──

class Cloudflare:
    def __init__(self, token, api=None):
        self.token = token
        self.api = (api or os.environ.get('CF_API') or API).rstrip('/')

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.api + path, data=data, method=method, headers={
            'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json',
            'User-Agent': 'flarepress-harvest'})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode() or 'null')
        except urllib.error.HTTPError as e:
            with e:
                body = e.read()
            raise ApiError('HTTP %d from %s: %s' % (e.code, path.split('?')[0], messages(body)),
                           unreachable=e.code >= 500 or e.code == 429)
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            # HTTPException covers an answer cut short (IncompleteRead, BadStatusLine).
            raise ApiError('cannot reach %s: %r' % (self.api, getattr(e, 'reason', e)), unreachable=True)
        except ValueError:
            raise ApiError('%s did not answer JSON' % path.split('?')[0])

    def zone(self, name):
        """(zone id, account id) for a zone name."""
        res = self.call('GET', '/zones?' + urllib.parse.urlencode({'name': name}))
        found = (res or {}).get('result') or []
        if not found:
            raise ApiError('zone %s not found (does the token have it?)' % name)
        return found[0]['id'], (found[0].get('account') or {}).get('id', '')

    def d1(self, account, database, sql, params=()):
        """Rows of one read-only statement."""
        if not re.match(r'^\s*(SELECT\b|PRAGMA\s+table_info\b)', sql, re.I):
            raise ValueError('harvest only reads D1: %s' % sql)
        res = self.call('POST', '/accounts/%s/d1/database/%s/query' % (account, database),
                        {'sql': sql, 'params': list(params)})
        if not res or not res.get('success'):
            raise ApiError(messages(res))
        out = res.get('result') or [{}]
        if not out[0].get('success', True):
            raise ApiError(messages(out[0]))
        return out[0].get('results') or []

    def graphql(self, query, variables):
        res = self.call('POST', '/graphql', {'query': query, 'variables': variables})
        if (res or {}).get('errors'):
            raise ApiError(messages(res))
        return (res or {}).get('data') or {}


def messages(body):
    """The error messages in a Cloudflare answer, joined."""
    if isinstance(body, bytes):
        try:
            body = json.loads(body.decode() or 'null')
        except ValueError:
            return body.decode(errors='replace')[:200]
    errs = (body or {}).get('errors') or [] if isinstance(body, dict) else []
    return '; '.join(str(e.get('message', e)) if isinstance(e, dict) else str(e) for e in errs) or 'no details'


# ── the archive ──

def q(name):
    return '"%s"' % name.replace('"', '""')


def local_columns(conn, table):
    return [r[1] for r in conn.execute('PRAGMA table_info(%s)' % q(table))]


def set_meta(conn, key, value):
    conn.execute('INSERT INTO harvest_meta (key, value) VALUES (?, ?) '
                 'ON CONFLICT (key) DO UPDATE SET value = excluded.value', (key, value))


def ensure_table(conn, local, remote, pk, say):
    """Make the local table from D1's columns [(name, type)], or add the ones it lacks."""
    have = local_columns(conn, local)
    if not have:
        cols = ', '.join('%s %s' % (q(n), t) if t else q(n) for n, t in remote)
        conn.execute('CREATE TABLE %s (%s, PRIMARY KEY (%s))' % (q(local), cols, ', '.join(q(c) for c in pk)))
        say('note', '🆕 %s: new local table' % local)
        return
    for n, t in remote:
        if n not in have:
            conn.execute('ALTER TABLE %s ADD COLUMN %s %s' % (q(local), q(n), t))
            say('note', '➕ %s: new column %s' % (local, n))


def copy_table(cf, account, database, conn, t, say):
    """One table= line. Returns the step's result line, or raises ApiError."""
    info = cf.d1(account, database, 'PRAGMA table_info(%s)' % q(t['name']))
    if not info:
        return None
    remote = [(r['name'], r.get('type') or '') for r in info]
    if t['cols']:
        lacking = [c for c in t['cols'] if c not in [n for n, _ in remote]]
        if lacking:
            raise ApiError('D1 table %s has no column %s' % (t['name'], ', '.join(lacking)))
        remote = [(n, ty) for n, ty in remote if n in t['cols']]
    d1_pk = [r['name'] for r in sorted(info, key=lambda r: r.get('pk') or 0) if r.get('pk')]
    pk = (d1_pk or t['keys']) if t['mode'] == 'append' else t['keys']
    missing = [k for k in t['keys'] + t['min'] + t['max'] if k not in [n for n, _ in remote]]
    if missing:
        raise ApiError('D1 table %s has no column %s' % (t['name'], ', '.join(missing)))
    with conn:
        ensure_table(conn, t['local'], remote, pk, say)
    have = local_columns(conn, t['local'])
    cols = [n for n, _ in remote if n in have]
    select = 'SELECT %s FROM %s' % (', '.join(q(c) for c in cols), q(t['name']))
    marks = ', '.join('?' * len(cols))
    if t['mode'] == 'append':
        idcol = t['keys'][0]
        last = conn.execute('SELECT MAX(%s) FROM %s' % (q(idcol), q(t['local']))).fetchone()[0]
        insert = 'INSERT OR IGNORE INTO %s (%s) VALUES (%s)' % (q(t['local']), ', '.join(q(c) for c in cols), marks)
        added = 0
        while True:
            where, params = ('', []) if last is None else (' WHERE %s > ?' % q(idcol), [last])
            page = cf.d1(account, database, '%s%s ORDER BY %s LIMIT %d' % (select, where, q(idcol), PAGE), params)
            with conn:
                added += conn.executemany(insert, [tuple(r.get(c) for c in cols) for r in page]).rowcount
            if len(page) < PAGE:
                break
            last = page[-1][idcol]
        return '📦 %s +%d rows' % (t['local'], added)
    sets = []
    for c in cols:
        if c in t['keys']:
            continue
        if c in t['min'] or c in t['max']:
            fn = 'MIN' if c in t['min'] else 'MAX'
            # A NULL on either side is no value: keep the other one.
            sets.append('%s = %s(COALESCE(%s, excluded.%s), COALESCE(excluded.%s, %s))' % ((q(c), fn) + (q(c),) * 4))
        else:
            sets.append('%s = excluded.%s' % (q(c), q(c)))
    upsert = 'INSERT INTO %s (%s) VALUES (%s) ON CONFLICT (%s) DO %s' % (
        q(t['local']), ', '.join(q(c) for c in cols), marks, ', '.join(q(k) for k in t['keys']),
        'UPDATE SET ' + ', '.join(sets) if sets else 'NOTHING')
    order = ', '.join(q(k) for k in t['keys'])
    rows, offset = [], 0
    while True:
        page = cf.d1(account, database, '%s ORDER BY %s LIMIT %d OFFSET %d' % (select, order, PAGE, offset))
        rows += page
        if len(page) < PAGE:
            break
        offset += PAGE
    with conn:
        conn.executemany(upsert, [tuple(r.get(c) for c in cols) for r in rows])
    src = '' if t['local'] == t['name'] else ' ← %s' % t['name']
    return '📦 %s%s %d rows' % (t['local'], src, len(rows))


# ── Cloudflare analytics: one query a step, so a wrong field breaks only that step ──
# Field names: developers.cloudflare.com/analytics/graphql-api/ (migration-guides/
# zone-analytics, features/nested-structures, tutorials/querying-workers-metrics).

ANALYTICS_SCHEMA = """
CREATE TABLE IF NOT EXISTS cf_http_daily (day TEXT PRIMARY KEY, requests INTEGER, cached_requests INTEGER,
  bytes INTEGER, cached_bytes INTEGER, page_views INTEGER, threats INTEGER, uniques INTEGER);
CREATE TABLE IF NOT EXISTS cf_http_country (day TEXT, country TEXT, requests INTEGER, bytes INTEGER,
  threats INTEGER, PRIMARY KEY (day, country));
CREATE TABLE IF NOT EXISTS cf_http_status (day TEXT, status INTEGER, requests INTEGER, PRIMARY KEY (day, status));
CREATE TABLE IF NOT EXISTS cf_http_browser (day TEXT, browser TEXT, page_views INTEGER, PRIMARY KEY (day, browser));
CREATE TABLE IF NOT EXISTS cf_workers_daily (day TEXT, script TEXT, requests INTEGER, errors INTEGER,
  subrequests INTEGER, cpu_p50 REAL, cpu_p99 REAL, PRIMARY KEY (day, script));
"""

ZONE_QUERY = """query %s($zone: string, $start: Date, $end: Date) {
  viewer { zones(filter: {zoneTag: $zone}) {
    httpRequests1dGroups(limit: 1000, filter: {date_geq: $start, date_leq: $end}, orderBy: [date_ASC]) {
      dimensions { date }
      %s
    }
  } }
}"""


def zone_groups(cf, ids, span, op, fields):
    data = cf.graphql(ZONE_QUERY % (op, fields), {'zone': ids['zone'], 'start': span[0], 'end': span[1]})
    zones = (data.get('viewer') or {}).get('zones') or []
    return (zones[0].get('httpRequests1dGroups') or []) if zones else []


def http_daily(cf, ids, span, site):
    out = []
    for g in zone_groups(cf, ids, span, 'HarvestHttpDaily',
                         'sum { requests cachedRequests bytes cachedBytes pageViews threats } uniq { uniques }'):
        s, u = g.get('sum') or {}, g.get('uniq') or {}
        out.append((g['dimensions']['date'], s.get('requests'), s.get('cachedRequests'), s.get('bytes'),
                    s.get('cachedBytes'), s.get('pageViews'), s.get('threats'), u.get('uniques')))
    return out


def http_country(cf, ids, span, site):
    return [(g['dimensions']['date'], m.get('clientCountryName'), m.get('requests'), m.get('bytes'), m.get('threats'))
            for g in zone_groups(cf, ids, span, 'HarvestHttpCountry',
                                 'sum { countryMap { clientCountryName requests bytes threats } }')
            for m in (g.get('sum') or {}).get('countryMap') or []]


def http_status(cf, ids, span, site):
    return [(g['dimensions']['date'], m.get('edgeResponseStatus'), m.get('requests'))
            for g in zone_groups(cf, ids, span, 'HarvestHttpStatus',
                                 'sum { responseStatusMap { edgeResponseStatus requests } }')
            for m in (g.get('sum') or {}).get('responseStatusMap') or []]


def http_browser(cf, ids, span, site):
    return [(g['dimensions']['date'], m.get('uaBrowserFamily'), m.get('pageViews'))
            for g in zone_groups(cf, ids, span, 'HarvestHttpBrowser',
                                 'sum { browserMap { uaBrowserFamily pageViews } }')
            for m in (g.get('sum') or {}).get('browserMap') or []]


WORKERS_QUERY = """query HarvestWorkersDaily($account: string, $script: string, $start: Time, $end: Time) {
  viewer { accounts(filter: {accountTag: $account}) {
    workersInvocationsAdaptive(limit: 1000, filter: {scriptName: $script, datetime_geq: $start, datetime_leq: $end}) {
      dimensions { date }
      sum { requests errors subrequests }
      quantiles { cpuTimeP50 cpuTimeP99 }
    }
  } }
}"""


def workers_daily(cf, ids, span, site):
    # The docs show datetime_geq/datetime_leq on this node, so the day span is
    # sent as times: midnight UTC of the first day to the end of the last.
    data = cf.graphql(WORKERS_QUERY, {'account': ids['account'], 'script': site['name'],
                                      'start': span[0] + 'T00:00:00Z', 'end': span[1] + 'T23:59:59Z'})
    accounts = (data.get('viewer') or {}).get('accounts') or []
    groups = (accounts[0].get('workersInvocationsAdaptive') or []) if accounts else []
    out = []
    for g in groups:
        s, qs = g.get('sum') or {}, g.get('quantiles') or {}
        out.append((g['dimensions']['date'], site['name'], s.get('requests'), s.get('errors'),
                    s.get('subrequests'), qs.get('cpuTimeP50'), qs.get('cpuTimeP99')))
    return out


# (table, fetch, key columns, what to call the rows, the permission it needs, emoji)
STEPS = (
    ('cf_http_daily', http_daily, 1, 'days', 'Zone → Analytics: Read', '🌐'),
    ('cf_http_country', http_country, 2, 'rows', 'Zone → Analytics: Read', '🌐'),
    ('cf_http_status', http_status, 2, 'rows', 'Zone → Analytics: Read', '🌐'),
    ('cf_http_browser', http_browser, 2, 'rows', 'Zone → Analytics: Read', '🌐'),
    ('cf_workers_daily', workers_daily, 2, 'days', 'Account → Account Analytics: Read', '⚙️'),
)
COLUMNS = {
    'cf_http_daily': ('day', 'requests', 'cached_requests', 'bytes', 'cached_bytes', 'page_views', 'threats', 'uniques'),
    'cf_http_country': ('day', 'country', 'requests', 'bytes', 'threats'),
    'cf_http_status': ('day', 'status', 'requests'),
    'cf_http_browser': ('day', 'browser', 'page_views'),
    'cf_workers_daily': ('day', 'script', 'requests', 'errors', 'subrequests', 'cpu_p50', 'cpu_p99'),
}


def store_rows(conn, table, nkeys, rows):
    cols = COLUMNS[table]
    sql = 'INSERT INTO %s (%s) VALUES (%s) ON CONFLICT (%s) DO UPDATE SET %s' % (
        table, ', '.join(cols), ', '.join('?' * len(cols)), ', '.join(cols[:nkeys]),
        ', '.join('%s = excluded.%s' % (c, c) for c in cols[nkeys:]))
    with conn:
        conn.executemany(sql, rows)


def permission_note(msg, perm):
    if re.search(r'authoriz|permission|access', msg, re.I):
        return 'token lacks %s (%s)' % (perm, msg)
    return msg


# ── one site ──

def harvest_site(domain, root, days, db_path, say, today=None):
    """Every step for one site. Returns (steps that worked, steps that failed,
    how many of those failed only because Cloudflare could not be reached)."""
    folder = os.path.join(root, domain)
    okn = bad = 0
    down = [0]

    def fail(step, msg, error=None):
        if getattr(error, 'unreachable', False):
            down[0] += 1
        say('problem', '⚠️ %s: %s' % (step, msg))
        return 1
    try:
        conf = read_conf(os.path.join(folder, 'harvest.conf'), domain)
        site = wrangler(folder)
    except (ConfError, OSError) as e:
        return 0, fail('config', str(e)), 0
    path = os.path.expanduser(db_path or conf['db'])
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute('CREATE TABLE IF NOT EXISTS harvest_meta (key TEXT PRIMARY KEY, value TEXT)')
        results = {}
        say('ok', '📥 %s → %s' % (domain, path.replace(os.path.expanduser('~'), '~', 1)))

        def record(step, result):
            results[step] = result

        try:
            cf = Cloudflare(token_for(conf['keychain']))
            ids = {'account': conf['account'], 'zone': ''}
            need_zone = conf['analytics'] == '1' or (conf['tables'] and not ids['account'])
            if need_zone:
                zone = conf['zone'] or (zone_of(site['route']) if site['route'] else '')
                if not zone:
                    raise ApiError('no zone= and no route in wrangler.toml to take it from')
                ids['zone'], acct = cf.zone(zone)
                ids['account'] = ids['account'] or acct
        except ApiError as e:
            bad += fail('cloudflare', str(e), e)
            record('cloudflare', 'error: %s' % e)
            conf['tables'], conf['analytics'] = [], '0'
        if conf['tables'] and not site['database_id']:
            bad += fail('d1', 'harvest.conf names tables but wrangler.toml has no [[d1_databases]] database_id (or its ${NAME} is not set)')
            record('d1', 'error: no database_id')
            conf['tables'] = []
        for t in conf['tables']:
            step = 'table:' + t['local']
            try:
                line = copy_table(cf, ids['account'], site['database_id'], conn, t, say)
                if line is None:
                    say('note', '⏭️ %s: not in D1 yet, skipped' % t['name'])
                    record(step, 'skipped: not in D1')
                else:
                    say('ok', line)
                    record(step, 'ok: ' + line.split(' ', 1)[1])
                okn += 1
            except ApiError as e:
                if 'no such table' in str(e):
                    say('note', '⏭️ %s: not in D1 yet, skipped' % t['name'])
                    record(step, 'skipped: not in D1')
                    okn += 1
                else:
                    bad += fail(t['local'], str(e), e)
                    record(step, 'error: %s' % e)
            except sqlite3.Error as e:
                bad += fail(t['local'], 'archive: %s' % e)
                record(step, 'error: archive: %s' % e)
        if conf['analytics'] == '1':
            conn.executescript(ANALYTICS_SCHEMA)
            today = today or datetime.datetime.now(datetime.timezone.utc).date()
            span = ((today - datetime.timedelta(days=days - 1)).isoformat(), today.isoformat())
            for table, fetch, nkeys, unit, perm, icon in STEPS:
                if table == 'cf_workers_daily' and not site['main']:
                    continue  # static assets only: no Worker to count
                try:
                    rows = fetch(cf, ids, span, site)
                    store_rows(conn, table, nkeys, rows)
                    n = len(set(r[0] for r in rows)) if unit == 'days' else len(rows)
                    say('ok', '%s %s %d %s' % (icon, table, n, unit))
                    record('analytics:' + table, 'ok: %d %s' % (n, unit))
                    okn += 1
                except ApiError as e:
                    bad += fail(table, permission_note(str(e), perm), e)
                    record('analytics:' + table, 'error: %s' % e)
                except (KeyError, TypeError, sqlite3.Error) as e:
                    bad += fail(table, 'unexpected answer: %r' % e)
                    record('analytics:' + table, 'error: %r' % e)
        with conn:
            set_meta(conn, 'last_run', datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
            set_meta(conn, 'last_result', 'ok' if not bad else '%d failed' % bad)
            for step, result in results.items():
                set_meta(conn, 'step:' + step, result)
    finally:
        conn.close()
    return okn, bad, down[0]


def main(argv=None, root=ROOT, today=None):
    p = argparse.ArgumentParser(prog='harvest.py', description=__doc__.split('\n')[0])
    p.add_argument('domains', nargs='*', help='site folders (default: every folder with a harvest.conf)')
    p.add_argument('--days', type=int, default=30, help='days of Cloudflare analytics to (re)copy, default 30')
    p.add_argument('--db', help='archive path for this run, instead of the config\'s db=')
    p.add_argument('--quiet', action='store_true', help='print only problems and a one-line summary')
    a = p.parse_args(argv)
    if a.days < 1:
        p.error('--days must be 1 or more')
    domains = a.domains or sorted(d for d in os.listdir(root) if os.path.isfile(os.path.join(root, d, 'harvest.conf')))
    if not domains:
        print('🛑 no folder has a harvest.conf')
        return 1
    if a.db and len(domains) != 1:
        p.error('--db takes exactly one domain: two sites in one file would share cf_http_daily rows')

    prefix = {'': ''}

    def say(level, text):
        # Quiet prints problems only, each naming its site.
        if not a.quiet or level == 'problem':
            print(prefix[''] + text)
    okn = bad = down = 0
    for d in domains:
        prefix[''] = ('%s: ' % d) if a.quiet else ''
        if not os.path.isfile(os.path.join(root, d, 'harvest.conf')):
            say('problem', '⚠️ no %s/harvest.conf' % d)
            bad += 1
            continue
        o, b, u = harvest_site(d, root, a.days, a.db, say, today=today)
        okn, bad, down = okn + o, bad + b, down + u
    unreachable = bad and down == bad
    print('%s harvest: %d site%s, %d step%s worked, %d failed%s' % (
        '✅' if not bad else '📵' if unreachable else '❌', len(domains), '' if len(domains) == 1 else 's',
        okn, '' if okn == 1 else 's', bad, ' (Cloudflare unreachable)' if unreachable else ''))
    return 0 if not bad else UNREACHABLE if unreachable else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
