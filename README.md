# flarepress

Deploy static sites and small Workers to Cloudflare Workers, one site folder each.

- **A GitHub action** for app repos: check, stamp `version.json`, `wrangler deploy`, verify the live site serves the commit.
- **`just` recipes** for deploying and checking any site folder from a Mac.
- **IDs stay out of public repos:** write `${NAME}` in `wrangler.toml`; it is filled from the environment at deploy time.
- **Telemetry harvest:** copy a site's D1 tables and Cloudflare analytics into a local SQLite archive.

Bash and stdlib Python, no packages. See [AGENTS.md](AGENTS.md) for how it fits together, and run `just test` (needs `just`, `python3`, `curl`, `git`).
