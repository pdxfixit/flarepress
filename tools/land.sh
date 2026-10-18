#!/usr/bin/env bash
# Lands commits on an app's main branch (just land, in the app): pulls main from
# GitHub first, cherry-picks the commits in a throwaway worktree, resolves the
# list files every agent appends to, tests the result, pushes, then moves main
# and brings the main checkout's copies of the touched files up to date
# (deleting the ones the commits deleted). GitHub deploys the push.
#
#   land.sh [--wait] [<sha>…]    the commits, oldest first; none: this branch's
#                                commits that main lacks (matched by patch)
#   land.sh --continue <worktree>
#
# Run from anywhere in the app: the main checkout, or an agent's worktree.
#
# Pull first: a push to main from another branch or checkout left main here
# behind GitHub, and a landing built on it was refused at the push, or landed
# on stale files (2026-10-17). Offline, it goes on with what it has.
#
# A conflict it can't resolve stops it with the worktree kept: resolve it
# there (git add, git cherry-pick --continue), then land --continue <dir>
# finishes the same way. By hand, a finished landing once left deleted files
# behind in the main checkout (2026-10-13).
#
# With --wait it first waits for another session to commit its edits to the
# files the commits touch (LAND_WAIT_SECONDS, default an hour; LAND_WAIT_POLL,
# default 10 s), instead of stopping. A hand-written wait loop once passed
# that file list as one word in zsh and landed early (2026-10-16).
#
# Config: land.conf at the app's root, key=value lines, read and never run as
# shell; an unknown key is an error. Every key is optional:
#   branch=main            the branch to land on (default: GitHub's default branch)
#   test=just test         run in the landing worktree before the push (bash -c)
#   ci=gate                push to land/<sha> first and move main only once the
#                          ci_workflow run on that exact commit passed (default none)
#   ci_workflow=test.yml   the workflow ci=gate waits for
#   resolve=TODO.md:todo docs/questions.md:questions
#                          conflicts in these files are resolved by
#                          tools/resolve_<kind>.py (todo or questions)
#   after=just live        what to run once GitHub has deployed the push
#
# Before anything else it pulls its own flarepress checkout, when that is clean
# and on main, so every app lands with the current tool. Bash 3.2 safe.
set -euo pipefail

FP=$(cd "$(dirname "$0")/.." && pwd)

die() { echo "🛑 $*" >&2; exit 1; }

# This tool's own checkout: fast-forward it and start again on the new copy.
if [ -z "${LAND_SELF_PULLED:-}" ] && [ -z "${LAND_NO_SELF_PULL:-}" ]; then
  export LAND_SELF_PULLED=1
  if [ "$(git -C "$FP" symbolic-ref -q --short HEAD 2>/dev/null)" = main ] && [ -z "$(git -C "$FP" status --porcelain 2>/dev/null)" ]; then
    before=$(git -C "$FP" rev-parse HEAD)
    if git -C "$FP" pull -q --ff-only origin main 2>/dev/null; then
      [ "$(git -C "$FP" rev-parse HEAD)" = "$before" ] || { echo "⬇️  flarepress updated to $(git -C "$FP" log --oneline -1 | cat)"; exec bash "$FP/tools/land.sh" "$@"; }
    else
      echo "⚠️  could not pull flarepress ($FP); landing with the copy it has" >&2
    fi
  fi
fi

here=$(git rev-parse --show-toplevel 2>/dev/null) || die "not in a git repo"
# The main checkout: the folder that holds the repo's .git (an agent's worktree
# shares it). Main's files are synced there, and the landing worktree hangs off it.
home=$(cd "$(git rev-parse --git-common-dir)/.." && pwd)

# land.conf: one value by key ('' when absent).
CONF=$home/land.conf
conf() { if [ -f "$CONF" ]; then sed -n "s/^$1=//p" "$CONF" | tail -1; fi; }
if [ -f "$CONF" ]; then
  bad=
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    k=${line%%=*}
    case "$line" in *=*) ;; *) die "$CONF: not key=value: $line" ;; esac
    case "$k" in ''|*[!a-z_]*) die "$CONF: not key=value (no spaces around =): $line" ;; esac
    case "$k" in branch|test|ci|ci_workflow|resolve|after) ;; *) bad="$bad $k" ;; esac
  done < "$CONF"
  [ -z "$bad" ] || die "unknown key(s) in $CONF:$bad"
fi
BRANCH=$(conf branch)
if [ -z "$BRANCH" ]; then
  BRANCH=$(git -C "$home" symbolic-ref -q --short refs/remotes/origin/HEAD 2>/dev/null || true)
  BRANCH=${BRANCH#origin/}
  BRANCH=${BRANCH:-main}
fi
TEST=$(conf test); TEST=${TEST:-just test}
CI=$(conf ci); CI=${CI:-none}
case "$CI" in none|gate) ;; *) die "$CONF: ci=$CI, want none or gate" ;; esac
CI_WORKFLOW=$(conf ci_workflow); CI_WORKFLOW=${CI_WORKFLOW:-test.yml}
RESOLVE=$(conf resolve)
for pair in $RESOLVE; do
  case "${pair##*:}" in todo|questions) ;; *) die "$CONF: resolve=$pair, want <path>:todo or <path>:questions" ;; esac
done
AFTER=$(conf after)

g() { git -C "$home" "$@"; }

# Whether the main checkout has the landing branch checked out (its files are synced then).
home_on_branch() { [ "$(g symbolic-ref -q --short HEAD 2>/dev/null)" = "$BRANCH" ]; }

# The files among $@ with uncommitted edits in the main checkout, one a line.
held() {
  local f
  home_on_branch || return 0
  for f in "$@"; do
    if [ -e "$home/$f" ] && ! g diff --quiet HEAD -- "$f"; then echo "$f"; fi
  done
}

# Waits until held() is empty, saying which files it waits on whenever they change.
wait_free() {
  local limit=${LAND_WAIT_SECONDS:-3600} poll=${LAND_WAIT_POLL:-10} start now busy last=
  start=$(date +%s)
  while busy=$(held "$@"); [ -n "$busy" ]; do
    # shellcheck disable=SC2086,SC2116 # echo joins the lines into one
    if [ "$busy" != "$last" ]; then echo "⏳ waiting for uncommitted edits here: $(echo $busy)"; last=$busy; fi
    now=$(date +%s)
    # shellcheck disable=SC2086,SC2116
    if [ $((now - start)) -ge "$limit" ]; then echo "🛑 still uncommitted after ${limit}s: $(echo $busy); nothing landed" >&2; return 1; fi
    sleep "$poll"
  done
  [ -z "$last" ] || echo "✅ committed; landing on $(g log --oneline -1 "$BRANCH" | cat)"
}

# Brings the landing branch here up to GitHub's: fast-forwards it (the main
# checkout's files too, when it is checked out there). Returns 1 when it could
# not; offline, it says so and leaves the branch as it is.
pull_branch() {
  local behind where
  if ! g fetch -q origin "$BRANCH" 2>/dev/null; then
    echo "⚠️  GitHub not reachable: landing on $BRANCH as it is here" >&2
    return 0
  fi
  g merge-base --is-ancestor "origin/$BRANCH" "$BRANCH" && return 0
  if ! g merge-base --is-ancestor "$BRANCH" "origin/$BRANCH"; then
    echo "🛑 $BRANCH here and GitHub's have diverged ($(g rev-list --count "origin/$BRANCH..$BRANCH") commit(s) only here); sort that out first, nothing landed" >&2
    return 1
  fi
  behind=$(g rev-list --count "$BRANCH..origin/$BRANCH")
  if home_on_branch; then
    # A merge refuses, and changes nothing, when it would overwrite uncommitted edits.
    if ! g merge -q --ff-only "origin/$BRANCH" > /dev/null 2>&1; then
      echo "🛑 cannot pull: uncommitted edits in $home are in files GitHub's $BRANCH changed; commit them, then land again (nothing landed)" >&2
      return 1
    fi
  else
    where=$(g worktree list --porcelain | awk -v b="branch refs/heads/$BRANCH" '/^worktree /{w=substr($0,10)} $0==b{print w}')
    [ -z "$where" ] || { echo "🛑 $BRANCH is checked out in $where and behind GitHub: pull there, then land again" >&2; return 1; }
    g update-ref "refs/heads/$BRANCH" "origin/$BRANCH"
  fi
  echo "⬇️  pulled $behind commit(s) from GitHub: $BRANCH is at $(g log --oneline -1 "$BRANCH" | cat)"
}

# Removes the lines of a todo file up to and including commit $2. Portable:
# GNU sed (Linux, cloud sessions) and BSD sed (macOS) disagree on in-place
# editing, and the BSD form stopped every landing on Linux (2026-10-16).
#
# Not sed's `1,/re/`: it looks for the end from line 2, so a commit on line 1
# wiped the whole list and land --continue landed nothing after it (2026-10-16).
drop_done() {
  local file=$1 c=$2 rest
  rest=$(awk -v c="$c" 'seen { print } $0 == c { seen = 1 }' "$file")
  printf '%s\n' "$rest" > "$file"
}

# Adds a line to the repo's own ignore list (.git/info/exclude, shared by its worktrees) once.
exclude() {
  local file
  file=$(g rev-parse --path-format=absolute --git-path info/exclude)
  mkdir -p "$(dirname "$file")"
  grep -qxF "$1" "$file" 2>/dev/null || echo "$1" >> "$file"
}

# The resolver kind land.conf names for path $1 ('' when none).
kind_for() {
  local pair
  for pair in $RESOLVE; do
    if [ "${pair%:*}" = "$1" ]; then echo "${pair##*:}"; return; fi
  done
}

# Picks the commits listed in the worktree's state file that are not picked yet.
# The resolvers read a conflict's base section, which git writes only in the
# zdiff3 style: a cloud session's git has none set, so it is set here
# (2026-10-18). A resolver that fails stops the landing: once its file was
# staged with the markers still in, and only the tests caught it.
pick_rest() {
  local wt=$1 c left f kind
  while read -r c; do
    [ -n "$c" ] || continue
    if ! git -C "$wt" -c merge.conflictStyle=zdiff3 cherry-pick "$c" > /dev/null 2>&1; then
      left=$(git -C "$wt" diff --name-only --diff-filter=U)
      [ -n "$left" ] || { echo "🛑 $c does not apply (empty, or already on $BRANCH?)" >&2; return 1; }
      for f in $left; do
        kind=$(kind_for "$f")
        if [ -n "$kind" ]; then
          python3 "$FP/tools/resolve_$kind.py" "$wt/$f" || return 1
          git -C "$wt" add "$f"
        else
          # Keep the worktree and what is left to pick, for --continue.
          drop_done "$wt/.land/todo" "$c"
          # shellcheck disable=SC2086,SC2116
          echo "🛑 $c conflicts in: $(echo $left) ($BRANCH unchanged)" >&2
          echo "   Resolve it in $wt (git -C $wt add …; GIT_EDITOR=true git -C $wt cherry-pick --continue)," >&2
          echo "   then: just land-continue $wt" >&2
          return 2
        fi
      done
      GIT_EDITOR=true git -C "$wt" cherry-pick --continue > /dev/null
    fi
    drop_done "$wt/.land/todo" "$c"
  # A copy: drop_done rewrites the file while the loop reads it, and read
  # straight from it the loop stopped after the first commit.
  done < <(cat "$wt/.land/todo")
}

# ci=gate: pushes the commit to land/<sha> and waits for the CI_WORKFLOW run on
# it to pass (LAND_TIMEOUT seconds, default 900), then deletes that branch.
ci_gate() {
  local sha=$1 short branch run deadline
  command -v gh > /dev/null || { echo "🛑 ci=gate needs gh, signed in; push your branch and say so: a session with gh lands it" >&2; return 1; }
  short=$(g rev-parse --short "$sha")
  branch=land/$short
  echo "⬆️  pushing $short to $branch for CI"
  g push -q origin "$sha:refs/heads/$branch"
  deadline=$(( $(date +%s) + ${LAND_TIMEOUT:-900} ))
  run=
  while [ -z "$run" ]; do
    run=$(gh run list --commit "$sha" --workflow "$CI_WORKFLOW" --json databaseId --jq '.[0].databaseId // empty' 2>/dev/null || true)
    [ -n "$run" ] && break
    [ "$(date +%s)" -lt "$deadline" ] || { g push -q origin --delete "$branch" 2>/dev/null || true; echo "🛑 no $CI_WORKFLOW run appeared for $short" >&2; return 1; }
    sleep "${LAND_CI_POLL:-3}"
  done
  echo "⏳ waiting for GitHub Actions run $run on $short"
  if ! gh run watch "$run" --exit-status > /dev/null 2>&1; then
    g push -q origin --delete "$branch" 2>/dev/null || true
    echo "🛑 CI failed on $short; $BRANCH unchanged (gh run view $run --log-failed)" >&2
    return 1
  fi
  g push -q origin --delete "$branch" 2>/dev/null || true
  echo "✅ CI passed on $short"
}

# Catches up with GitHub, tests, pushes, then moves the branch here from the
# recorded start and syncs the main checkout.
finish() {
  local wt=$1 old touched f new
  old=$(cat "$wt/.land/old")
  touched=$(cat "$wt/.land/touched")
  # GitHub, or another landing here, may have moved the branch while it waited
  # or while a conflict was resolved: pull again and put the picked commits on
  # top, so the push is a fast-forward.
  pull_branch || return 1
  if [ "$(g rev-parse "$BRANCH")" != "$old" ]; then
    g merge-base --is-ancestor "$old" "$BRANCH" || { echo "🛑 $BRANCH here no longer contains where this landing began; land again ($wt kept)" >&2; return 1; }
    if ! git -C "$wt" rebase -q "$BRANCH" > /dev/null 2>&1; then
      git -C "$wt" rebase --abort 2>/dev/null || true
      echo "🛑 GitHub's $BRANCH moved, and the commits no longer apply on top of it; land them again ($wt kept)" >&2
      return 1
    fi
    old=$(g rev-parse "$BRANCH")
    echo "$old" > "$wt/.land/old"
  fi
  (cd "$wt" && bash -c "$TEST") || { echo "🛑 tests fail on the landed commits; $BRANCH unchanged ($wt kept)" >&2; return 1; }
  new=$(git -C "$wt" rev-parse HEAD)
  if [ "$CI" = gate ]; then ci_gate "$new" || return 1; fi
  # Push before moving anything here: a refused push leaves this checkout as it was.
  g push -q origin "$new:refs/heads/$BRANCH" || { echo "🛑 the push was refused; $BRANCH unchanged here ($wt kept): land again" >&2; return 1; }
  g update-ref "refs/heads/$BRANCH" "$new" "$old"
  if home_on_branch; then
    g reset -q
    for f in $touched; do
      if g ls-files --error-unmatch "$f" > /dev/null 2>&1; then g checkout -- "$f"; else rm -f "$home/$f"; fi
    done
  fi
  git worktree remove --force "$wt"
  g status --short
  g log --oneline "$old..$BRANCH" | cat
  echo "🚀 pushed to $BRANCH: GitHub deploys it; read that run (gh run list --limit 3)${AFTER:+, then $AFTER}"
}

case "${1:-}" in
  --continue)
    wt=${2:?usage: land.sh --continue <worktree>}
    wt=$(cd "$wt" && pwd)
    [ -d "$wt/.land" ] || die "$wt is not a stopped landing"
    if [ -n "$(git -C "$wt" diff --name-only --diff-filter=U)" ] || git -C "$wt" rev-parse -q --verify CHERRY_PICK_HEAD > /dev/null; then
      die "finish the cherry-pick in $wt first (git add …; GIT_EDITOR=true git -C $wt cherry-pick --continue)"
    fi
    set +e; pick_rest "$wt"; rc=$?; set -e
    [ $rc -eq 0 ] || exit 1
    finish "$wt"
    ;;
  -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
  *)
    wait=0
    if [ "${1:-}" = --wait ]; then wait=1; shift; fi
    case "${1:-}" in -*) die "usage: land.sh [--wait] [<sha>…] | land.sh --continue <worktree>" ;; esac
    if [ $# -eq 0 ]; then
      # This branch's commits: a worktree's own work, committed.
      [ "$(git symbolic-ref -q --short HEAD || true)" != "$BRANCH" ] || die "on $BRANCH itself: name the commits (just land <sha>…), or run it from the branch to land"
      [ -z "$(git status --porcelain --untracked-files=no)" ] || die "uncommitted changes in $here; commit them first"
    fi
    pull_branch || exit 1
    if [ $# -eq 0 ]; then
      # Matched by patch, so commits already on the branch (landed before) are skipped.
      # shellcheck disable=SC2046 # one word a commit
      set -- $(git cherry "$BRANCH" HEAD | sed -n 's/^+ //p')
      [ $# -gt 0 ] || { echo "✅ nothing to land: $BRANCH has every commit of $(git symbolic-ref -q --short HEAD || echo HEAD)"; exit 0; }
      echo "🛬 landing $# commit(s) from $(git symbolic-ref -q --short HEAD || echo HEAD)"
    fi
    # Full SHAs: the worktree reads them from a file, and git cherry gives them in full.
    shas=
    for c in "$@"; do
      full=$(git rev-parse --verify -q "$c^{commit}") || die "not a commit: $c"
      shas="$shas $full"
    done
    # shellcheck disable=SC2086 # one word a commit
    set -- $shas
    # One commit at a time: given two, diff-tree compares them to each other.
    touched=$(for c in "$@"; do git diff-tree --no-commit-id --name-only -r "$c"; done | sort -u)
    # The main checkout must hold nothing unsaved in those files: they are replaced.
    # Another session may be editing them: --wait waits for its commit.
    # shellcheck disable=SC2086 # one word a file
    if [ $wait -eq 1 ]; then
      wait_free $touched || exit 1
    else
      busy=$(held $touched)
      # shellcheck disable=SC2086,SC2116
      [ -z "$busy" ] || die "uncommitted edits in $home in: $(echo $busy); nothing landed (just land --wait waits for them)"
    fi
    # Read only now: what landed while it waited is the base.
    old=$(g rev-parse "$BRANCH")
    # .worktrees/ is this tool's scratch space and .land/ its notes in one: never committed.
    exclude .worktrees/
    exclude .land/
    wt=$home/.worktrees/land-$(date +%s)-$$
    g worktree add -q --detach "$wt" "$BRANCH"
    mkdir "$wt/.land"
    echo "$old" > "$wt/.land/old"
    echo "$touched" > "$wt/.land/touched"
    printf '%s\n' "$@" > "$wt/.land/todo"
    set +e; pick_rest "$wt"; rc=$?; set -e
    if [ $rc -eq 2 ]; then exit 1; fi
    if [ $rc -ne 0 ]; then git -C "$wt" cherry-pick --abort 2>/dev/null || true; git worktree remove --force "$wt"; exit 1; fi
    finish "$wt"
    ;;
esac
