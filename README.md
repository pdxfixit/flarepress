# flarepress

Deploy static sites and small Workers to Cloudflare Workers (Static Assets), one site folder each,
and check afterwards that the live site is the commit you meant to ship.

- **A GitHub action** for app repos: check the run is for `main` and the files are committed,
  stamp `version.json`, `wrangler deploy`, then verify the live site serves that version.
- **`just` recipes** to scaffold, deploy and check any site folder from your own machine.
- **IDs stay out of public repos:** write `${NAME}` in `wrangler.toml`; it is filled from the
  environment at deploy time, and a missing value stops the deploy.
- **Telemetry harvest:** copy a site's D1 tables and Cloudflare's daily analytics into a local
  SQLite archive, read-only against Cloudflare.
- **Telemetry events:** one wire format for apps' usage events: a browser client, a Worker route that writes
  batches to R2, and the telemetry runner's job that checks and stores them. Apps vendor the files.

Bash and stdlib Python, no packages to install. Wrangler runs through `npx`, pinned to one version.

## What it assumes

flarepress is built for one owner's sites and shared as it is: it does what those sites need,
and may never support yours. MIT licensed ([LICENSE](LICENSE)); fork it freely.

- **The live site is always a commit on `main`.** `check` refuses any other branch, a dirty
  tree and an unpushed commit. Another default branch name is not supported.
- **Config is `wrangler.toml`.** `wrangler.json` and `wrangler.jsonc` are not read.
- **Bash 3.2 and Python 3.8 or newer**, so the tools run on a stock Mac and on GitHub's runners.
  The macOS Keychain is optional: every token can come from the environment instead.
- **Releases are rolling.** `@v1` moves to each new `v1.M.P` release. Pin a full commit SHA if
  you want to choose when you take changes.

## Deploy an app from its own repo

Put a site folder in the repo (here `site/`) with a `wrangler.toml` and a `site.conf`:

```toml
# site/wrangler.toml
name = "example-app"
compatibility_date = "2026-10-01"
routes = [ { pattern = "app.example.com", custom_domain = true } ]

[assets]
directory = "../web"
```

```ini
# site/site.conf (key=value, never run as shell; an unknown key or value is an error)
committed=web
```

Add `version.json` to the app's `.gitignore` (the stamp is written there at deploy time), then
the workflow:

```yaml
# .github/workflows/deploy.yml
on: { push: { branches: [main] } }
permissions: { contents: read }
concurrency: { group: deploy, cancel-in-progress: false }
jobs:
  deploy:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v7.0.1
        with: { fetch-depth: 0 }   # git describe needs the tags
      - uses: pdxfixit/flarepress@v1
        with: { site: site }
        env:
          CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_API_TOKEN }}
          CLOUDFLARE_ACCOUNT_ID: ${{ secrets.CLOUDFLARE_ACCOUNT_ID }}
```

The first deploy creates the Custom Domain, so the action refuses it until you set
`FIRST_PUBLISH: 1` in `env` once. A deploy that went through but fails `verify` exits non-zero
and names each failed check.

**The token:** one per app repo, limited to that zone: Zone → Workers Routes: Edit, DNS: Edit,
Zone: Read, plus Account → Workers Scripts: Edit (Cloudflare cannot narrow that to one Worker),
and D1: Read only for a site with a D1 binding.

## Deploy from your own machine

```sh
just new example.com              # a static site under ~/.config/flarepress/sites/
just alias old.example.com example.com   # a Worker that 301s every path to the target
just plan example.com             # dry run: nothing is sent
just push example.com             # deploy for real
just live example.com             # status, cache headers, and for an app site its version
just secret example.com API_KEY   # set a Worker secret; wrangler asks for the value (or pipe it in)
just link ~/src/app/site          # register an app's site folder, so `just sites` and `just harvest` see it
just sites                        # every site answers as its folder says
```

A `<site>` is a path to a folder with a `wrangler.toml`, or a name under `$FLAREPRESS_SITES`
(default `~/.config/flarepress/sites`). Auth is `CLOUDFLARE_API_TOKEN`, else the macOS Keychain
item `cloudflare-www-deploy`, else wrangler's own login.

## Land commits on main

`tools/land.sh` lands an app's commits on its main branch the same way in every repo: it pulls main from
GitHub first (fast-forward only; it stops when main here has diverged, or when a pull would overwrite
uncommitted edits), cherry-picks the commits in a throwaway worktree, resolves the list files the app names,
runs the app's tests, pushes, then moves main and syncs the main checkout's files. GitHub deploys the push.
With no commits named, it lands the current branch's commits that main lacks. The app runs it from a
flarepress checkout beside its own (or `$FLAREPRESS`, else a clone in `~/.cache/flarepress`), and the tool
pulls that checkout first, so every app lands with the current copy. In the app's `justfile`:

```just
# 🛬 Land commits on main with flarepress's shared tool: just land [--wait] [<sha>…] (no sha: this branch's)
[positional-arguments]
land *args:
    #!/usr/bin/env bash
    set -eu
    fp=${FLAREPRESS:-$(cd "$(git rev-parse --git-common-dir)/../.." && pwd)/flarepress}
    [ -e "$fp/tools/land.sh" ] || fp=${XDG_CACHE_HOME:-$HOME/.cache}/flarepress
    [ -e "$fp/tools/land.sh" ] || git clone -q https://github.com/pdxfixit/flarepress "$fp"
    exec bash "$fp/tools/land.sh" "$@"

# 🛬 Finish a landing that stopped on a conflict, once it is resolved in its worktree
land-continue worktree:
    @just land --continue {{worktree}}
```

And a `land.conf` at the app's root (key=value, never run as shell; every key optional):

```ini
test=just test                    # run in the landing worktree before the push
resolve=TODO.md:todo docs/questions.md:questions   # conflicts these resolvers settle
ci=gate                           # move main only after the CI run on that exact commit passed (needs gh)
branch=main                       # default: GitHub's default branch
after=just live                   # named in the last line, for once the deploy ran
```

## Reference

[AGENTS.md](AGENTS.md) is the full reference: every recipe, every `site.conf` and `harvest.conf`
key, rollback, the telemetry harvest and its token, telemetry events, and the rules for working on this repo.

## Development

`just test` runs every test against local servers and throwaway repos. Nothing reaches
Cloudflare. It needs `just`, `python3`, `curl`, `git` and `node` (for the events tests; skipped with a warning
outside CI when it is missing). CI runs it on Linux and on macOS
(`/bin/bash` 3.2, the system `python3`), with shellcheck. Every change ships with a test that
fails without it.

Security issues: see [SECURITY.md](SECURITY.md).
