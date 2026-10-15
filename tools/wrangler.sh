#!/usr/bin/env bash
# Runs wrangler for one site folder: tools/wrangler.sh <site> <wrangler args…>
#   tools/wrangler.sh ~/src/app/site deploy
#   tools/wrangler.sh ~/src/app/site deploy --dry-run
#   tools/wrangler.sh ~/src/app/site d1 execute my-db --remote --command "SELECT 1"
#
# <site> is a path to a folder with a wrangler.toml, or a name under
# $FLAREPRESS_SITES (default ~/.config/flarepress/sites).
#
# Auth: CLOUDFLARE_API_TOKEN when it is set (GitHub Actions passes the repo's
# secret), else the macOS Keychain item named by $CLOUDFLARE_KEYCHAIN (default
# cloudflare-www-deploy), else wrangler's own login.
#
# IDs a public repo must not hold (a D1 database_id, say) are written in
# wrangler.toml as ${NAME}. Each is filled from the environment into a copy next
# to it (.wrangler.deploy.toml, removed on exit), which wrangler then reads; a
# name with no value stops the run before wrangler starts.
#
# Bash 3.2 safe (macOS /bin/bash).
set -euo pipefail
FP="${FLAREPRESS:-$(cd "$(dirname "$0")/.." && pwd)}"
die() { echo "🛑 $*" >&2; exit 1; }
[ $# -ge 2 ] || die "usage: wrangler.sh <site> <wrangler args…>"
SITE=$1; shift
SITES="${FLAREPRESS_SITES:-$HOME/.config/flarepress/sites}"
case "$SITE" in */*|.|..) DIR=$SITE ;; *) DIR="$SITES/$SITE" ;; esac
[ -f "$DIR/wrangler.toml" ] || die "no $DIR/wrangler.toml"
cd "$DIR"

# app sites are stamped by their repo first; a push from anywhere else ships a stale version.json
if [ "${1:-}" = deploy ] && [ -f site.conf ]; then
  case " $* " in *" --dry-run "*) ;; *)
    stamp=$(python3 "$FP/tools/assets_dir.py" wrangler.toml)/version.json
    [ -n "$(find "$stamp" -mmin -10 2>/dev/null)" ] \
      || echo "⚠️ $DIR is an app site and $stamp was not stamped in the last 10 minutes: deploy it through its repo"
  ;; esac
fi

if [ -z "${CLOUDFLARE_API_TOKEN:-}" ] && command -v security > /dev/null; then
  TOKEN=$(security find-generic-password -s "${CLOUDFLARE_KEYCHAIN:-cloudflare-www-deploy}" -w 2>/dev/null || true)
  [ -z "$TOKEN" ] || export CLOUDFLARE_API_TOKEN="$TOKEN"
fi

config=()
if grep -q '\${[A-Za-z_][A-Za-z0-9_]*}' wrangler.toml; then
  python3 "$FP/tools/fill_ids.py" wrangler.toml .wrangler.deploy.toml || exit 1
  trap 'rm -f .wrangler.deploy.toml' EXIT
  config=(--config .wrangler.deploy.toml)
fi

${WRANGLER:-npx --yes wrangler@4.151.0} "$@" ${config[@]+"${config[@]}"}
