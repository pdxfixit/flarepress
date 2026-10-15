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
harvest` and `just sites` see every site.

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
- `just d1 <site> …` / `just kv <site> …` / `just whoami <site>`: wrangler with the same auth.
- `just check|gate|stamp|verify|live|rollback <site>`: the app-site steps (below).
- `just sites [domain…]`: every site under `$FLAREPRESS_SITES` answers as its folder says
  (aliases 301 to their target, move pages 200 at / and 301 elsewhere, sites 200, app sites
  serve `/version.json`). Read-only GETs.
- `just harvest [domain…]`: telemetry (below).
- `just test`: the tools' tests against a local server. Nothing reaches Cloudflare. GitHub
  runs it on every push (`.github/workflows/test.yml`).

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

`site.conf` is `key=value`, read and never run as shell; an unknown key is an error:
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

Run the app's own tests before the action and its live checks after it. **Releases:** tag
`vN.M.P` and move `vN` to it; app repos pin `@vN`. A change that breaks a caller is a new `vN`.

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
| `zone` | zone name | the first route in `wrangler.toml`, cut to its last two labels |
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
## Rules for this repo

- Public: no real domain, person, account ID, zone ID or database ID in any file. Tests use
  `example.com`, `*.test` and `*.invalid`. `tests/test_public.py` checks every tracked file.
- Tools are bash (3.2 safe: macOS `/bin/bash`) or stdlib Python 3.8+. No packages.
- Every change ships with a test that fails without it.
