#!/usr/bin/env bash
# `just link` registers app sites under $FLAREPRESS_SITES with a link named for the site's first route;
# `just unlink` removes only such a link. Runs the real recipes into a throwaway folder; nothing reaches Cloudflare.
#   bash tests/link_test.sh
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
T=$(cd "$(mktemp -d)" && pwd -P); trap 'rm -rf "$T"' EXIT
fails=0
ok() { echo "✅ $1"; }
no() { echo "❌ $1"; fails=$((fails + 1)); }
export FLAREPRESS_SITES="$T/sites"
J="$ROOT/Justfile"
site() { # site <dir> <wrangler.toml body>
  mkdir -p "$T/$1"; printf '%s\n' "$2" > "$T/$1/wrangler.toml"
}
site apps/one/site 'name = "one"
routes = [
  { pattern = "one.example.test", custom_domain = true },
  { pattern = "www.one.example.test", custom_domain = true }
]'
site apps/two/site '# pattern = "commented.example.test"
name = "two"
routes = [{ pattern = "Two.Example.Test", custom_domain = true }]'
site apps/norout/site 'name = "norout"'
site apps/wild/site 'routes = [{ pattern = "*.example.test/*", zone_name = "example.test" }]'
mkdir -p "$T/apps/nowrangler"

# One site, by absolute path: a link named for the first route, to the folder itself.
just --justfile "$J" link "$T/apps/one/site" > "$T/out" 2>&1 || { no "link runs"; cat "$T/out"; }
[ -L "$T/sites/one.example.test" ] && [ "$(cd "$T/sites/one.example.test" && pwd -P)" = "$T/apps/one/site" ] \
  && ok "link: one.example.test -> the site folder" || no "link: one.example.test -> the site folder"
[ ! -e "$T/sites/www.one.example.test" ] && ok "link: only the first route names it" || no "link: only the first route names it"
[ -f "$T/sites/one.example.test/wrangler.toml" ] && ok "link: the folder is readable through it" || no "link: the folder is readable through it"
# Again: nothing changes.
just --justfile "$J" link "$T/apps/one/site" > "$T/out" 2>&1 && grep -q 'already links' "$T/out" && ok "link twice: already linked" || { no "link twice: already linked"; cat "$T/out"; }
# A relative path is read from where just runs; the name is folded to lower case; a commented route is not a route.
(cd "$T" && just --justfile "$J" link apps/two/site) > "$T/out" 2>&1 || { no "link with a relative path"; cat "$T/out"; }
[ "$(cd "$T/sites/two.example.test" 2>/dev/null && pwd -P)" = "$T/apps/two/site" ] \
  && ok "link: relative path, lower-cased name, comment skipped" || no "link: relative path, lower-cased name, comment skipped"
# A link to somewhere else is pointed at the new folder.
mkdir -p "$T/moved/site"; cp "$T/apps/one/site/wrangler.toml" "$T/moved/site/"
just --justfile "$J" link "$T/moved/site" > "$T/out" 2>&1 && grep -q 'now links' "$T/out" \
  && [ "$(cd "$T/sites/one.example.test" && pwd -P)" = "$T/moved/site" ] && ok "link: a stale link is re-pointed" || { no "link: a stale link is re-pointed"; cat "$T/out"; }
# A real folder is never touched.
mkdir -p "$T/sites/real.example.test"; echo mine > "$T/sites/real.example.test/keep"
site apps/real/site 'routes = [{ pattern = "real.example.test", custom_domain = true }]'
just --justfile "$J" link "$T/apps/real/site" > "$T/out" 2>&1 && no "link refuses a real folder" \
  || { grep -q 'real folder' "$T/out" && ok "link refuses a real folder" || { no "link refuses a real folder (wrong reason)"; cat "$T/out"; }; }
[ "$(cat "$T/sites/real.example.test/keep")" = mine ] && [ ! -L "$T/sites/real.example.test" ] && ok "link left the real folder alone" || no "link left the real folder alone"
# Things that are not sites: nothing is made, and the exit is not 0.
before=$(ls "$T/sites" | sort | tr '\n' ' ')
for bad in "$T/apps/nowrangler" "$T/apps/norout/site" "$T/apps/wild/site" "$T/apps/missing"; do
  just --justfile "$J" link "$bad" > "$T/out" 2>&1 && no "link refuses ${bad#$T/}" || ok "link refuses ${bad#$T/}"
done
[ "$(ls "$T/sites" | sort | tr '\n' ' ')" = "$before" ] && ok "a refused path made no link" || no "a refused path made no link"
# Several at once: the good ones are linked even when one is bad, and the exit says one was not.
rm "$T/sites/two.example.test"
just --justfile "$J" link "$T/apps/missing" "$T/apps/two/site" > "$T/out" 2>&1 && no "link: a bad path makes the exit non-zero" || ok "link: a bad path makes the exit non-zero"
[ -L "$T/sites/two.example.test" ] && ok "link: the good path was still linked" || no "link: the good path was still linked"
just --justfile "$J" link > "$T/out" 2>&1 && no "link with no path is a usage error" || ok "link with no path is a usage error"
# unlink: a link goes; a real folder, a missing name and a path do not.
just --justfile "$J" unlink two.example.test > "$T/out" 2>&1 && [ ! -e "$T/sites/two.example.test" ] && [ -d "$T/apps/two/site" ] \
  && ok "unlink removes the link, not the site" || { no "unlink removes the link, not the site"; cat "$T/out"; }
just --justfile "$J" unlink real.example.test > "$T/out" 2>&1 && no "unlink refuses a real folder" || ok "unlink refuses a real folder"
[ "$(cat "$T/sites/real.example.test/keep")" = mine ] && ok "unlink left the real folder alone" || no "unlink left the real folder alone"
just --justfile "$J" unlink nothere.example.test > "$T/out" 2>&1 && no "unlink refuses a missing name" || ok "unlink refuses a missing name"
ln -s "$T/apps/one/site" "$T/outside-link"
just --justfile "$J" unlink ../outside-link > "$T/out" 2>&1 && no "unlink refuses a path" || ok "unlink refuses a path"
[ -L "$T/outside-link" ] && ok "unlink left a link outside the sites folder alone" || no "unlink left a link outside the sites folder alone"
[ "$fails" = 0 ] && echo "🎉 just link: all passed" || { echo "💥 just link: $fails failed"; exit 1; }
