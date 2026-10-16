#!/usr/bin/env bash
# `just new` scaffolds a site folder with safe defaults: wrangler.toml, an index,
# _headers (no-cache, nosniff, a referrer policy and a strict CSP) and
# .assetsignore, under $FLAREPRESS_SITES; `just alias` a redirect Worker. Runs the
# real recipes into a throwaway folder; nothing reaches Cloudflare.
#   bash tests/new_test.sh
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
fails=0
ok() { echo "✅ $1"; }
no() { echo "❌ $1"; fails=$((fails + 1)); }
export FLAREPRESS_SITES="$T/sites"
just --justfile "$ROOT/Justfile" new example.test > "$T/out" 2>&1 || { no "just new runs"; cat "$T/out"; }
d="$T/sites/example.test"
grep -q 'pattern = "example.test"' "$d/wrangler.toml" 2>/dev/null && grep -q 'directory = "./public"' "$d/wrangler.toml" && ok "wrangler.toml: route and assets" || no "wrangler.toml: route and assets"
[ -f "$d/public/index.html" ] && ok "index.html" || no "index.html"
h="$d/public/_headers"
# Rules apply to a path line at column 0; header lines are indented under it.
[ "$(grep -c '^/\*$' "$h" 2>/dev/null)" = 1 ] && ok "_headers: one /* rule" || no "_headers: one /* rule"
for want in 'Cache-Control: no-cache' 'X-Content-Type-Options: nosniff' 'Referrer-Policy: ' "Content-Security-Policy: default-src 'self'"; do
  grep -q "^  $want" "$h" 2>/dev/null && ok "_headers: $want" || no "_headers: $want"
done
grep -q '^\.DS_Store$' "$d/public/.assetsignore" 2>/dev/null && ok ".assetsignore keeps .DS_Store out" || no ".assetsignore keeps .DS_Store out"
just --justfile "$ROOT/Justfile" alias old.test new.test > "$T/out" 2>&1 || { no "just alias runs"; cat "$T/out"; }
grep -q '"https://new.test"' "$T/sites/old.test/index.js" 2>/dev/null && grep -q 'main = "index.js"' "$T/sites/old.test/wrangler.toml" \
  && ok "alias: index.js redirects to the target" || no "alias: index.js redirects to the target"
# A second run never overwrites a site someone has edited.
echo '<h1>mine</h1>' > "$d/public/index.html"
just --justfile "$ROOT/Justfile" new example.test > "$T/out" 2>&1 && no "new refuses an existing folder" \
  || { grep -q 'already exists' "$T/out" && ok "new refuses an existing folder" || { no "new refuses an existing folder (wrong reason)"; cat "$T/out"; }; }
grep -q 'mine' "$d/public/index.html" && ok "new left the existing index.html alone" || no "new left the existing index.html alone"
just --justfile "$ROOT/Justfile" alias old.test other.test > "$T/out" 2>&1 && no "alias refuses an existing folder" || ok "alias refuses an existing folder"
grep -q '"https://new.test"' "$T/sites/old.test/index.js" && ok "alias left the existing redirect alone" || no "alias left the existing redirect alone"
# A URL or a path is not a domain: nothing is made.
for bad in https://example.test example.test/ ../up.test 'a b.test' nodot; do
  just --justfile "$ROOT/Justfile" new "$bad" > "$T/out" 2>&1 && no "new refuses '$bad'" || ok "new refuses '$bad'"
  just --justfile "$ROOT/Justfile" alias x.test "$bad" > "$T/out" 2>&1 && no "alias refuses target '$bad'" || ok "alias refuses target '$bad'"
done
[ "$(ls "$T/sites")" = "$(printf 'example.test\nold.test')" ] && ok "a refused name made no folder" || { no "a refused name made no folder"; ls -R "$T/sites"; }
# Upper case is folded: the folder, the route and the Worker name are lower case.
just --justfile "$ROOT/Justfile" new Mixed.Example.Test > "$T/out" 2>&1
grep -q '^name = "mixed-example-test"$' "$T/sites/mixed.example.test/wrangler.toml" 2>/dev/null && ok "new folds a domain to lower case" || { no "new folds a domain to lower case"; cat "$T/out"; }
[ "$fails" = 0 ] && echo "🎉 just new: all passed" || { echo "💥 just new: $fails failed"; exit 1; }
