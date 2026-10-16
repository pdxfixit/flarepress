# Security

## Reporting a problem

Report it privately: on GitHub, **Security → Report a vulnerability** on this repository.
Please do not open a public issue for it.

Only the latest `v1` release is supported; a fix ships as the next `v1.M.P`, and `v1` moves to it.

## What flarepress touches

- **Deploys** (`just push`, the action's Deploy step, `rollback --yes`) run `wrangler deploy` with
  the token you give it. `just d1`, `just kv` and `just whoami` hand their arguments to wrangler
  as they are, so they can write whatever wrangler can.
- **Every other step** (`check`, `gate`, `stamp`, `verify`, `live`, `just sites`) changes nothing
  in Cloudflare: git, plain HTTPS GETs to your own site, and `stamp` writes one local file.
- **The telemetry harvest** sends D1 one read-only statement at a time (`SELECT`, `PRAGMA
  table_info`; anything else, or a second statement, is refused before it is sent) and GraphQL
  analytics queries. Give it a token with only Account → D1: Read, Zone → Analytics: Read and
  Account → Account Analytics: Read.
- **IDs written as `${NAME}`** in `wrangler.toml` are filled from the environment into
  `.wrangler.deploy.toml` beside it for the length of one wrangler run, then removed. Add that
  file to your app's `.gitignore`.
- **Config files** (`site.conf`, `harvest.conf`) are parsed, never run as shell.

## Supply chain

- Wrangler runs through `npx`, pinned to one exact version in `tools/wrangler.sh`.
- The workflows pin each action by commit, and check the `just` download against its release's
  published SHA-256.
- `@v1` is a rolling tag. To review each change before you run it, pin
  `pdxfixit/flarepress@<full commit SHA>` instead.
