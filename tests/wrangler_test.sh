#!/usr/bin/env bash
# tools/wrangler.sh: ${NAME} ids are filled from the environment into a copy that
# wrangler reads, a missing one stops the run, and CLOUDFLARE_API_TOKEN reaches
# wrangler. WRANGLER is a stand-in that prints what it got; nothing reaches Cloudflare.
#   bash tests/wrangler_test.sh
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
SH=bash; [ -x /bin/bash ] && SH=/bin/bash
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
fails=0
ok() { echo "✅ $1"; }
no() { echo "❌ $1"; fails=$((fails + 1)); }

mkdir -p "$T/site"
printf 'name = "t"\n\n[[d1_databases]]\nbinding = "DB"\ndatabase_id = "${TEST_DB_ID}"\n' > "$T/site/wrangler.toml"
# The stand-in: its arguments, the token it saw, and the config it was given.
cat > "$T/fake" <<'F'
#!/bin/sh
echo "args: $*"
echo "token: ${CLOUDFLARE_API_TOKEN:-none}"
while [ $# -gt 0 ]; do [ "$1" = --config ] && { echo "config:"; cat "$2"; }; shift; done
F
chmod +x "$T/fake"
run() { (cd "$T" && env -u CLOUDFLARE_API_TOKEN WRANGLER="$T/fake" PATH="/usr/bin:/bin" "$@") > "$T/out" 2>&1; }

run TEST_DB_ID=abc-123 CLOUDFLARE_API_TOKEN=tok "$SH" "$ROOT/tools/wrangler.sh" "$T/site" deploy --dry-run
grep -q 'database_id = "abc-123"' "$T/out" && ok "the id is filled into the copy wrangler reads" || { no "the id is filled"; cat "$T/out"; }
grep -q 'token: tok' "$T/out" && ok "CLOUDFLARE_API_TOKEN reaches wrangler" || { no "the token reaches wrangler"; cat "$T/out"; }
[ ! -e "$T/site/.wrangler.deploy.toml" ] && ok "the filled copy is removed after" || no "the filled copy is removed after"
grep -q '${TEST_DB_ID}' "$T/site/wrangler.toml" && ok "wrangler.toml itself is untouched" || no "wrangler.toml itself is untouched"
run "$SH" "$ROOT/tools/wrangler.sh" "$T/site" deploy
grep -q 'needs TEST_DB_ID' "$T/out" && ! grep -q '^args:' "$T/out" && ok "a missing id stops the run before wrangler" || { no "a missing id stops the run"; cat "$T/out"; }
printf 'name = "plain"\n' > "$T/site/wrangler.toml"
run "$SH" "$ROOT/tools/wrangler.sh" "$T/site" whoami
grep -q '^args: whoami$' "$T/out" && ok "a plain wrangler.toml is passed through as it is" || { no "a plain wrangler.toml passes through"; cat "$T/out"; }

# A bare name that is a folder where you run it is that folder (`site` in an app repo).
run "$SH" "$ROOT/tools/wrangler.sh" site whoami
grep -q '^args: whoami$' "$T/out" && ok "a bare folder name here is a path" || { no "a bare folder name here is a path"; cat "$T/out"; }
[ "$fails" = 0 ] && echo "🎉 wrangler.sh: all passed" || { echo "💥 wrangler.sh: $fails failed"; exit 1; }
