# flarepress: deploy static sites and small Workers to Cloudflare, one site
# folder each (wrangler.toml, and for an app site site.conf and harvest.conf).
# An app's site folder lives in the app's repo; other sites (aliases, move
# pages) live under $FLAREPRESS_SITES (default ~/.config/flarepress/sites).
# Every <site> below is a name there or a path, read from where you run just.
#
# Auth (tools/wrangler.sh): CLOUDFLARE_API_TOKEN when set, else the macOS
# Keychain item $CLOUDFLARE_KEYCHAIN (default cloudflare-www-deploy), else
# wrangler's own login. Add the Keychain item once:
#   security add-generic-password -s cloudflare-www-deploy -a cloudflare -w <API_TOKEN>

set shell := ["bash", "-uc"]

sites := env("FLAREPRESS_SITES", env("HOME") / ".config/flarepress/sites")
tools := justfile_directory() / "tools"
here := invocation_directory()

# The recipes, then the site folders under $FLAREPRESS_SITES
default:
    @just --list
    @echo
    @ls -d "{{sites}}"/*/ 2>/dev/null | sed 's#/$##; s#.*/##' || echo "(no site folders in {{sites}})"

# Preview a deploy without publishing anything (extra flags go to wrangler deploy --dry-run)
[positional-arguments]
plan site *flags:
    @cd "{{here}}" && "{{tools}}/wrangler.sh" "$1" deploy --dry-run "${@:2}"

# Publish for real: assets, Worker, and the Custom Domains in wrangler.toml (extra flags go to wrangler deploy)
[positional-arguments]
push site *flags:
    @cd "{{here}}" && "{{tools}}/wrangler.sh" "$1" deploy "${@:2}"

# Scaffold a new site folder under $FLAREPRESS_SITES: just new example.com
[positional-arguments]
new domain:
    #!/usr/bin/env bash
    set -euo pipefail
    domain=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')
    printf '%s\n' "$domain" | grep -Eq '^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z0-9-]*[a-z]$' \
      || { echo "🛑 '$1' is not a domain name (example.com, not https://example.com/)" >&2; exit 1; }
    d="{{sites}}/$domain"
    [ ! -e "$d" ] || { echo "🛑 $d already exists; nothing changed" >&2; exit 1; }
    mkdir -p "$d/public"
    cat > "$d/wrangler.toml" <<EOF
    name = "$(echo "$domain" | tr '.' '-')"
    compatibility_date = "$(date +%Y-%m-%d)"

    routes = [
      { pattern = "$domain", custom_domain = true },
      { pattern = "www.$domain", custom_domain = true }
    ]

    [assets]
    directory = "./public"
    EOF
    echo "<!DOCTYPE html><html><head><title>$domain</title></head><body></body></html>" > "$d/public/index.html"
    # Response headers (Workers Static Assets reads _headers; neither file is served).
    cat > "$d/public/_headers" <<'EOF'
    # Response headers for Cloudflare Workers Static Assets (this file is not served).
    # no-cache: browsers check for a new copy on every load, so a deploy shows up at once.
    # The CSP allows only this site's own files and data: images. When the site
    # loads something from another origin (fonts, analytics, a CDN), add that
    # origin here on purpose; the browser console names what was blocked.
    /*
      Cache-Control: no-cache
      X-Content-Type-Options: nosniff
      Referrer-Policy: strict-origin-when-cross-origin
      Content-Security-Policy: default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'
    EOF
    printf '.DS_Store\n.assetsignore\n' > "$d/public/.assetsignore"
    echo "Scaffolded $d (index.html, _headers, .assetsignore). Edit public/index.html, then: just plan $domain"

# Scaffold a redirect-only site under $FLAREPRESS_SITES (301s everything to target): just alias old.com new.com
[positional-arguments]
alias domain target:
    #!/usr/bin/env bash
    set -euo pipefail
    domain=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]'); target=$(printf '%s' "$2" | tr '[:upper:]' '[:lower:]')
    for n in "$domain" "$target"; do
      printf '%s\n' "$n" | grep -Eq '^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z0-9-]*[a-z]$' \
        || { echo "🛑 '$n' is not a domain name (example.com, not https://example.com/)" >&2; exit 1; }
    done
    d="{{sites}}/$domain"
    [ ! -e "$d" ] || { echo "🛑 $d already exists; nothing changed" >&2; exit 1; }
    mkdir -p "$d"
    cat > "$d/index.js" <<EOF
    export default {
      async fetch(request) {
        const url = new URL(request.url);
        const target = new URL(url.pathname + url.search, "https://$target");
        return Response.redirect(target.toString(), 301);
      }
    };
    EOF
    cat > "$d/wrangler.toml" <<EOF
    name = "$(echo "$domain" | tr '.' '-')"
    compatibility_date = "$(date +%Y-%m-%d)"
    main = "index.js"

    routes = [
      { pattern = "$domain", custom_domain = true },
      { pattern = "www.$domain", custom_domain = true }
    ]
    EOF
    echo "Scaffolded $d: redirects (301) to $target. Then: just plan $domain"

# A site's D1 or KV, with the same auth as push; arguments reach wrangler whole:
#   just d1 ~/src/app/site execute my-db --remote --command "SELECT count(*) FROM t"
#   just kv my.site key list --binding LOGS --remote
[positional-arguments]
d1 site *args:
    @cd "{{here}}" && "{{tools}}/wrangler.sh" "$1" d1 "${@:2}"

[positional-arguments]
kv site *args:
    @cd "{{here}}" && "{{tools}}/wrangler.sh" "$1" kv "${@:2}"

# Who wrangler is signed in as, and which accounts it can reach
[positional-arguments]
whoami site:
    @cd "{{here}}" && "{{tools}}/wrangler.sh" "$1" whoami

# ---- App sites (a folder with site.conf): the shared deploy steps.
# ---- All read-only except `rollback --yes`. See AGENTS.md, App sites.

# 🔎 Pre-flight for an app site: on main, committed, pushed, files present (--from DIR: the caller's checkout)
[positional-arguments]
check site *flags:
    @cd "{{here}}" && "{{tools}}/site.sh" "$1" check "${@:2}"

# 🚧 Refuse a first publish (the site does not answer yet) unless FIRST_PUBLISH=1
[positional-arguments]
gate site:
    @cd "{{here}}" && "{{tools}}/site.sh" "$1" gate

# 🏷️ Write version.json into the assets folder: git describe, -dirty when a committed path has edits
[positional-arguments]
stamp site:
    @cd "{{here}}" && "{{tools}}/site.sh" "$1" stamp

# ✅ After a push: the stamped version is live, the compare files match, 200s, and a PWA's worker headers
[positional-arguments]
verify site *flags:
    @cd "{{here}}" && "{{tools}}/site.sh" "$1" verify "${@:2}"

# 📋 What is live (any site): status, cache headers, and for an app site its version and whether it is main's
[positional-arguments]
live site:
    @cd "{{here}}" && "{{tools}}/site.sh" "$1" live

# ⏪ Roll an app site back (dry unless --yes; a rollback=revert site only prints the revert steps)
[positional-arguments]
rollback site *flags:
    @cd "{{here}}" && "{{tools}}/site.sh" "$1" rollback "${@:2}"

# 🧪 The tools' tests, against a local server; nothing reaches Cloudflare
test:
    bash -n tools/site.sh && bash -n tools/wrangler.sh && bash -n tools/release.sh
    bash tests/site_test.sh
    bash tests/new_test.sh
    bash tests/wrangler_test.sh
    bash tests/release_test.sh
    python3 -m unittest discover -s tests -p 'test_*.py'

# 🌐 Every site under $FLAREPRESS_SITES answers as its folder says (read-only GETs): just sites [domain ...]
sites *domains:
    python3 tools/sites_check.py {{domains}}

# 📥 Copy each site's D1 telemetry and Cloudflare stats into its local SQLite archive (just harvest [domain ...] [--days N] [--quiet])
harvest *domains:
    python3 tools/harvest.py {{domains}}

# 🚀 Release: GitHub runs the tests, tags main as the next v1.M.P and moves v1 (just release minor: something new; agents start it through the API)
release bump="patch":
    gh workflow run release.yml --repo pdxfixit/flarepress --ref main -f bump={{bump}}
    @echo "Started: https://github.com/pdxfixit/flarepress/actions/workflows/release.yml"
