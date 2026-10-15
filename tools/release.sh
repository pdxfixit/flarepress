#!/usr/bin/env bash
# Release flarepress: tag HEAD as the next v1.M.P (the commits since the last
# release in the tag's message) and move the rolling v1 tag to it, so every
# app site's next deploy uses it. Run by .github/workflows/release.yml after
# the tests pass: Actions → release → Run workflow, `just release`, or the API.
#
#   tools/release.sh          print what it would tag; change nothing
#   tools/release.sh --push   tag and push
#
# HEAD already released: says so and stops. A change that breaks a caller is a
# new major (RELEASE_MAJOR=2), which each app moves to on purpose.
# Bash 3.2 safe (macOS /bin/bash).
set -euo pipefail
major=${RELEASE_MAJOR:-1}
head=$(git rev-parse HEAD)
last=$(git tag -l "v$major.*.*" --sort=-v:refname | head -1)
if [ -n "$last" ] && [ "$(git rev-parse "$last^{}")" = "$head" ]; then
  echo "✅ nothing to release: $last is HEAD ($(git rev-parse --short HEAD))"
  exit 0
fi
if [ -z "$last" ]; then
  next="v$major.0.0"; range=HEAD
else
  IFS=. read -r M m p <<EOT
${last#v}
EOT
  next="v$M.$m.$((p + 1))"; range="$last..HEAD"
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
