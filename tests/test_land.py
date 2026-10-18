"""tools/land.sh in throwaway repos: it pulls the branch from GitHub before it
lands (2026-10-17: a main left behind by a push from elsewhere was landed on),
lands a worktree's own commits when given none, waits for another session's
edits with --wait, resolves the list files land.conf names, finishes a stopped
landing with --continue, and with ci=gate moves the branch only after CI passed.
"""
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAND = ROOT / 'tools' / 'land.sh'
CONFIG = (('user.name', 'Test'), ('user.email', 'test@example.com'), ('commit.gpgsign', 'false'))


def git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def commit(repo, path, text, message):
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(text)
    git(repo, 'add', path)
    git(repo, 'commit', '-q', '-m', message)
    return git(repo, 'rev-parse', 'HEAD')


class Land(unittest.TestCase):
    branch = 'main'
    conf = 'test=true\n'

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # The tool as an app finds it: a flarepress folder (not a git checkout here, so it never pulls itself).
        (self.tmp / 'fp' / 'tools').mkdir(parents=True)
        for name in ('land.sh', 'resolve_todo.py', 'resolve_questions.py', 'resolve_words.py'):
            shutil.copy(ROOT / 'tools' / name, self.tmp / 'fp' / 'tools' / name)
        self.origin = self.tmp / 'origin.git'
        subprocess.run(['git', 'init', '-q', '--bare', '-b', self.branch, str(self.origin)], check=True)
        seed = self.clone('seed')
        (seed / 'land.conf').write_text(self.conf)
        (seed / 'a.txt').write_text('one\n')
        (seed / 'b.txt').write_text('one\n')
        (seed / 'TODO.md').write_text('- first\n- second\n')
        git(seed, 'add', '-A')
        git(seed, 'commit', '-q', '-m', 'start')
        git(seed, 'push', '-q', 'origin', f'HEAD:{self.branch}')
        self.repo = self.clone('repo')
        self.start = git(self.repo, 'rev-parse', 'HEAD')
        # An agent's commit on a branch of its own, touching a.txt.
        git(self.repo, 'switch', '-q', '-c', 'agent')
        self.sha = commit(self.repo, 'a.txt', 'one\nagent\n', 'agent change')
        git(self.repo, 'switch', '-q', self.branch)

    def clone(self, name):
        path = self.tmp / name
        subprocess.run(['git', 'clone', '-q', str(self.origin), str(path)], check=True, capture_output=True)
        for k, v in CONFIG:
            git(path, 'config', k, v)
        return path

    def push_elsewhere(self, path='b.txt', text='one\nfrom elsewhere\n', message='pushed from elsewhere'):
        other = self.clone(f'other-{time.monotonic_ns()}')
        commit(other, path, text, message)
        git(other, 'push', '-q', 'origin', self.branch)

    def land(self, *args, cwd=None, **env):
        return subprocess.run(['bash', str(self.tmp / 'fp' / 'tools' / 'land.sh'), *args], cwd=cwd or self.repo,
                              capture_output=True, text=True, env={**os.environ, **env}, timeout=60)

    def remote_head(self):
        return git(self.repo, 'ls-remote', 'origin', f'refs/heads/{self.branch}').split()[0]

    def log(self, ref=None):
        return git(self.repo, 'log', '--format=%s', ref or self.branch).splitlines()


class PullFirst(Land):
    def test_a_branch_behind_github_is_pulled_then_landed_on_top(self):
        self.push_elsewhere()
        out = self.land(self.sha)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('⬇️  pulled 1 commit(s) from GitHub', out.stdout)
        self.assertEqual(self.log()[:3], ['agent change', 'pushed from elsewhere', 'start'])
        self.assertEqual(self.remote_head(), git(self.repo, 'rev-parse', self.branch), 'pushed')
        self.assertIn('from elsewhere', (self.repo / 'b.txt').read_text(), 'the main checkout has the pulled files')
        self.assertIn('agent', (self.repo / 'a.txt').read_text(), 'and the landed ones')

    def test_a_branch_diverged_from_github_stops_it(self):
        self.push_elsewhere()
        commit(self.repo, 'b.txt', 'one\nonly here\n', 'only here')
        here = git(self.repo, 'rev-parse', 'HEAD')
        out = self.land(self.sha)
        self.assertEqual(out.returncode, 1)
        self.assertIn('diverged', out.stderr)
        self.assertEqual(git(self.repo, 'rev-parse', self.branch), here, 'nothing landed')

    def test_a_pull_that_would_overwrite_an_edit_here_stops_it(self):
        self.push_elsewhere()
        (self.repo / 'b.txt').write_text('one\nanother session\n')
        out = self.land(self.sha)
        self.assertEqual(out.returncode, 1)
        self.assertIn('cannot pull', out.stderr)
        self.assertEqual(git(self.repo, 'rev-parse', self.branch), self.start)
        self.assertIn('another session', (self.repo / 'b.txt').read_text(), 'their edit is untouched')

    def test_github_moving_during_a_stopped_landing_puts_the_commits_on_top(self):
        git(self.repo, 'switch', '-q', 'agent')
        theirs = commit(self.repo, 'c.txt', 'two\n', 'c from the agent')
        git(self.repo, 'switch', '-q', self.branch)
        self.push_elsewhere('c.txt', 'three\n', 'c from main')
        out = self.land(theirs)
        self.assertEqual(out.returncode, 1)
        wt = next(Path(self.repo, '.worktrees').iterdir())
        (wt / 'c.txt').write_text('three\ntwo\n')
        git(wt, 'add', 'c.txt')
        subprocess.run(['git', 'cherry-pick', '--continue'], cwd=wt, check=True, capture_output=True,
                       env={**os.environ, 'GIT_EDITOR': 'true'})
        self.push_elsewhere()   # GitHub moves while the conflict was resolved
        out = self.land('--continue', str(wt))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.log()[:3], ['c from the agent', 'pushed from elsewhere', 'c from main'])
        self.assertEqual(self.remote_head(), git(self.repo, 'rev-parse', self.branch))
        self.assertFalse(wt.exists(), 'the landing worktree is removed')


class HeldEdits(Land):
    def test_an_edit_here_stops_it_and_names_wait(self):
        (self.repo / 'a.txt').write_text('one\nanother session\n')
        out = self.land(self.sha)
        self.assertEqual(out.returncode, 1)
        self.assertIn('a.txt', out.stderr)
        self.assertIn('--wait', out.stderr)
        self.assertEqual(git(self.repo, 'rev-parse', self.branch), self.start, 'nothing landed')

    def test_an_edit_to_another_file_does_not_hold_it(self):
        (self.repo / 'b.txt').write_text('one\nanother session\n')
        out = self.land(self.sha)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('agent', (self.repo / 'a.txt').read_text())
        self.assertIn('another session', (self.repo / 'b.txt').read_text(), 'their edit stays')

    def test_wait_gives_up_after_its_limit(self):
        (self.repo / 'a.txt').write_text('one\nanother session\n')
        out = self.land('--wait', self.sha, LAND_WAIT_SECONDS='2', LAND_WAIT_POLL='1')
        self.assertEqual(out.returncode, 1)
        self.assertIn('⏳ waiting for uncommitted edits here: a.txt', out.stdout)
        self.assertIn('still uncommitted', out.stderr)
        self.assertEqual(git(self.repo, 'rev-parse', self.branch), self.start)
        self.assertIn('another session', (self.repo / 'a.txt').read_text(), 'their edit is untouched')

    def test_wait_lands_once_the_edit_is_committed_on_top_of_it(self):
        (self.repo / 'b.txt').write_text('one\nanother session\n')
        (self.repo / 'a.txt').write_text('another session\none\n')   # an edit that merges with the agent's

        def commit_later():
            time.sleep(2)
            git(self.repo, 'commit', '-q', '-am', 'another session')
        t = threading.Thread(target=commit_later)
        t.start()
        out = self.land('--wait', self.sha, LAND_WAIT_POLL='1')
        t.join()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('✅ committed', out.stdout)
        self.assertEqual(self.log()[:2], ['agent change', 'another session'], 'landed on top of what it waited for')
        self.assertEqual((self.repo / 'a.txt').read_text(), 'another session\none\nagent\n')
        self.assertEqual(self.remote_head(), git(self.repo, 'rev-parse', self.branch), 'pushed')


class BranchMode(Land):
    def worktree(self):
        wt = self.tmp / 'agent-wt'
        git(self.repo, 'worktree', 'add', '-q', str(wt), 'agent')
        return wt

    def test_no_commits_named_lands_the_worktrees_own_from_there(self):
        wt = self.worktree()
        commit(wt, 'b.txt', 'one\nsecond agent change\n', 'second agent change')
        self.push_elsewhere('c.txt', 'new\n')
        out = self.land(cwd=wt)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('🛬 landing 2 commit(s) from agent', out.stdout)
        self.assertEqual(self.log()[:3], ['second agent change', 'agent change', 'pushed from elsewhere'])
        self.assertIn('second agent change', (self.repo / 'b.txt').read_text(), 'the main checkout is synced')
        self.assertTrue((self.repo / 'c.txt').exists(), 'and pulled')

    def test_land_conf_is_read_from_the_worktree_it_runs_in(self):
        """2026-10-18: the main checkout had another branch out, with no land.conf, and the CI gate was off."""
        wt = self.worktree()
        commit(wt, 'land.conf', 'test=echo the worktree conf ran\n', 'agent: its own test')
        git(self.repo, 'switch', '-q', '-c', 'elsewhere')
        (self.repo / 'land.conf').unlink()
        git(self.repo, 'commit', '-q', '-am', 'no land.conf on this branch')
        out = self.land(cwd=wt)   # the main checkout's (none) would run `just test`, which fails here
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('the worktree conf ran', out.stdout)

    def test_a_commit_already_landed_is_skipped(self):
        wt = self.worktree()
        self.assertEqual(self.land(self.sha).returncode, 0)
        out = self.land(cwd=wt)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('nothing to land', out.stdout)
        self.assertEqual(self.log().count('agent change'), 1)

    def test_uncommitted_changes_in_the_worktree_stop_it(self):
        wt = self.worktree()
        (wt / 'a.txt').write_text('half done\n')
        out = self.land(cwd=wt)
        self.assertEqual(out.returncode, 1)
        self.assertIn('uncommitted changes', out.stderr)

    def test_on_the_branch_itself_it_asks_for_commits(self):
        out = self.land()
        self.assertEqual(out.returncode, 1)
        self.assertIn('name the commits', out.stderr)


class Conflicts(Land):
    conf = 'test=true\nresolve=TODO.md:todo docs/*.md:words\n'

    def test_a_long_line_in_a_glob_named_file_merges_word_by_word(self):
        self.push_elsewhere('docs/guide.md', 'One two three. Four five six.\n', 'seed guide')
        git(self.repo, 'pull', '-q', '--ff-only')
        git(self.repo, 'switch', '-q', 'agent')
        git(self.repo, 'rebase', '-q', self.branch)
        mine = commit(self.repo, 'docs/guide.md', 'One two three. Four FIVE six.\n', 'agent: five')
        git(self.repo, 'switch', '-q', self.branch)
        self.push_elsewhere('docs/guide.md', 'One TWO three. Four five six.\n', 'main: two')
        out = self.land(mine)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual((self.repo / 'docs/guide.md').read_text(), 'One TWO three. Four FIVE six.\n')

    def test_a_clash_on_the_same_words_stops_for_a_person(self):
        self.push_elsewhere('docs/guide.md', 'One two three.\n', 'seed guide')
        git(self.repo, 'pull', '-q', '--ff-only')
        git(self.repo, 'switch', '-q', 'agent')
        git(self.repo, 'rebase', '-q', self.branch)
        mine = commit(self.repo, 'docs/guide.md', 'One deux three.\n', 'agent: deux')
        git(self.repo, 'switch', '-q', self.branch)
        self.push_elsewhere('docs/guide.md', 'One dos three.\n', 'main: dos')
        out = self.land(mine)
        self.assertEqual(out.returncode, 1)
        self.assertIn('conflicts in: docs/guide.md', out.stderr)
        self.assertTrue(any(Path(self.repo, '.worktrees').iterdir()), 'the worktree is kept for --continue')

    def test_a_list_file_named_in_land_conf_is_resolved(self):
        git(self.repo, 'switch', '-q', 'agent')
        mine = commit(self.repo, 'TODO.md', '- second\n- agent adds\n', 'agent: done with first')
        git(self.repo, 'switch', '-q', self.branch)
        self.push_elsewhere('TODO.md', '- first\n- second\n- main adds\n', 'main adds')
        out = self.land(mine)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual((self.repo / 'TODO.md').read_text(), '- second\n- main adds\n- agent adds\n')

    def test_any_other_conflict_stops_it_and_continue_finishes(self):
        git(self.repo, 'switch', '-q', 'agent')
        mine = commit(self.repo, 'b.txt', 'agent\n', 'agent b')
        git(self.repo, 'switch', '-q', self.branch)
        self.push_elsewhere('b.txt', 'main\n', 'main b')
        out = self.land(self.sha, mine)
        self.assertEqual(out.returncode, 1)
        self.assertIn('conflicts in: b.txt', out.stderr)
        self.assertEqual(self.remote_head(), git(self.repo, 'rev-parse', self.branch), 'nothing pushed')
        wt = next(Path(self.repo, '.worktrees').iterdir())
        (wt / 'b.txt').write_text('main\nagent\n')
        git(wt, 'add', 'b.txt')
        subprocess.run(['git', 'cherry-pick', '--continue'], cwd=wt, check=True, capture_output=True,
                       env={**os.environ, 'GIT_EDITOR': 'true'})
        out = self.land('--continue', str(wt))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.log()[:3], ['agent b', 'agent change', 'main b'])
        self.assertEqual((self.repo / 'b.txt').read_text(), 'main\nagent\n')


class FailingTests(Land):
    conf = 'test=false\n'

    def test_failing_tests_push_nothing(self):
        out = self.land(self.sha)
        self.assertEqual(out.returncode, 1)
        self.assertIn('tests fail', out.stderr)
        self.assertEqual(self.remote_head(), self.start)
        self.assertEqual(git(self.repo, 'rev-parse', self.branch), self.start)


class AfterPick(Land):
    conf = 'test=true\nafter_pick=git rev-list --count HEAD > count.txt; git add count.txt\n'

    def test_what_after_pick_changes_is_amended_into_each_commit(self):
        self.push_elsewhere('c.txt', 'new\n')
        git(self.repo, 'switch', '-q', 'agent')
        second = commit(self.repo, 'b.txt', 'one\nagent too\n', 'agent second')
        git(self.repo, 'switch', '-q', self.branch)
        out = self.land(self.sha, second)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.log()[:3], ['agent second', 'agent change', 'pushed from elsewhere'], 'no extra commits')
        # Each commit carries its own count on the landed branch: 3 and 4 (start, elsewhere, then these).
        self.assertEqual(git(self.repo, 'show', f'{self.branch}~1:count.txt'), '3')
        self.assertEqual(git(self.repo, 'show', f'{self.branch}:count.txt'), '4')
        self.assertEqual(self.remote_head(), git(self.repo, 'rev-parse', self.branch))


class Master(Land):
    branch = 'master'

    def test_the_default_branch_comes_from_github(self):
        out = self.land(self.sha)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('pushed to master', out.stdout)
        self.assertEqual(self.log()[0], 'agent change')


class BadConf(Land):
    conf = 'test=true\ntset=true\n'

    def test_an_unknown_key_is_an_error(self):
        out = self.land(self.sha)
        self.assertEqual(out.returncode, 1)
        self.assertIn('unknown key(s)', out.stderr)
        self.assertIn('tset', out.stderr)


class CiGate(Land):
    conf = 'test=true\nci=gate\n'

    def gh(self, conclusion):
        """A stand-in gh: one run for any commit, which passes or fails; it logs its calls."""
        bin_dir = self.tmp / 'bin'
        bin_dir.mkdir(exist_ok=True)
        (bin_dir / 'gh').write_text(f'#!/bin/sh\necho "$*" >> "{self.tmp}/gh.log"\n'
                                    'case "$1 $2" in "run list") echo 42 ;; "run watch") exit '
                                    f'{0 if conclusion == "success" else 1} ;; esac\n')
        (bin_dir / 'gh').chmod(0o755)
        return {'PATH': f'{bin_dir}{os.pathsep}{os.environ["PATH"]}', 'LAND_CI_POLL': '0'}

    def test_ci_passing_lands_it_and_removes_the_land_branch(self):
        out = self.land(self.sha, **self.gh('success'))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('✅ CI passed', out.stdout)
        self.assertEqual(self.log()[0], 'agent change')
        self.assertEqual(git(self.repo, 'ls-remote', 'origin', 'refs/heads/land/*'), '', 'land/ branch deleted')
        self.assertIn('run watch 42', (self.tmp / 'gh.log').read_text())

    def test_ci_failing_moves_nothing(self):
        out = self.land(self.sha, **self.gh('failure'))
        self.assertEqual(out.returncode, 1)
        self.assertIn('CI failed', out.stderr)
        self.assertEqual(self.remote_head(), self.start)
        self.assertEqual(git(self.repo, 'rev-parse', self.branch), self.start)
        self.assertEqual(git(self.repo, 'ls-remote', 'origin', 'refs/heads/land/*'), '', 'land/ branch deleted')


class Script(unittest.TestCase):
    """land.sh runs on macOS (BSD sed, bash 3.2) and on Linux in cloud sessions (GNU sed)."""
    script = LAND.read_text()

    def drop_done(self):
        start = self.script.index('drop_done() {')
        return self.script[start:self.script.index('\n}\n', start) + 2]

    def run_drop_done(self, lines, commit_):
        with tempfile.TemporaryDirectory() as d:
            todo = Path(d) / 'todo'
            todo.write_text(lines)
            subprocess.run(['bash', '-c', f'{self.drop_done()}\ndrop_done "$1" "$2"', 'x', str(todo), commit_], check=True)
            return todo.read_text()

    def test_it_never_uses_sed_i_which_gnu_and_bsd_read_differently(self):
        self.assertNotRegex(self.script, r'sed\s+-i')

    def test_it_cherry_picks_in_the_zdiff3_style_and_stops_when_a_resolver_fails(self):
        self.assertIn('-c merge.conflictStyle=zdiff3 cherry-pick', self.script)
        self.assertRegex(self.script, r'resolve_\$kind\.py" "\$wt/\$f" \|\| return 1')
        self.assertRegex(self.script, r'if python3 "\$FP/tools/resolve_words\.py" "\$wt/\$f"; then git -C "\$wt" add "\$f"; else stop_here')

    def test_drop_done_removes_the_picked_commits(self):
        self.assertEqual(self.run_drop_done('aaa111\nbbb222\nccc333\n', 'bbb222'), 'ccc333\n')
        self.assertEqual(self.run_drop_done('ccc333\n', 'ccc333').strip(), '')

    def test_drop_done_on_the_first_commit_keeps_the_ones_after_it(self):
        """sed's `1,/re/` looks for its end from line 2, so a commit on line 1 took the whole list (2026-10-16)."""
        self.assertEqual(self.run_drop_done('aaa111\nbbb222\nccc333\n', 'aaa111'), 'bbb222\nccc333\n')


if __name__ == '__main__':
    unittest.main()
