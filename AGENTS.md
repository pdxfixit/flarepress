# flarepress

Deploys static sites and small Workers to Cloudflare Workers (Static Assets). This repo
is the **code only**: the deploy steps, a GitHub action, and the telemetry harvest.
Each site's config lives with the site. **This repo is public:** no domain, person,
account or database ID belongs in it (`tests/test_public.py` fails on them).

## Where sites live

A **site folder** is a folder with a `wrangler.toml`. Two kinds:

| Kind | Where | Deployed by |
|---|---|---|
| **App site**: an app with its own repo | a folder in that repo (`site/`), with `site.conf` and maybe `harvest.conf` | the repo's GitHub workflow, on every push to `main` (`uses: pdxfixit/flarepress@v1`); the owner's Mac as a fallback |
| **Plain site**: an alias, a move page, a static page | `~/.config/flarepress/sites/<domain>/` on the owner's Mac (`$FLAREPRESS_SITES`) | the owner, from the Mac: `just push <domain>` |

Every recipe and tool takes `<site>` as a **path** to a site folder, or a **name** under
`$FLAREPRESS_SITES`. A name there may be a link to an app's `site/` folder, so `just
harvest` and `just sites` see every site: `just link <path to the app's site/>` makes it.

An app's `wrangler.toml` points at its files relatively (`[assets] directory = "../web"`,
`main = "../worker/index.js"`). Paths resolve from the folder's real location, as wrangler
resolves them, even when it is reached through a link.

**IDs a public repo must not hold** are written as `${NAME}` in `wrangler.toml`, for
example `database_id = "${D1_DATABASE_ID}"`. `tools/wrangler.sh` fills each from the
environment into `.wrangler.deploy.toml` beside it (removed on exit) and hands wrangler
that; a name with no value stops the run first. On GitHub the values are repo secrets; on
a Mac the app's recipes export them from its config file.

## Commands (`just`, from anywhere)

- `just plan <site>`: dry-run deploy, nothing sent.
- `just push <site>`: deploy for real (assets, Worker, the Custom Domains in `wrangler.toml`).
- `just new <domain>` / `just alias <domain> <target>`: scaffold a plain site under `$FLAREPRESS_SITES`
  (`new`: `index.html`, `_headers` with no-cache, nosniff, a referrer policy and a strict CSP,
  `.assetsignore`; `alias`: a Worker that 301s every path to `<target>`).
- `just link <site folder>…` / `just unlink <domain>`: register an app's `site/` folder under
  `$FLAREPRESS_SITES`, as a link named for the site's first route (a relative path is read from where you run
  just). Safe to run again: a link already right is left, a link to somewhere else is re-pointed, and a real
  folder is never touched or removed. A path that is not a site folder (no `wrangler.toml`, or no route with a
  domain name) is refused, the others are still linked, and the exit is 1.
- `just d1 <site> …` / `just kv <site> …` / `just whoami <site>`: wrangler with the same auth.
- `just secret <site> NAME [flags]`: `wrangler secret put NAME` with the same auth (and `${NAME}` ids filled), so a
  Worker secret is set like the site's D1. The value comes on stdin, typed at wrangler's prompt or piped
  (`printf '%s' "$KEY" | just secret <site> NAME`), never as an argument; a NAME that is not letters, digits and `_` is
  refused before wrangler starts. Flags such as `--env production` reach wrangler.
- `just check|gate|stamp|verify|live|rollback <site>`: the app-site steps (below).
- `just sites [domain…]`: every site under `$FLAREPRESS_SITES` answers as its folder says
  (aliases 301 to their target, move pages 200 at / and 301 elsewhere, sites 200, app sites
  serve `/version.json`). Read-only GETs.
- `just harvest [domain…]`: telemetry (below).
- `just events-vendor --client DIR --worker DIR | --standalone DIR [--check]`: copy the telemetry events files into
  an app, or check its copies ("Telemetry events", below).
- `just release [minor]`: start the release workflow (the tests, then the next `v1.M.P` tag and `v1` moved to it;
  `minor` for something new a caller can use, the default `patch` for fixes).
- `just land [--wait] [<sha>…]` / `just land-continue <worktree>`: land commits on a repo's main, from that repo
  or a worktree of it ("Landing", below). Apps call `tools/land.sh` from their own `just land`.
- `just test`: the tools' tests against a local server. Nothing reaches Cloudflare. GitHub
  runs it on every branch push and pull request, on Linux and on macOS (`/bin/bash` 3.2, the
  system `python3`), with shellcheck (`.github/workflows/test.yml`).

## Auth

`tools/wrangler.sh`, in order: `CLOUDFLARE_API_TOKEN` when set (GitHub passes the repo's
secret), else the macOS Keychain item `$CLOUDFLARE_KEYCHAIN` (default
`cloudflare-www-deploy`), else wrangler's own login. Give each app repo its **own** token,
limited to its zone (Workers Routes: Edit, DNS: Edit, Zone: Read) plus Account → Workers
Scripts: Edit, and D1: Read only for a site with a D1 binding. Workers Scripts: Edit is
account-wide in Cloudflare; it cannot be narrowed to one Worker.

A cloud agent's network secret does not work for deploys: an agent proxy that sets
`Authorization` on every request to `api.cloudflare.com` also replaces the upload key
wrangler sends for changed files, and the upload fails with 401.

## App sites

A site folder with a `site.conf`. `tools/site.sh <site> <step>`; the GitHub action runs
check, gate, stamp, deploy and verify in that order.

| Step | Does | Changes the site? |
|---|---|---|
| `check [--from DIR]` | on GitHub: the run is for `refs/heads/main` and HEAD is the pushed commit; elsewhere: the main checkout, on `main`, pushed. Both: committed paths clean, `index.html` (and a PWA's manifest and service worker) present | no |
| `gate` | refuses a first publish (no answer at all) unless `FIRST_PUBLISH=1` | no |
| `stamp` | writes `version.json` into the assets folder: `git describe`, `-dirty` when a committed path has edits | no (gitignored in the app) |
| `verify [--want V] [--web DIR]` | the stamped version is live, `compare` files match byte for byte, 200s, a PWA's worker headers | no |
| `live` | status, cache headers, the live version and whether it is main's | no |
| `rollback [--to C] [--yes]` | dry by default; `rollback=redeploy` deploys a throwaway checkout of an old commit from its own site folder (a commit from before the site folder was in the repo cannot be redeployed); `rollback=revert` only prints the revert steps | only with `--yes` |

`site.conf` is `key=value` (no spaces around `=`), read and never run as shell; an unknown key
or a value it does not take (`pwa` 0 or 1, `rollback` redeploy or revert, waits in whole seconds) is an error:
`url`, `committed` (app paths that ship; default the assets folder), `pwa`, `compare`,
`compare_wait`, `version_wait`, `rollback`. `tools/site.sh` names each in its header.

## Using the action

```yaml
on: { push: { branches: [main] } }
permissions: { contents: read }
concurrency: { group: deploy, cancel-in-progress: false }
jobs:
  deploy:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v7.0.1
        with: { fetch-depth: 0 }
      - uses: pdxfixit/flarepress@v1
        with: { site: site }
        env:
          CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_API_TOKEN }}
          CLOUDFLARE_ACCOUNT_ID: ${{ secrets.CLOUDFLARE_ACCOUNT_ID }}
```

Run the app's own tests before the action and its live checks after it. **Releases are
rolling tags** (the owner, 2026-10-15): app repos use `@v1` (and check out `ref: v1`), so
moving `v1` releases to every site's next deploy, with no commit in the app repos. Moving
the tag is the release step. **Release with the release workflow** (`.github/workflows/release.yml`):
Actions → release → Run workflow, `just release [minor]`, or the GitHub API (an agent may start it). It
runs the tests on Linux and macOS without write access, then, in a job that can write and runs no
tests, `tools/release.sh --push`: tags `main` as the next `v1.M.P` (`bump`: patch, or minor; the commits
since the last release in its message) and moves `v1` to it, annotated and on the commit, with
the repo's own token. `main` already released: it says so and tags nothing. A change that breaks a caller is a new major tag, `v2`, which each app moves to on purpose.

## Telemetry harvest

`just harvest [domain...]` (`tools/harvest.py`) copies each site's D1 tables and
Cloudflare's daily analytics into one SQLite archive on the owner's machine, kept forever. With no
domain it does every folder with a `harvest.conf`. A second run adds nothing new; the
last `--days` (default 30) of analytics are re-copied, so partial days fill in. It
prints one line per step and exits 1 when any step failed; what worked is kept. When
every failure was Cloudflare not answering (no connection, a timeout, a 5xx or a 429), it
exits 75 instead, which the telemetry runner (pdxfixit/telemetry) logs without alarming.

**From the telemetry runner:** the runner calls `python3 tools/harvest.py <domain> --db
<file>` with `HARVEST_TOKEN` set, in a fresh cloud container. The file is seeded with the
archive from the runner's last stored run, so harvest's append and upsert rules work as they
do on a Mac. Every table harvest makes has a primary key, is a rowid table and has plain
identifier names, as the runner requires.

`harvest.conf` is `key=value`, read and never run as shell; an unknown key is an error.

| Key | Means | Default |
|---|---|---|
| `db` | the archive (`~` expands) | `~/.local/state/<domain>/telemetry.sqlite3` |
| `keychain` | Keychain service holding the read-only token (`HARVEST_TOKEN` overrides) | `cloudflare-analytics-read` |
| `account` | account id | from the zone lookup |
| `zone` | zone name | the first route's host in `wrangler.toml`, or its nearest parent the token can see (`app.example.com`, then `example.com`) |
| `analytics` | `1` copies Cloudflare's daily stats into `cf_http_daily`, `cf_http_country`, `cf_http_status`, `cf_http_browser` and, for a site with a Worker, `cf_workers_daily` | `1` |
| `table` | one D1 table a line: `<name> append <idcol>` (new rows by id), or `<name> upsert <pk,..> [min=c,..] [max=c,..] [cols=a,b,..] [as=local]` (every row; `cols=` keeps secrets out) | none |

Local tables are made from D1's own columns and gain any column D1 adds; none is ever
dropped. A table D1 lacks yet is skipped with a note. `harvest_meta` holds the last run
and each step's result.

**The token** (Cloudflare → API Tokens, the one account only):
Account → D1: Read, Zone → Analytics: Read, Account → Account Analytics: Read. Store it with
`security add-generic-password -s cloudflare-analytics-read -a cloudflare -w <TOKEN>`
(a site's `harvest.conf` can name its own service).

**When it runs:** when the owner runs it, or an app repo's recipe calls it,
and daily from the owner's routine. **Read-only against Cloudflare:** it sends D1
only `SELECT` and `PRAGMA table_info` (it refuses anything else) and GraphQL queries;
it never writes to D1 or changes anything in Cloudflare.
## Telemetry events

One shared wire format for apps' usage events, so every app sends and stores them the same way (the telemetry
repo's `docs/standard.md` §3 and §6). Three parts, one per place, all in this repo so they cannot drift:

| Part | File | Runs in |
|---|---|---|
| Client | `events/events-client.js` | the app's page: `track(event, details)`, queued and batched |
| Worker | `events/events-worker.js` | the app's own Worker: `handleEvents(request, env, opts)` for `POST /api/events` |
| Standalone Worker | `events/events-standalone.js` | a Worker of its own, for an app with no site (a watch app) |
| Job | `tools/events_job.py` | the telemetry runner: R2 to `events.sqlite3` and the daily tables |

**The wire.** `POST /api/events` on the app's own origin, `Content-Type: text/plain`, one JSON record per line
(the standard's twelve columns, `details` as an object), at most 64 KB. The batch id, a random UUID, is the
`X-Batch` header, or `?batch=<uuid>` in the URL: `navigator.sendBeacon` cannot set headers. A retry reuses it.
The Worker checks method, origin (or key), type, size and id, then writes the body unread to
`v1/<UTC day>/<batch>.jsonl` in R2 and answers 204. It never parses the body, never reads the IP address and logs
nothing. 403 origin or key, 405 method, 413 size, 415 type, 400 id or empty body, 503 R2.

**Vendoring.** An app copies the files in; it never imports them from here. From a flarepress checkout, with
paths from where you run it:

```sh
just events-vendor --client ../app/web --worker ../app/worker          # a web app
just events-vendor --standalone ../app/telemetry                       # an app with no site
just events-vendor --client ../app/web --worker ../app/worker --check  # exit 1 if a copy is missing, edited or behind
```

Each copy starts with three comment lines naming its source and the sha256 of the original, so an edit inside the
app shows up (its body no longer matches its header). Change a file here, release, and vendor it again.

**Wire it in an app's Worker** (the `/api/events` route goes before the assets):

```js
import { handleEvents } from "./events-worker.js";
if (url.pathname === "/api/events") {
  return handleEvents(request, env, { bucket: "EVENTS", origins: ["https://app.example.com"] });
}
```

`opts`: `bucket` (the R2 binding's name), `origins` (exact `Origin` values), `key` (`{ header, secret }`: the
header must equal the Worker secret of that name, for a native app, which sends no `Origin`). A site whose
`Referrer-Policy` is `no-referrer` sends `Origin: null` and is refused: keep `strict-origin-when-cross-origin`.

**The R2 binding** in the app's `site/wrangler.toml` (the bucket is made once, by the owner):

```toml
[[r2_buckets]]
binding = "EVENTS"
bucket_name = "example-app-events"
```

**The client**, loaded as a module by the page (CSP `connect-src 'self'` covers it):

```js
import { init, track, clear } from "./events-client.js";
init({ app: "example", version: VERSION, player: () => playerId, screen: () => screenName,
       enabled: () => settings.telemetry });
track("control.use", { control: "pencil", group: "tools", layout: "rail", vw: innerWidth, vh: innerHeight, input: "touch" });
// the switch's handler, when it is turned off: clear();
```

`init` options: `app`, `version`, `player` (the app's plain random id, a string or a function; the client never
makes or hashes one), `screen` (a function), `enabled` (a function, read on every call), and optionally
`endpoint` (default `/api/events`), `interval` (300000 ms, 5 minutes), `maxRecords` (5000), `form` (default from the shortest
screen side and a coarse pointer: `phone` under 600 px, `tablet`, or `desktop`). It fills `platform` `web`,
`session` (random per page load), `seq`, `at` (UTC ms) and `tz` (minutes east of UTC). Records queue in memory and
`localStorage`; batches go every 5 minutes while shown, and by beacon when the page is hidden or on `pagehide`. A batch
is dropped only after a 2xx. A page that closed with records queued is sent by the app's next page load. While
`enabled()` is false nothing is recorded or sent, and queued records are cleared. Register the app's own
`visibilitychange` listener before `init`, so its `app.background` record rides the beacon.

**A native app** posts the same batches to the standalone Worker with an `X-Events-Key` header equal to its
secret `EVENTS_KEY` (`just secret <site> EVENTS_KEY`); `EVENTS_ORIGINS` (a var, comma-separated) may also allow
browser origins. Its site folder's `wrangler.toml` has `main = "../telemetry/events-standalone.js"` and the
`EVENTS` binding.

**The runner job** (`telemetry.toml` in the telemetry repo), one per app and bucket:

```toml
[jobs.example-events]
repo = "flarepress"
run = ["python3", "tools/events_job.py", "pull", "--app", "example", "--bucket", "example-app-events",
       "--events", "{src}/telemetry/events.toml"]
ack = ["python3", "tools/events_job.py", "ack", "--bucket", "example-app-events"]
secrets = { R2_ACCESS_KEY_ID = "...", R2_SECRET_ACCESS_KEY = "...", R2_ACCOUNT_ID = "..." }
d1 = ["daily_events", "daily_players"]
[jobs.example-events.ids]
day_players = { columns = ["player"], time = "day", unit = "iso", drop = true }
```

`pull` lists `v1/`, reads at most 1000 objects a run, and checks each line: exactly the twelve columns, the right
types, `app` equal to `--app`, `platform` equal to the app's in `events.toml`. It writes this run's new events to
`$TELEMETRY_OUT/events.sqlite3` (table `events`, key `app, session, seq`), adds counts to `daily_events` (keyed by
day, app, platform, form, version, event, and `key`: a sorted JSON object of the event's `summary` keys from
`events.toml`, null when absent), adds to `day_players` and recounts `daily_players`, all in `data.sqlite3`,
keeps each object under `raw/` and writes `manifest.json`. `event_batches` in `data.sqlite3` has one row per
object, so a batch id is counted once, however many times or days it arrived. An object with a bad line keeps
its good lines, goes under `raw/rejected/` and stays in the bucket for good. An event or details key that
`events.toml` lacks is stored, counted and named in the output. `ack` deletes exactly the manifest's `delete`
keys. Exit 0, 75 when R2 does not answer, 1 on any other failure. `--events` defaults to
`../telemetry/events.toml` beside this repo. The R2 token: Object Read & Write, that bucket only.

## Landing

`tools/land.sh` is every app's `just land` (README: "Land commits on main" has the app's recipe and
`land.conf`). In order: it stops when the main checkout is flagged `core.bare=true`, printing the `git config
core.bare false` that restores it; it pulls its own flarepress checkout when that is clean and on main, then starts
again on the new copy; fetches the branch and fast-forwards it here, the main checkout's files too (stops when
diverged, or when the pull would overwrite uncommitted edits; offline it goes on); refuses commits touching a
file the main checkout holds uncommitted edits in (`--wait` waits up to `LAND_WAIT_SECONDS`, default 3600);
cherry-picks them in `.worktrees/land-*` (git's `info/exclude` ignores it), settling conflicts in the files
`resolve=` names (a path or glob, first match wins) with `tools/resolve_todo.py`, `tools/resolve_questions.py`
or `tools/resolve_words.py` (long lines word by word), and stopping on any other conflict, or a clash on the
same words, with the worktree kept for `--continue`; runs `after_pick=` after each pick and amends what it changed into that commit; pulls again and rebases the picked commits when the branch moved meanwhile;
runs `test=`; with `ci=gate` pushes `land/<sha>` and waits for the `ci_workflow` run (`LAND_TIMEOUT`, default
900 s); pushes; only then moves the branch here and syncs the touched files (deletions too). A refused push
leaves the main checkout as it was. `tests/test_land.py` covers each step in throwaway repos.

Landing this repo: `just land` from a worktree (its `land.conf` runs `just test`).

## Rules for this repo

- Public: no real domain, person, account ID, zone ID or database ID in any file. Tests use
  `example.com`, `*.test` and `*.invalid`. `tests/test_public.py` checks every tracked file.
- Tools are bash (3.2 safe: macOS `/bin/bash`) or stdlib Python 3.8+. No packages. The events files are plain ES
  modules with no packages, tested with Node's own runner (`node --test`).
- Every change ships with a test that fails without it.
