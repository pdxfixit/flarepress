#!/usr/bin/env bash
# Tests for tools/site.sh against a throwaway app repo (its site folder inside
# it) and a local web server. Nothing reaches Cloudflare: `push` and
# `rollback --yes` on a redeploy site are never run here.
#   bash tests/site_test.sh
# site.sh runs under /bin/bash when it exists (macOS ships 3.2), so syntax newer
# than 3.2 fails here and not mid-deploy.
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
SH=bash; [ -x /bin/bash ] && SH=/bin/bash
T=$(mktemp -d); SERVER=""
trap '[ -n "$SERVER" ] && kill "$SERVER" 2>/dev/null; rm -rf "$T"' EXIT
fails=0
ok() { echo "✅ $1"; }
no() { echo "❌ $1"; fails=$((fails + 1)); }
G() { git -C "$T/app" -c user.name=t -c user.email=t@t "$@" >/dev/null 2>&1; }

# The app: web/ with a PWA's files, a bare origin, main pushed.
git init -q --bare "$T/origin.git"
git init -q -b main "$T/app"
mkdir -p "$T/app/web"
echo '<!doctype html><title>t</title>' > "$T/app/web/index.html"
echo 'self.addEventListener("fetch", () => {});' > "$T/app/web/sw.js"
echo '{"name":"t"}' > "$T/app/web/manifest.webmanifest"
echo "self.V = '1';" > "$T/app/web/version.js"
echo 'version.json' > "$T/app/.gitignore"
echo 'notes' > "$T/app/TODO.md"
# The site folder, in the app's repo: assets are ../web.
mkdir -p "$T/app/site"
printf 'name = "site-test"\nroutes = [ { pattern = "site.test", custom_domain = true } ]\n\n[assets]\ndirectory = "../web"\n' > "$T/app/site/wrangler.toml"
G add -A; G commit -m one; G remote add origin "$T/origin.git"; G push -u origin main

# A local server: sw.js gets no-cache unless NOCACHE_OFF exists (http.server sends none).
PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')
cat > "$T/serve.py" <<'PY'
import http.server, os, sys
root, port, flag = sys.argv[1], int(sys.argv[2]), sys.argv[3]
class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k): super().__init__(*a, directory=root, **k)
    def log_message(self, *a): pass
    def end_headers(self):
        if self.path.split('?')[0] == '/sw.js' and not os.path.exists(flag):
            self.send_header('Cache-Control', 'no-cache')
        super().end_headers()
    def do_GET(self):
        # FLAP holds a count: while it exists, version.js answers this commit's copy
        # and an old one in turn, as Cloudflare's edge can while it updates.
        flap = os.path.join(os.path.dirname(flag), 'FLAP')
        if self.path.split('?')[0] == '/version.js' and os.path.exists(flap):
            n = int(open(flap).read() or 0) + 1
            open(flap, 'w').write(str(n))
            if n % 2 == 0:
                body = b"self.V = 'old';\n"
                self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers()
                self.wfile.write(body); return
        super().do_GET()
http.server.ThreadingHTTPServer(('127.0.0.1', port), H).serve_forever()
PY
python3 "$T/serve.py" "$T/app/web" "$PORT" "$T/NOCACHE_OFF" & SERVER=$!
for _ in $(seq 1 50); do curl -fsS "http://127.0.0.1:$PORT/index.html" >/dev/null 2>&1 && break; sleep 0.1; done

# flarepress itself, copied, so a test can never reach the real one's sites.
mkdir -p "$T/fp"; cp -R "$ROOT/tools" "$T/fp/"
conf() { printf 'url=http://127.0.0.1:%s\ncommitted=web\npwa=1\ncompare=version.js\ncompare_wait=2\nversion_wait=2\nrollback=%s\n' "$PORT" "${1:-redeploy}" > "$T/app/site/site.conf"; }
conf
# GITHUB_* are cleared so a run on GitHub tests the local rules; the CI tests set them.
run() { (cd "$T/app" && env -u GITHUB_ACTIONS -u GITHUB_REF -u GITHUB_SHA FLAREPRESS="$T/fp" "$SH" "$T/fp/tools/site.sh" "$T/app/site" "$@") > "$T/out" 2>&1; }
says() { grep -q -- "$1" "$T/out"; }

# 1. check passes on a clean, pushed main.
run check --from "$T/app" && says 'would ship' && ok "check passes clean" || { no "check passes clean"; cat "$T/out"; }
# 2. Uncommitted edits in web/ stop it.
echo x >> "$T/app/web/index.html"
run check && no "check refuses edits in web/" || ok "check refuses edits in web/"
G checkout -- web/index.html
# 3. An unpushed HEAD stops it.
echo two > "$T/app/web/two.txt"; G add web/two.txt; G commit -m two
run check && no "check refuses an unpushed HEAD" || { says 'push first' && ok "check refuses an unpushed HEAD" || no "check refuses an unpushed HEAD (wrong reason)"; }
G push
# 4. A caller that is not the linked checkout is refused.
run check --from /tmp && no "check refuses another checkout" || ok "check refuses another checkout"
# 5. A mistyped key in site.conf is an error, not a silent default.
echo 'complare=x' >> "$T/app/site/site.conf"
run check && no "an unknown site.conf key fails" || { says 'unknown key' && ok "an unknown site.conf key fails" || no "an unknown site.conf key fails (wrong reason)"; }
conf
# 5b. A mistyped value is an error too: rollback=revrt once meant redeploy.
for bad in 'rollback=revrt' 'pwa=yes' 'compare_wait=ninety' 'pwa = 1' 'just a line'; do
  conf; echo "$bad" >> "$T/app/site/site.conf"
  run rollback && no "site.conf refuses '$bad'" || { says 'site.conf' && ok "site.conf refuses '$bad'" || { no "site.conf refuses '$bad' (wrong reason)"; cat "$T/out"; }; }
done
conf; printf '# a note\n  # indented\n\n' >> "$T/app/site/site.conf"
run check && ok "site.conf comments and blank lines are fine" || { no "site.conf comments and blank lines are fine"; cat "$T/out"; }
conf
# 6. Noodle Pads' bug: an edit outside the committed paths must not make stamp
#    and verify disagree (stamp said X, verify expected X-dirty and failed).
echo more >> "$T/app/TODO.md"
run stamp; run verify && says 'live: http' && ok "stamp then verify agree with a dirty TODO.md" || { no "stamp then verify agree with a dirty TODO.md"; cat "$T/out"; }
G checkout -- TODO.md
# 6b. The stamp names the short commit, for apps that show their build.
grep -q "\"commit\":\"$(git -C "$T/app" rev-parse --short HEAD)\"" "$T/app/web/version.json" && ok "stamp names the short commit" || { no "stamp names the short commit"; cat "$T/app/web/version.json"; }
# 6c. A host this Mac cannot resolve (a cached miss after a first publish) is
#     checked at the resolver's answer instead; with no answer, verify still fails.
printf '#!/bin/sh\necho 127.0.0.1\n' > "$T/dns"; chmod +x "$T/dns"   # a resolver that knows every host
DEPLOY_URL="http://isopix-test.invalid:$PORT" DNS_FALLBACK="$T/dns" run verify \
  && says 'cannot resolve isopix-test.invalid' && ok "verify checks an unresolvable host at the fallback's address" \
  || { no "verify checks an unresolvable host at the fallback's address"; cat "$T/out"; }
DEPLOY_URL="http://isopix-test.invalid:$PORT" DNS_FALLBACK="$T/dns" run gate \
  && ok "gate passes a published host this Mac cannot resolve yet" || { no "gate passes a published host this Mac cannot resolve yet"; cat "$T/out"; }
DEPLOY_URL="http://isopix-test.invalid:$PORT" DNS_FALLBACK="true" run gate \
  && no "gate refuses a host no resolver knows" || ok "gate refuses a host no resolver knows"
DEPLOY_URL="http://isopix-test.invalid:$PORT" DNS_FALLBACK="true" run verify \
  && no "verify fails when no resolver knows the host" || ok "verify fails when no resolver knows the host"
# 7. A version that is not live fails.
run verify --want nope && no "verify fails on the wrong version" || ok "verify fails on the wrong version"
# 8. pwa=1 checks the service worker's cache header.
touch "$T/NOCACHE_OFF"
run verify && no "verify fails without sw.js no-cache" || { says 'sw-cache' && ok "verify fails without sw.js no-cache" || no "verify fails without sw.js no-cache (wrong reason)"; }
rm -f "$T/NOCACHE_OFF"
# 8b. An edge that answers new and old copies in turn still passes: the check
#     judges by what its wait saw (a second look once failed a good deploy, 2026-10-15).
echo 0 > "$T/FLAP"; conf; sed -i.bak 's/^compare_wait=2$/compare_wait=6/' "$T/app/site/site.conf"
run verify && says 'live version.js is this commit' && ok "verify passes while the edge flaps between copies" || { no "verify passes while the edge flaps between copies"; cat "$T/out"; }
rm -f "$T/FLAP" "$T/app/site/site.conf.bak"; conf
# 9. compare: a live file that differs from the checkout fails.
mkdir -p "$T/other"; cp "$T/app/web/"* "$T/other/"; echo "self.V = 'other';" > "$T/other/version.js"; cp "$T/app/web/version.json" "$T/other/"
run verify --web "$T/other" && no "verify fails when a compare file differs" || { says 'differs' && ok "verify fails when a compare file differs" || no "verify fails when a compare file differs (wrong reason)"; }
# 10. live: main's files after the stamp; a warning after a new web/ commit.
run live && says "serves main's" && ok "live: serves main's web" || { no "live: serves main's web"; cat "$T/out"; }
echo three > "$T/app/web/three.txt"; G add web/three.txt; G commit -m three
run live; says "differs from main's" && ok "live: warns when main moved on" || { no "live: warns when main moved on"; cat "$T/out"; }
# 11. rollback without --yes changes nothing.
before=$(git -C "$T/app" worktree list | wc -l)
run rollback && says 'dry: nothing changed' && [ "$(git -C "$T/app" worktree list | wc -l)" = "$before" ] && ok "rollback is dry without --yes" || { no "rollback is dry without --yes"; cat "$T/out"; }
# 12. rollback=revert prints the revert and refuses --yes.
conf revert
run rollback --yes && no "rollback=revert refuses --yes" || { says 'git revert --no-edit' && says 'refused' && ok "rollback=revert prints the revert and refuses --yes" || { no "rollback=revert refuses --yes (wrong output)"; cat "$T/out"; }; }
# 13. A plain folder (no site.conf) is not an app site.
rm "$T/app/site/site.conf"
run check && no "check needs site.conf" || { says 'not an app site' && ok "check needs site.conf" || no "check needs site.conf (wrong reason)"; }
conf
# 14. A site named by a link under FLAREPRESS_SITES is the same site.
mkdir -p "$T/sites"; ln -s "$T/app/site" "$T/sites/site.test"
(cd /tmp && FLAREPRESS_SITES="$T/sites" "$SH" "$T/fp/tools/site.sh" site.test stamp) > "$T/out" 2>&1 \
  && [ -f "$T/app/web/version.json" ] && ok "a site found by name stamps the app's web/" || { no "a site found by name stamps the app's web/"; cat "$T/out"; }
# 15. On GitHub Actions, check takes the pushed ref: main passes, another branch does not.
G push
ci() { (cd "$T/app" && GITHUB_ACTIONS=true GITHUB_REF="$1" GITHUB_SHA="$(git -C "$T/app" rev-parse HEAD)" "$SH" "$T/fp/tools/site.sh" "$T/app/site" check) > "$T/out" 2>&1; }
ci refs/heads/main && says 'pushed commit' && ok "CI: a run for main passes check" || { no "CI: a run for main passes check"; cat "$T/out"; }
ci refs/heads/feature && no "CI: a run for another branch is refused" || { says 'not refs/heads/main' && ok "CI: a run for another branch is refused" || no "CI: refused for the wrong reason"; }
# 15b. With no url=, the address is the first route's host: a zone route's /* is not part of it.
conf; sed -i.bak '/^url=/d' "$T/app/site/site.conf"; cp "$T/app/site/wrangler.toml" "$T/wt.bak"
printf 'name = "site-test"\nroutes = [ { pattern = "app.site-test.invalid/*", zone_name = "site-test.invalid" } ]\n\n[assets]\ndirectory = "../web"\n' > "$T/app/site/wrangler.toml"
run rollback && says 'Roll https://app.site-test.invalid back' && ok "a zone route's host is the address, without /*" || { no "a zone route's host is the address, without /*"; cat "$T/out"; }
printf 'name = "site-test"\nroutes = [ { pattern = "*.site-test.invalid/*", zone_name = "site-test.invalid" } ]\n\n[assets]\ndirectory = "../web"\n' > "$T/app/site/wrangler.toml"
run gate && no "a wildcard route with no url= is refused" || { says 'no address to check' && ok "a wildcard route with no url= is refused" || { no "a wildcard route with no url= is refused (wrong reason)"; cat "$T/out"; }; }
cp "$T/wt.bak" "$T/app/site/wrangler.toml"; rm -f "$T/app/site/site.conf.bak"
# 16. A redeploy rollback names the old commit's own site folder.
conf redeploy
run rollback && says 'site' && says 'dry: nothing changed' && ok "rollback names the old commit's site folder" || { no "rollback names the old commit's site folder"; cat "$T/out"; }

# 17. release= names the app's release file: the version is v<release>-<short commit>.
conf; echo 'release=web/version.js' >> "$T/app/site/site.conf"
cp "$T/app/web/version.json" "$T/stamp.bak"
run check && no "check refuses release= with no X.Y.Z" || { says 'no quoted X.Y.Z' && ok "check refuses release= with no X.Y.Z" || { no "check refuses release= with no X.Y.Z (wrong reason)"; cat "$T/out"; }; }
run stamp && no "release= with no X.Y.Z in the file fails" || { says 'no quoted X.Y.Z' && cmp -s "$T/stamp.bak" "$T/app/web/version.json" && ok "release= with no X.Y.Z fails, and leaves version.json as it was" || { no "release= with no X.Y.Z fails (wrong reason, or version.json changed)"; cat "$T/out"; }; }
echo "self.V = '1.2.3'; // since 0.9.1" > "$T/app/web/version.js"; G add web/version.js; G commit -m four; G push
run stamp; want="v1.2.3-$(git -C "$T/app" rev-parse --short HEAD)"
grep -q "\"version\":\"$want\"" "$T/app/web/version.json" && ok "release= stamps $want" || { no "release= stamps v<release>-<short commit>"; cat "$T/app/web/version.json"; }
run verify && says "live version is $want" && ok "release=: stamp then verify agree" || { no "release=: stamp then verify agree"; cat "$T/out"; }
run live && says "serves main's" && ok "release=: live finds the commit in the label" || { no "release=: live finds the commit in the label"; cat "$T/out"; }
conf; echo 'release=../outside.js' >> "$T/app/site/site.conf"
run check && no "release= outside the repo is refused" || { says 'inside the app repo' && ok "release= outside the repo is refused" || { no "release= outside the repo is refused (wrong reason)"; cat "$T/out"; }; }
conf
[ "$fails" = 0 ] && echo "🎉 site.sh: all passed" || { echo "💥 site.sh: $fails failed"; exit 1; }
