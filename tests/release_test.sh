#!/usr/bin/env bash
# tools/release.sh in a throwaway repo with a bare origin: the first release is
# v1.0.0, the next v1.0.1 with v1 moved to it, an already released HEAD changes
# nothing, and a dry run tags nothing. Nothing reaches GitHub.
#   bash tests/release_test.sh
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
SH=bash; [ -x /bin/bash ] && SH=/bin/bash
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
fails=0
ok() { echo "✅ $1"; }
no() { echo "❌ $1"; fails=$((fails + 1)); }
G() { git -C "$T/r" -c user.name=t -c user.email=t@t "$@" > /dev/null 2>&1; }
rel() { (cd "$T/r" && GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t "$SH" "$ROOT/tools/release.sh" "$@") > "$T/out" 2>&1; }
remote_tag() { git -C "$T/origin.git" rev-parse -q --verify "refs/tags/$1^{commit}" 2>/dev/null; }

git init -q --bare "$T/origin.git"
git init -q -b main "$T/r"
echo one > "$T/r/a"; G add -A; G commit -m one; G remote add origin "$T/origin.git"; G push -u origin main

rel && grep -q 'v1.0.0' "$T/out" && [ -z "$(git -C "$T/r" tag)" ] && ok "a dry run names v1.0.0 and tags nothing" || { no "a dry run tags nothing"; cat "$T/out"; }
rel --push && [ "$(remote_tag v1.0.0)" = "$(git -C "$T/r" rev-parse HEAD)" ] && [ "$(remote_tag v1)" = "$(git -C "$T/r" rev-parse HEAD)" ] \
  && ok "the first release is v1.0.0, and v1 is on it" || { no "the first release is v1.0.0"; cat "$T/out"; }
rel --push && grep -q 'nothing to release' "$T/out" && ok "an already released HEAD changes nothing" || { no "an already released HEAD changes nothing"; cat "$T/out"; }
echo two > "$T/r/a"; G commit -am "fix: two"; G push
rel --push && [ "$(remote_tag v1.0.1)" = "$(git -C "$T/r" rev-parse HEAD)" ] && [ "$(remote_tag v1)" = "$(git -C "$T/r" rev-parse HEAD)" ] \
  && ok "the next release is v1.0.1, and v1 moves to it" || { no "the next release is v1.0.1"; cat "$T/out"; }
git -C "$T/origin.git" cat-file -p "$(git -C "$T/origin.git" rev-parse refs/tags/v1.0.1)" | grep -q -- '- fix: two' \
  && ok "the tag lists the commits since the last release" || no "the tag lists the commits since the last release"
[ "$(git -C "$T/origin.git" cat-file -t "$(git -C "$T/origin.git" rev-parse refs/tags/v1)")" = tag ] \
  && [ "$(git -C "$T/origin.git" cat-file -p "$(git -C "$T/origin.git" rev-parse refs/tags/v1)" | sed -n 's/^type //p')" = commit ] \
  && ok "v1 is annotated and points at the commit, not at a tag" || no "v1 points at the commit"

# A minor release resets the patch number; a bump that is neither is refused.
echo three > "$T/r/a"; G commit -am "feat: three"; G push
(export RELEASE_BUMP=minor; rel --push) && [ "$(remote_tag v1.1.0)" = "$(git -C "$T/r" rev-parse HEAD)" ] && [ "$(remote_tag v1)" = "$(git -C "$T/r" rev-parse HEAD)" ] \
  && ok "RELEASE_BUMP=minor makes v1.1.0, and v1 moves to it" || { no "RELEASE_BUMP=minor makes v1.1.0"; cat "$T/out"; }
echo four > "$T/r/a"; G commit -am "fix: four"; G push
rel --push && [ "$(remote_tag v1.1.1)" = "$(git -C "$T/r" rev-parse HEAD)" ] && ok "the next patch after v1.1.0 is v1.1.1" || { no "the next patch after v1.1.0 is v1.1.1"; cat "$T/out"; }
echo five > "$T/r/a"; G commit -am "five"; G push
(export RELEASE_BUMP=major; rel --push) && no "RELEASE_BUMP=major is refused" || { [ -z "$(git -C "$T/origin.git" tag -l 'v1.1.2' 'v1.2.0' 'v2*')" ] && ok "RELEASE_BUMP=major is refused and tags nothing" || no "RELEASE_BUMP=major tags nothing"; }

[ "$fails" = 0 ] && echo "🎉 release.sh: all passed" || { echo "💥 release.sh: $fails failed"; exit 1; }
