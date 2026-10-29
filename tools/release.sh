#!/usr/bin/env bash
# Release flarepress: tag HEAD as the next v1.M.P (the commits since the last
# release in the tag's message) and move the rolling v1 tag to it, so every
# app site's next deploy uses it. Run by .github/workflows/release.yml after
# the tests pass: Actions → release → Run workflow, `just release`, or the API.
#
#   tools/release.sh          print what it would tag; change nothing
#   tools/release.sh --push   tag and push
#
# RELEASE_BUMP=patch (default: fixes) or minor (something new a caller can use)
# picks the next number. HEAD already released, or older than the last release
# (a late test run finishing after a newer one): says so and stops, so v1 never
# moves back. RELEASE_WHEN=changed (the automatic release after green tests)
# also stops when nothing a caller uses changed since the last release: the
# files in CALLER_PATHS. A change that breaks a caller is a new major
# (RELEASE_MAJOR=2), which each app moves to on purpose.
# Bash 3.2 safe (macOS /bin/bash).
set -euo pipefail
major=${RELEASE_MAJOR:-1}
bump=${RELEASE_BUMP:-patch}
case "$bump" in patch|minor) ;; *) echo "🛑 RELEASE_BUMP=$bump, want patch or minor" >&2; exit 1 ;; esac
case "$major" in ''|*[!0-9]*) echo "🛑 RELEASE_MAJOR=$major, want a number" >&2; exit 1 ;; esac
when=${RELEASE_WHEN:-always}
case "$when" in always|changed) ;; *) echo "🛑 RELEASE_WHEN=$when, want always or changed" >&2; exit 1 ;; esac
# What a caller of v1 runs: the deploy action, the ci report, the tools both call, and the vendored events files.
CALLER_PATHS="action.yml ci tools events"
head=$(git rev-parse HEAD)
last=$(git tag -l "v$major.*.*" --sort=-v:refname | head -1)
if [ -n "$last" ] && [ "$(git rev-parse "$last^{}")" = "$head" ]; then
  echo "✅ nothing to release: $last is HEAD ($(git rev-parse --short HEAD))"
  exit 0
fi
if [ -n "$last" ] && git merge-base --is-ancestor HEAD "$last"; then
  echo "✅ nothing to release: $(git rev-parse --short HEAD) is already in $last"
  exit 0
fi
# shellcheck disable=SC2086 # CALLER_PATHS is a list of paths
if [ "$when" = changed ] && [ -n "$last" ] && git diff --quiet "$last" HEAD -- $CALLER_PATHS; then
  echo "✅ nothing to release: nothing a caller uses changed since $last"
  exit 0
fi
if [ -z "$last" ]; then
  next="v$major.0.0"; range=HEAD
else
  IFS=. read -r M m p <<EOT
${last#v}
EOT
  if [ "$bump" = minor ]; then next="v$M.$((m + 1)).0"; else next="v$M.$m.$((p + 1))"; fi
  range="$last..HEAD"
fi
notes=$(git log --format='- %s (%h)' "$range")
echo "📦 $next at $(git rev-parse --short HEAD), after ${last:-nothing}:"
echo "$notes"
[ "${1:-}" = --push ] || { echo "(dry run: --push tags and pushes)"; exit 0; }
git tag -a "$next" -m "flarepress ${next#v}" -m "$notes"
git tag -f -a "v$major" -m "flarepress v$major" "$next^{}"
git push -q origin "refs/tags/$next"
git push -q -f origin "refs/tags/v$major"
echo "✅ released $next; v$major moved to it"
