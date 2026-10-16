#!/usr/bin/env bash
# flarepress site tool: the deploy steps for an app site, a site folder inside
# the app's own repo. The app's recipes and the flarepress GitHub action call it;
# the app's own checks (tests, browser checks) stay in its repo.
#
#   tools/site.sh <site> check [--from DIR]      read-only pre-flight
#   tools/site.sh <site> gate                    refuse a first publish without FIRST_PUBLISH=1
#   tools/site.sh <site> stamp                   write version.json into the assets folder (gitignored in the app)
#   tools/site.sh <site> verify [--want V] [--web DIR]   after a push: is it live and well?
#   tools/site.sh <site> live                    what is live now (any site, read-only)
#   tools/site.sh <site> rollback [--to C] [--yes]       dry unless --yes
#
# <site> is a path to a site folder (one with a wrangler.toml; a folder by that
# name where you run it counts as a path), or a name under $FLAREPRESS_SITES
# (default ~/.config/flarepress/sites). The assets folder is
# wrangler.toml's [assets] directory, relative to the site folder.
#
# Config: <site>/site.conf, key=value lines, read and never run as shell:
#   url           the address to check (default: the first route in wrangler.toml)
#   committed     app-repo paths that ship (default: the assets folder)
#   pwa           1: a manifest and a service worker are required and checked live
#   compare       files under public/ the live site must serve byte for byte after a deploy
#   compare_wait  seconds to wait for those files (edge propagation), default 90
#   version_wait  seconds to wait for version.json, default 90
#   rollback      redeploy (a throwaway checkout of an old commit) or revert (print the steps only)
# Only `rollback --yes` changes the live site, through the Justfile's push, which
# accepts a path. DEPLOY_URL points the live checks at another server (tests).
# verify and live check through Cloudflare's resolver when this machine cannot
# resolve the host (pin_dns); DNS_FALLBACK overrides that resolver (tests).
# On GitHub Actions (GITHUB_ACTIONS=true) check trusts the pushed ref instead of
# a local branch and upstream: the run must be for refs/heads/main.
# Bash 3.2 safe (macOS /bin/bash): no mapfile, no associative arrays, no ${x,,}.
set -euo pipefail
FP="${FLAREPRESS:-$(cd "$(dirname "$0")/.." && pwd)}"
die() { echo "🛑 $*" >&2; exit 1; }
[ $# -ge 2 ] || die "usage: site.sh <site> check|gate|stamp|verify|live|rollback [flags]"
DOMAIN=$1 SUB=$2; shift 2
SITES="${FLAREPRESS_SITES:-$HOME/.config/flarepress/sites}"
# A folder here wins over a name: `site` in an app repo means ./site.
if [ -f "$DOMAIN/wrangler.toml" ]; then DIR=$(cd "$DOMAIN" && pwd)
else case "$DOMAIN" in
  */*|.|..) DIR=$(cd "$DOMAIN" 2>/dev/null && pwd) || die "no site folder $DOMAIN" ;;
  *) DIR="$SITES/$DOMAIN" ;;
esac; fi
[ -f "$DIR/wrangler.toml" ] || die "no $DIR/wrangler.toml"

KEYS=" url committed pwa compare compare_wait version_wait rollback "
conf() { [ -f "$DIR/site.conf" ] && sed -n "s/^$1=//p" "$DIR/site.conf" | tail -1 || true; }
# An app site needs site.conf, and a mistyped key or value is an error, not a
# silent default (rollback=revrt once meant redeploy, the mode revert refuses).
conf_check() {
  [ -f "$DIR/site.conf" ] || die "no $DIR/site.conf: $DOMAIN is not an app site (see AGENTS.md, App sites)"
  local line k bad=""
  while IFS= read -r line || [ -n "$line" ]; do
    # Blank lines and # comments (indented or not) are fine.
    case "$(printf '%s' "$line" | sed 's/^[[:space:]]*//')" in ''|'#'*) continue ;; esac
    k=${line%%=*}
    case "$line" in *=*) ;; *) k="" ;; esac
    case "$k" in ''|*[!a-z_]*) die "$DIR/site.conf: not key=value (no spaces around =): $line" ;; esac
    case "$KEYS" in *" $k "*) ;; *) bad="$bad $k" ;; esac
  done < "$DIR/site.conf"
  [ -z "$bad" ] || die "unknown key(s) in $DIR/site.conf:$bad"
  case "$(conf pwa)" in ''|0|1) ;; *) die "$DIR/site.conf: pwa=$(conf pwa), want 0 or 1" ;; esac
  case "$(conf rollback)" in ''|redeploy|revert) ;; *) die "$DIR/site.conf: rollback=$(conf rollback), want redeploy or revert" ;; esac
  for k in compare_wait version_wait; do
    case "$(conf "$k")" in *[!0-9]*) die "$DIR/site.conf: $k=$(conf "$k"), want whole seconds" ;; esac
  done
}
# The first route's host: a zone route is "app.example.com/*", a Custom Domain a bare host.
first_route() { sed -n 's/.*pattern *= *"\([^"]*\)".*/\1/p' "$DIR/wrangler.toml" | head -1 | sed 's#^[a-z]*://##; s#/.*##'; }
URL="${DEPLOY_URL:-$(conf url)}"
if [ -z "$URL" ]; then HOST=$(first_route); case "$HOST" in ''|*'*'*) ;; *) URL="https://$HOST" ;; esac; fi
URL="${URL%/}"
# The live checks need an address: url= in site.conf, or a route with a plain host.
need_url() { [ -n "$URL" ] || die "no address to check: add url=https://… to $DIR/site.conf (wrangler.toml's first route is ${HOST:-missing}, not a plain host)"; }
# The [assets] directory, relative to the site folder (public/ when not given).
ASSETS=$(python3 "$FP/tools/assets_dir.py" "$DIR/wrangler.toml")
PUBLIC=$(cd -P "$ASSETS" 2>/dev/null && pwd) || PUBLIC=""
REPO=""; [ -z "$PUBLIC" ] || REPO=$(git -C "$PUBLIC" rev-parse --show-toplevel 2>/dev/null || true)
COMMITTED=$(conf committed); [ -n "$COMMITTED" ] || COMMITTED=${PUBLIC#"$REPO"/}
g() { git -C "$REPO" "$@"; }
need_repo() { [ -n "$REPO" ] || die "$DIR's assets folder ($ASSETS) is not in a git repo"; }
secs() { local v; v=$(conf "$1"); echo "${v:-$2}"; }

# The version the site shows: git describe, plus -dirty when a committed path has
# uncommitted edits. stamp writes it and verify reads the stamp back, so the two
# can never disagree (archive/ and backup/ tags are bookkeeping, never a version).
version() {
  local v; v=$(g describe --tags --always --exclude 'archive/*' --exclude 'backup/*')
  # shellcheck disable=SC2086  # COMMITTED is a space-separated list of paths
  [ -z "$(g status --porcelain -- $COMMITTED)" ] || v="$v-dirty"
  echo "$v"
}

# First existing file of the given names under $1 (the app's own naming).
find_in() { local d=$1 f; shift; for f in "$@"; do [ -f "$d/$f" ] && { echo "$f"; return 0; }; done; return 1; }
manifest_file() { find_in "${1:-$PUBLIC}" manifest.webmanifest manifest.json || true; }
sw_file() { find_in "${1:-$PUBLIC}" sw.js service-worker.js serviceworker.js || true; }

# A just-published name can stay missing from this machine's DNS cache for the zone's
# negative TTL (30 min on Cloudflare): `gate` looked it up before it existed. On
# 2026-10-13 that made verify report 8 ❌ on Isopix's working first publish. When
# curl cannot resolve the host (exit 6) but 1.1.1.1 can, every check goes to that
# address (curl --resolve) and says so. gate does too: a cached miss is not "never
# published" (it refused Isopix's second deploy for that reason). A host no
# resolver knows still fails, so a real first publish still needs FIRST_PUBLISH=1.
RESOLVE=()
DNS_FALLBACK="${DNS_FALLBACK:-dig +short +time=3 @1.1.1.1}"
pin_dns() {
  local hostport host port ip rc=0
  need_url
  hostport=$(printf '%s' "$URL" | sed -E 's#^[a-z]+://([^/]+).*#\1#')
  host=${hostport%%:*}; port=${hostport#*:}
  [ "$port" != "$hostport" ] || case "$URL" in https://*) port=443 ;; *) port=80 ;; esac
  curl -sS -m 10 -o /dev/null "$URL/" 2>/dev/null || rc=$?
  [ "$rc" = 6 ] || return 0
  # shellcheck disable=SC2086  # DNS_FALLBACK is a command line
  ip=$($DNS_FALLBACK "$host" 2>/dev/null | grep -E '^[0-9]+(\.[0-9]+){3}$' | head -1 || true)
  [ -n "$ip" ] || return 0
  RESOLVE=(--resolve "$host:$port:$ip")
  echo "⚠️ this machine cannot resolve $host yet (a cached miss from before the first publish?); checking it at $ip, Cloudflare's answer"
}
cu() { curl ${RESOLVE[@]+"${RESOLVE[@]}"} "$@"; }

# GET $1 into the globals code, ctype, cache, etag (empty if unreachable).
probe() {
  local hdr; hdr=$(mktemp)
  code=$(cu -sS -m 20 -o /dev/null -D "$hdr" -w '%{http_code}' "$1" 2>/dev/null) || code=000
  ctype=$(tr -d '\r' < "$hdr" | awk -F': ' 'tolower($1)=="content-type"{print $2}' | tail -1)
  cache=$(tr -d '\r' < "$hdr" | awk -F': ' 'tolower($1)=="cache-control"{print $2}' | tail -1)
  etag=$(tr -d '\r' < "$hdr" | awk -F': ' 'tolower($1)=="etag"{print $2}' | tail -1)
  rm -f "$hdr"
}
stamp_of() { sed -n 's/.*"version" *: *"\([^"]*\)".*/\1/p' "$1" 2>/dev/null || true; }
live_version() { cu -fsS -m 20 "$URL/version.json?$RANDOM" 2>/dev/null | sed -n 's/.*"version" *: *"\([^"]*\)".*/\1/p' || true; }
same_live() { cu -fsS -m 20 "$URL/$1?$RANDOM" 2>/dev/null | cmp -s - "$2"; }
nocache() { case "$1" in *no-cache*|*no-store*|*max-age=0*) return 0 ;; *) return 1 ;; esac; }

cmd_check() {
  local from="" bad=0 m s upstream
  while [ $# -gt 0 ]; do case "$1" in --from) from=$2; shift 2 ;; *) die "check: unknown flag $1" ;; esac; done
  conf_check; need_repo
  # The deploy ships the assets folder's checkout: the caller must be that
  # checkout, or it would stamp one checkout and ship another.
  if [ -n "$from" ]; then
    [ "$(cd "$from" 2>/dev/null && git rev-parse --show-toplevel 2>/dev/null)" = "$REPO" ] \
      || die "$DIR ships from $REPO, not $from: deploy from that checkout"
  fi
  if [ "${GITHUB_ACTIONS:-}" = true ]; then
    # CI checks out the pushed commit: it is on main when the run is for main.
    [ "${GITHUB_REF:-}" = refs/heads/main ] || die "this run is for ${GITHUB_REF:-no ref}, not refs/heads/main; the live site is always a commit on main"
    [ -z "${GITHUB_SHA:-}" ] || [ "$(g rev-parse HEAD)" = "$GITHUB_SHA" ] || die "HEAD is not the pushed commit $GITHUB_SHA"
  else
    # A linked worktree is never the source of a deploy: deploy from the main checkout.
    [ "$(cd "$REPO" && cd "$(git rev-parse --git-dir)" && pwd -P)" = "$(cd "$REPO" && cd "$(git rev-parse --git-common-dir)" && pwd -P)" ] \
      || die "$REPO is a linked worktree, not the main checkout"
    [ "$(g symbolic-ref --short -q HEAD || true)" = main ] || die "$REPO is not on main (on: $(g branch --show-current)); the live site is always a commit on main"
  fi
  # shellcheck disable=SC2086
  if [ -n "$(g status --porcelain -- $COMMITTED)" ] && [ "${ALLOW_DIRTY:-}" != 1 ]; then
    echo "🛑 Uncommitted changes in $COMMITTED; commit them first (or ALLOW_DIRTY=1):" >&2
    g status --porcelain -- $COMMITTED >&2; exit 1
  fi
  echo "✅ $COMMITTED committed"
  [ -f "$PUBLIC/index.html" ] && echo "✅ index.html" || { echo "❌ index.html missing from $PUBLIC" >&2; bad=1; }
  if [ "$(conf pwa)" = 1 ]; then
    m=$(manifest_file); [ -n "$m" ] && echo "✅ $m" || { echo "❌ no web app manifest (manifest.webmanifest or manifest.json)" >&2; bad=1; }
    s=$(sw_file); [ -n "$s" ] && echo "✅ $s" || { echo "❌ no service worker (sw.js, service-worker.js or serviceworker.js)" >&2; bad=1; }
  fi
  [ "$bad" = 0 ] || die "required files missing; nothing to deploy"
  [ -f "$PUBLIC/_headers" ] || echo "⚠️ no _headers: Cloudflare's default cache headers apply"
  [ -f "$PUBLIC/.assetsignore" ] || echo "⚠️ no .assetsignore: .DS_Store would be uploaded"
  # The live site is always a pushed commit (on CI it is the push itself).
  if [ "${GITHUB_ACTIONS:-}" = true ]; then echo "✅ HEAD is the pushed commit"
  else
    upstream=$(g rev-parse --abbrev-ref '@{u}' 2>/dev/null) || die "main has no upstream: push it first (git push -u origin main)"
    [ "$(g rev-list --count "$upstream..HEAD")" = 0 ] || die "HEAD is ahead of $upstream: push first, then deploy"
    echo "✅ HEAD is pushed ($upstream)"
  fi
  echo "📦 would ship $(version)  (commit $(g rev-parse --short HEAD))"
}

# The first publish creates the Custom Domain (a DNS record) and needs the owner's go.
# While the site does not answer at all, stop unless FIRST_PUBLISH=1 is set on purpose.
cmd_gate() {
  pin_dns
  probe "$URL/"
  [ "$code" != 000 ] || [ "${FIRST_PUBLISH:-}" = 1 ] || die "$URL does not answer: not published yet (or no network). The first publish needs the owner's go: FIRST_PUBLISH=1"
  echo "✅ $URL answers ($code)"
}

cmd_stamp() {
  conf_check; need_repo
  local web=${1:-$PUBLIC}
  # commit: the short SHA, for an app that shows which build it runs (Isopix's corner stamp).
  printf '{"version":"%s","commit":"%s","date":"%s"}\n' "$(version)" "$(g rev-parse --short HEAD)" "$(date '+%Y-%m-%d %H:%M %Z')" > "$web/version.json"
  cat "$web/version.json"
}

cmd_verify() {
  local want="" web="$PUBLIC" failed="" got="" f t0 m s n
  while [ $# -gt 0 ]; do case "$1" in --want) want=$2; shift 2 ;; --web) web=$2; shift 2 ;; *) die "verify: unknown flag $1" ;; esac; done
  conf_check; pin_dns
  [ -n "$want" ] || want=$(stamp_of "$web/version.json")
  [ -n "$want" ] || die "no version stamp ($web/version.json); stamp first"
  # The edge can lag an upload: wait for this version.
  t0=$SECONDS; n=$(( $(secs version_wait 90) / 3 )); [ "$n" -ge 1 ] || n=1
  for _ in $(seq 1 "$n"); do got=$(live_version); [ "$got" = "$want" ] && break; sleep 3; done
  if [ "$got" = "$want" ]; then echo "✅ live version is $want (after $((SECONDS - t0)) s)"
  else echo "❌ live version is ${got:-nothing}; expected $want" >&2; failed="$failed version"; fi
  # Files that must match byte for byte (a release stamp that names a cache, say).
  # version.json can be live before other new files are: wait, and say how long it took.
  for f in $(conf compare); do
    [ -f "$web/$f" ] || { echo "❌ compare=$f, but $web/$f does not exist" >&2; failed="$failed $f"; continue; }
    t0=$SECONDS; n=$(( $(secs compare_wait 90) / 3 )); [ "$n" -ge 1 ] || n=1
    # Judge by what the wait saw: while the edge updates, one request can get the
    # new file and the next the old one, so a second look could fail a good deploy.
    seen=0
    for _ in $(seq 1 "$n"); do if same_live "$f" "$web/$f"; then seen=1; break; fi; sleep 3; done
    if [ "$seen" = 1 ]; then echo "✅ live $f is this commit's (after $((SECONDS - t0)) s)"
    else echo "❌ live $f differs from $web/$f" >&2; failed="$failed $f"; fi
  done
  probe "$URL/"; [ "$code" = 200 ] && echo "✅ 200 /" || { echo "❌ $code /" >&2; failed="$failed /"; }
  if [ "$(conf pwa)" = 1 ]; then
    m=$(manifest_file "$web"); s=$(sw_file "$web")
    probe "$URL/$m"; [ "$code" = 200 ] && echo "✅ 200 /$m" || { echo "❌ $code /$m" >&2; failed="$failed /$m"; }
    probe "$URL/$s"; [ "$code" = 200 ] && echo "✅ 200 /$s" || { echo "❌ $code /$s" >&2; failed="$failed /$s"; }
    if nocache "$cache"; then echo "✅ $s cache-control: $cache"; else echo "❌ $s cache-control is '${cache:-none}', not no-cache/no-store/max-age=0" >&2; failed="$failed sw-cache"; fi
    case "$ctype" in *javascript*) echo "✅ $s is javascript" ;; *) echo "❌ $s served as '${ctype:-?}', not javascript" >&2; failed="$failed sw-type" ;; esac
    cu -fsS -m 20 "$URL/$m" 2>/dev/null | python3 -c 'import json,sys; json.load(sys.stdin)' 2>/dev/null \
      && echo "✅ $m is valid JSON" || { echo "❌ $m is not valid JSON" >&2; failed="$failed manifest-json"; }
  fi
  [ -z "$failed" ] || die "live check failed:$failed. The deploy itself went through; read the ❌ lines."
  echo "✅ live: $URL is $want"
}

# Any folder, app site or not: status and cache headers, then for an app site the
# version and whether the live files are main's.
cmd_live() {
  local p m s v got sha mark f
  pin_dns
  m=""; s=""; v=""
  if [ -n "$PUBLIC" ] && [ "$(conf pwa)" = 1 ]; then m=$(manifest_file); s=$(sw_file); fi
  [ -f "$DIR/site.conf" ] && v=version.json  # only app sites are stamped
  for p in "/" ${m:+"/$m"} ${s:+"/$s"} ${v:+"/$v"}; do
    probe "$URL$p"
    case "$code" in 200|301|302|308) mark=✅ ;; *) mark=⚠️ ;; esac
    echo "$mark $code $p  cache: ${cache:--}  etag: ${etag:--}"
  done
  [ -f "$DIR/site.conf" ] && [ -n "$REPO" ] || return 0
  for f in $(conf compare); do
    [ -f "$PUBLIC/$f" ] || continue
    if same_live "$f" "$PUBLIC/$f"; then echo "✅ live $f matches this checkout"; else echo "⚠️ live $f is missing or differs from this checkout's"; fi
  done
  got=$(live_version)
  [ -n "$got" ] || { echo "⚠️ no live version (not published yet, or no version.json)"; return 0; }
  sha=${got%-dirty}; sha=${sha##*-g}
  # shellcheck disable=SC2086
  if g cat-file -e "$sha^{commit}" 2>/dev/null && g diff --quiet "$sha" main -- $COMMITTED; then
    echo "✅ live $got serves main's $COMMITTED"
  else
    echo "⚠️ live $got differs from main's $COMMITTED (or is not a commit here)"
  fi
  case "$got" in *-dirty) echo "⚠️ live was deployed with uncommitted edits (-dirty)" ;; esac
}

# rollback [--to C] [--yes]. Default target: the second-newest commit that changed
# a committed path (the previous release). rollback=revert only prints the revert
# steps and refuses --yes; rollback=redeploy pushes a throwaway checkout of that commit.
cmd_rollback() {
  local to yes=0 sha mode tmp wt v rel bad
  conf_check; need_repo
  SITE_REL=$(cd -P "$DIR" && pwd); SITE_REL=${SITE_REL#"$REPO"/}
  # shellcheck disable=SC2086
  to=$(g log -2 --format=%H main -- $COMMITTED | sed -n 2p)
  while [ $# -gt 0 ]; do case "$1" in --to) to=$2; shift 2 ;; --yes) yes=1; shift ;; *) die "rollback: unknown flag $1" ;; esac; done
  sha=$(g rev-parse --verify -q "$to^{commit}") || die "no such commit: $to"
  mode=$(conf rollback); mode=${mode:-redeploy}
  if [ "$mode" = revert ]; then
    # shellcheck disable=SC2086
    bad=$(g log --format=%h "$sha..main" -- $COMMITTED | tr '\n' ' ')
    echo "⏪ $DOMAIN rolls back by reverting, never by deploying an old checkout (site.conf: rollback=revert)."
    echo "   Back to: $(g log -1 --format='%h %s' "$sha")"
    if [ -z "$bad" ]; then echo "   Nothing to revert: main's $COMMITTED is already that commit's."
    else cat <<STEPS
   In $REPO, on main:
   1. git revert --no-edit ${bad% }
   2. just test
   3. git push origin main
   4. deploy: the push to main does it (or the app's own deploy recipe)
STEPS
    fi
    grep -q '^\[\[d1_databases\]\]' "$DIR/wrangler.toml" && echo "   ⚠️ a revert does not undo a D1 migration: the Worker must cope with the newer tables."
    [ "$yes" = 0 ] || die "rollback=revert: --yes is refused; run the steps above"
    return 0
  fi
  echo "⏪ Roll $URL back to: $(g log -1 --format='%h %s' "$sha")"
  cat <<STEPS
   1. git worktree add --detach <tmp>/wt $sha        a throwaway checkout of that commit
   2. write version.json into its assets folder with that commit's version
   3. just push <tmp>/wt/$SITE_REL                   its own site folder, as that commit had it
   4. verify --want <that version>
   5. remove the worktree
   Afterwards the live site is NOT main's HEAD. Fix forward, or deploy main again.
STEPS
  grep -q '^\[\[d1_databases\]\]' "$DIR/wrangler.toml" && echo "   ⚠️ D1 binding: this restores Worker code, not the database"
  [ "$yes" = 1 ] || { echo "(dry: nothing changed. Add --yes to run it: rollback --to $to --yes)"; return 0; }
  tmp=$(mktemp -d); wt="$tmp/wt"
  # shellcheck disable=SC2064  # expand now: these paths are fixed for this run
  trap "git -C '$REPO' worktree remove --force '$wt' 2>/dev/null || true; rm -rf '$tmp'" EXIT
  g worktree add -q --detach "$wt" "$sha"
  [ -f "$wt/$SITE_REL/wrangler.toml" ] || die "$sha has no $SITE_REL/wrangler.toml: it predates the site folder in this repo"
  rel=${PUBLIC#"$REPO"/}
  [ -d "$wt/$rel" ] || die "$sha has no $rel/ folder"
  v=$(git -C "$wt" describe --tags --always --exclude 'archive/*' --exclude 'backup/*')
  printf '{"version":"%s","date":"%s"}\n' "$v" "$(date '+%Y-%m-%d %H:%M %Z')" > "$wt/$rel/version.json"
  "$FP/tools/wrangler.sh" "$wt/$SITE_REL" deploy
  cmd_verify --want "$v" --web "$wt/$rel"
}

case "$SUB" in
  check) cmd_check "$@" ;; gate) cmd_gate "$@" ;; stamp) cmd_stamp "$@" ;; verify) cmd_verify "$@" ;;
  live) cmd_live "$@" ;; rollback) cmd_rollback "$@" ;;
  *) die "unknown command '$SUB': check|gate|stamp|verify|live|rollback" ;;
esac
