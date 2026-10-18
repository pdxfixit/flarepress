#!/usr/bin/env bash
# `just secret <site> NAME` runs `wrangler secret put NAME` through tools/wrangler.sh, so it gets push's auth and
# ${NAME} ids, and the value reaches wrangler on stdin, never as an argument. WRANGLER is a stand-in that prints
# what it got; nothing reaches Cloudflare.
#   bash tests/secret_test.sh
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
T=$(cd "$(mktemp -d)" && pwd -P); trap 'rm -rf "$T"' EXIT
fails=0
ok() { echo "✅ $1"; }
no() { echo "❌ $1"; fails=$((fails + 1)); }
J="$ROOT/Justfile"
export FLAREPRESS_SITES="$T/sites"
mkdir -p "$T/sites/example.test" "$T/app/site"
printf 'name = "t"\n' > "$T/sites/example.test/wrangler.toml"
printf 'name = "a"\n\n[[d1_databases]]\nbinding = "DB"\ndatabase_id = "${TEST_DB_ID}"\n' > "$T/app/site/wrangler.toml"
# The stand-in: its arguments, the token it saw, what came on stdin, and the config it was given.
cat > "$T/fake" <<'F'
#!/bin/sh
echo "args: $*"
echo "token: ${CLOUDFLARE_API_TOKEN:-none}"
echo "stdin: $(cat)"
while [ $# -gt 0 ]; do [ "$1" = --config ] && { echo "config:"; cat "$2"; }; shift; done
F
chmod +x "$T/fake"
# run <dir> <env and command…>: from <dir>, with the stand-in and no token from outside; stdin passes through.
run() { d=$1; shift; (cd "$d" && env -u CLOUDFLARE_API_TOKEN WRANGLER="$T/fake" "$@") > "$T/out" 2>&1; }

# A site by name: secret put NAME, the token reaches wrangler, the piped value arrives on stdin and in no argument.
printf 's3cr3t' | run "$T" CLOUDFLARE_API_TOKEN=tok just --justfile "$J" secret example.test EVENTS_KEY
grep -q '^args: secret put EVENTS_KEY$' "$T/out" && ok "runs wrangler secret put NAME" || { no "runs wrangler secret put NAME"; cat "$T/out"; }
grep -q '^token: tok$' "$T/out" && ok "CLOUDFLARE_API_TOKEN reaches wrangler" || { no "the token reaches wrangler"; cat "$T/out"; }
grep -q '^stdin: s3cr3t$' "$T/out" && ! grep -q '^args:.*s3cr3t' "$T/out" && ok "the value arrives on stdin, not as an argument" \
  || { no "the value arrives on stdin"; cat "$T/out"; }

# A relative path from where just runs, with ${NAME} ids filled as push fills them, and extra flags passed on.
printf 'v' | run "$T/app" TEST_DB_ID=abc-123 just --justfile "$J" secret site EVENTS_KEY --env production
grep -q '^args: secret put EVENTS_KEY --env production --config .wrangler.deploy.toml$' "$T/out" \
  && grep -q 'database_id = "abc-123"' "$T/out" && ok "a relative site path: ids filled, flags passed on" \
  || { no "a relative site path: ids filled, flags passed on"; cat "$T/out"; }
[ ! -e "$T/app/site/.wrangler.deploy.toml" ] && ok "the filled copy is removed after" || no "the filled copy is removed after"

# A name that is not an identifier (a value typed in its place, say) stops before wrangler starts.
for bad in 'sk-live/abc+123=' 'has space' '1STARTS_WITH_DIGIT'; do
  printf 'v' | run "$T" just --justfile "$J" secret example.test "$bad"
  grep -q 'is not a secret name' "$T/out" && ! grep -q '^args:' "$T/out" && ok "refuses the name '$bad'" || { no "refuses the name '$bad'"; cat "$T/out"; }
done
run "$T" just --justfile "$J" secret example.test < /dev/null && no "secret with no name is a usage error" || ok "secret with no name is a usage error"
printf 'v' | run "$T" just --justfile "$J" secret nothere.test EVENTS_KEY
grep -q 'no .*nothere.test/wrangler.toml' "$T/out" && ! grep -q '^args:' "$T/out" && ok "a missing site stops before wrangler" || { no "a missing site stops"; cat "$T/out"; }
[ "$fails" = 0 ] && echo "🎉 just secret: all passed" || { echo "💥 just secret: $fails failed"; exit 1; }
