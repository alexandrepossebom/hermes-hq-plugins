"""Where a chat's own branches stand in the owner's repositories (session_pr_local), on real git."""
import importlib.util
import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('pr_local_test', Path(__file__).with_name('session_pr_local.py'))
local = importlib.util.module_from_spec(spec)
spec.loader.exec_module(local)
spec = importlib.util.spec_from_file_location('pr_local_status_test', Path(__file__).with_name('session_pr_status.py'))
status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)

ENV = {**os.environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.com', 'GIT_COMMITTER_NAME': 't',
       'GIT_COMMITTER_EMAIL': 't@example.com', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull}


@pytest.fixture(autouse=True)
def fresh():
    local._repo_cache.clear(); local._answer_cache.clear()
    yield
    local._repo_cache.clear(); local._answer_cache.clear()


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True, env=ENV).stdout.strip()


def commit(repo, name, text='x'):
    (repo / name).write_text(text)
    git(repo, 'add', '-A')
    git(repo, 'commit', '-q', '-m', f'add {name}')
    return git(repo, 'rev-parse', 'HEAD')


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / 'app'
    path.mkdir()
    git(path, 'init', '-q', '-b', 'main')
    commit(path, 'README')
    return path


def listed(repo, **more):
    return local.load_repos([{'path': str(repo), **more}])


def state(work, repos):
    local._repo_cache.clear()
    answer, unplaced = local.local_state(work, repos)
    return (answer or {}).get('state'), unplaced


def branch(name, *shas):
    return {'branches': {name: list(shas)}, 'commits': []}


def test_a_branch_with_commits_of_its_own_is_waiting_then_merged(repo):
    git(repo, 'switch', '-q', '-c', 'feat/a')
    commit(repo, 'a')
    assert state(branch('feat/a'), listed(repo)) == ('draft', 0)
    git(repo, 'switch', '-q', 'main')
    git(repo, 'merge', '-q', '--no-ff', '-m', "Merge branch 'feat/a'", 'feat/a')
    assert state(branch('feat/a'), listed(repo)) == ('merged', 0)


def test_merged_into_main_but_not_public_yet_is_open(repo):
    marker = Path(git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir')) / 'published'
    marker.write_text(git(repo, 'rev-parse', 'main') + '\n')
    git(repo, 'switch', '-q', '-c', 'feat/a')
    commit(repo, 'a')
    git(repo, 'switch', '-q', 'main')
    git(repo, 'merge', '-q', '--no-ff', '-m', "Merge branch 'feat/a'", 'feat/a')
    repos = listed(repo, published_marker='published', public_repo='owner/app')
    answer, _ = local.local_state(branch('feat/a'), repos)
    assert answer == {'state': 'open', 'branch': 'feat/a', 'repo': 'owner/app'}
    commit(repo, 'notes')                                    # main moves on; the marker still lags
    assert state(branch('feat/a'), repos) == ('open', 0)
    marker.write_text(git(repo, 'rev-parse', 'main') + '\n')  # the publisher caught up
    assert state(branch('feat/a'), repos) == ('merged', 0)


def test_a_receipt_names_the_pull_request(repo):
    common = Path(git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
    git(repo, 'switch', '-q', '-c', 'feat/a')
    commit(repo, 'a')
    git(repo, 'switch', '-q', 'main')
    git(repo, 'merge', '-q', '--no-ff', '-m', "Merge branch 'feat/a'", 'feat/a')
    landed = git(repo, 'rev-parse', 'main')
    commit(repo, 'later')
    (common / 'published').write_text(git(repo, 'rev-parse', 'main') + '\n')
    (common / 'receipts.jsonl').write_text(json.dumps({'commit': landed[:8], 'url': 'https://github.com/owner/app/pull/42'}) + '\n'
                                           + 'not json\n' + json.dumps({'commit': 'zz', 'url': 'https://evil.example/pull/1'}) + '\n')
    answer, _ = local.local_state(branch('feat/a'), listed(repo, published_marker='published', receipts='receipts.jsonl'))
    assert answer == {'state': 'merged', 'branch': 'feat/a', 'repo': 'owner/app', 'number': 42,
                      'url': 'https://github.com/owner/app/pull/42'}


def test_a_fast_forwarded_branch_is_merged_and_a_branch_that_never_moved_is_nothing(repo):
    git(repo, 'switch', '-q', '-c', 'feat/ff')
    commit(repo, 'a')
    git(repo, 'switch', '-q', 'main')
    git(repo, 'merge', '-q', '--ff-only', 'feat/ff')         # main moved up to it: the tip is on main's own line
    git(repo, 'branch', 'feat/idle')                          # made, never committed on
    assert state(branch('feat/ff'), listed(repo)) == ('merged', 0)
    assert state(branch('feat/idle'), listed(repo)) == (None, 0)


def test_a_deleted_branch_is_found_by_its_commit_or_by_the_merge_that_names_it(repo):
    git(repo, 'switch', '-q', '-c', 'fix/gone')
    sha = commit(repo, 'a')
    git(repo, 'switch', '-q', 'main')
    git(repo, 'merge', '-q', '--no-ff', '-m', "Merge branch 'fix/gone' (what changed)", 'fix/gone')
    git(repo, 'branch', '-q', '-D', 'fix/gone')
    assert state(branch('fix/gone', sha[:7]), listed(repo)) == ('merged', 0)
    assert state(branch('fix/gone'), listed(repo)) == ('merged', 0)      # no commit id known: the merge's words
    assert state(branch('fix/gon'), listed(repo)) == (None, 1)            # a different name is not it
    assert state(branch('fix/never-existed'), listed(repo)) == (None, 1)


def test_a_deleted_unmerged_branch_is_closed_and_a_renamed_one_still_waits(repo):
    git(repo, 'switch', '-q', '-c', 'fix/dropped')
    sha = commit(repo, 'a')
    git(repo, 'switch', '-q', '-c', 'fix/kept')              # the same commit lives on under another name
    git(repo, 'switch', '-q', 'main')
    git(repo, 'branch', '-q', '-D', 'fix/dropped')
    assert state(branch('fix/dropped', sha[:7]), listed(repo)) == ('draft', 0)
    git(repo, 'branch', '-q', '-D', 'fix/kept')
    answer, _ = local.local_state(branch('fix/dropped', sha[:7]), listed(repo, public_repo='owner/app'))
    assert answer == {'state': 'closed', 'branch': 'fix/dropped', 'repo': 'owner/app'}
    # A merged branch outranks a given-up one: the chat's work did land.
    git(repo, 'switch', '-q', '-c', 'feat/landed')
    commit(repo, 'b')
    git(repo, 'switch', '-q', 'main')
    git(repo, 'merge', '-q', '--no-ff', '-m', "Merge branch 'feat/landed'", 'feat/landed')
    work = {'branches': {'feat/landed': [], 'fix/dropped': [sha[:7]]}, 'commits': []}
    assert state(work, listed(repo)) == ('merged', 0)


def test_a_branch_seen_waiting_is_remembered_after_it_is_deleted(repo):
    """Quiet commits leave no commit id in a chat's history; the tip seen while it waited tells later."""
    tips = {}
    git(repo, 'switch', '-q', '-c', 'feat/a')
    commit(repo, 'a')
    git(repo, 'switch', '-q', '-c', 'feat/b', 'main')
    commit(repo, 'b')
    git(repo, 'switch', '-q', 'main')
    for name in ('feat/a', 'feat/b'):
        assert local.local_state(branch(name), listed(repo), tips)[0]['state'] == 'draft'
    assert len(tips) == 2
    git(repo, 'merge', '-q', '--no-ff', '-m', 'Release 7', 'feat/a')      # the merge doesn't name the branch
    git(repo, 'branch', '-q', '-D', 'feat/a'); git(repo, 'branch', '-q', '-D', 'feat/b')
    local._repo_cache.clear()
    assert local.local_state(branch('feat/a'), listed(repo), tips)[0]['state'] == 'merged'
    assert local.local_state(branch('feat/b'), listed(repo), tips)[0]['state'] == 'closed'
    local._repo_cache.clear()
    assert local.local_state(branch('feat/b'), listed(repo)) == (None, 1)       # never seen: can't be told


def test_squashed_work_counts_as_merged_and_waiting_work_wins(repo):
    git(repo, 'switch', '-q', '-c', 'feat/squash')
    commit(repo, 'a')
    git(repo, 'switch', '-q', 'main')
    git(repo, 'cherry-pick', 'feat/squash')                   # the same change under another commit
    git(repo, 'switch', '-q', '-c', 'feat/next')
    commit(repo, 'b')
    git(repo, 'switch', '-q', 'main')
    assert state(branch('feat/squash'), listed(repo)) == ('merged', 0)
    work = {'branches': {'feat/next': [], 'feat/squash': []}, 'commits': []}
    assert state(work, listed(repo)) == ('draft', 0)          # not done because an older one merged


def test_only_checked_names_reach_git(repo, monkeypatch):
    seen = []
    real = local._git
    monkeypatch.setattr(local, '_git', lambda path, *args: seen.append(args) or real(path, *args))
    for name in ('--upload-pack=x', '-x', 'a b', 'a;rm -rf', '$(id)', 'x..y', 'main', 'HEAD', 'a\nb', ''):
        assert not local._plain_branch(name)
        local._repo_cache.clear()
        assert local.local_state(branch(name), listed(repo)) == (None, 1)
    assert not any(any(bad in str(arg) for bad in ('upload-pack', 'rm -rf', '$(id)')) for args in seen for arg in args)
    assert local.load_repos([{'path': 'relative'}, {'path': '/x', 'main': '--x'}, {'path': '/x', 'published_marker': '../x'},
                             {'path': '/x', 'public_repo': 'nope'}, 'x', None]) == ()


# ---- what a chat's history says it made ------------------------------------------------------------

@pytest.fixture
def db():
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, parent_session_id TEXT)')
    conn.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, '
                 'tool_calls TEXT, tool_call_id TEXT, tool_name TEXT)')
    conn.execute("INSERT INTO sessions VALUES ('s', 'desktop', NULL)")
    yield conn
    conn.close()


_ids = iter(range(1, 10_000))


def run(db, command, output='', sid='s', exit_code=0, name='terminal'):
    call = f'call-{next(_ids)}'
    db.execute('INSERT INTO messages (session_id, role, content, tool_calls) VALUES (?, ?, ?, ?)',
               (sid, 'assistant', '', json.dumps([{'id': call, 'type': 'function', 'function': {
                   'name': name, 'arguments': json.dumps({'command': command})}}])))
    db.execute('INSERT INTO messages (session_id, role, content, tool_call_id, tool_name) VALUES (?, ?, ?, ?, ?)',
               (sid, 'tool', json.dumps({'output': output, 'exit_code': exit_code, 'error': None}), call, name))


def test_branches_come_from_the_chats_own_git_commands(db):
    run(db, 'cd ~/app && git worktree add -q -b feat/one ../wt/one main')            # quiet: the words say it
    run(db, 'git -C ~/app switch -c fix/two', "Switched to a new branch 'fix/two'")
    run(db, 'git commit -m work', '[fix/two 1a2b3c4] work\n 1 file changed')
    run(db, 'git commit -m more', '[feat/three (root-commit) 5d6e7f8] more')
    run(db, 'git checkout -b feat/failed', 'fatal: not a git repository', exit_code=128)
    run(db, 'git log --oneline -3', "abc1234 Merge branch 'feat/someone-elses'")       # read, not made
    run(db, 'echo "[feat/echoed 1234567] x"', '[feat/echoed 1234567] x')               # not a git command
    run(db, 'git commit -m x', '[main 9999999] x')                                     # main is nobody's branch
    run(db, 'git commit -m y', '[detached HEAD aaaaaaa] y')
    assert local.work_from_conn(db, 's') == {
        'branches': {'feat/one': [], 'fix/two': ['1a2b3c4'], 'feat/three': ['5d6e7f8']}, 'commits': ['aaaaaaa']}


def test_no_git_work_is_empty_and_unreadable_history_is_none(db):
    run(db, 'ls -la', 'total 0')
    assert local.work_from_conn(db, 's') == {'branches': {}, 'commits': []}
    assert local.work_from_conn(db, 'missing') is None
    db.execute('DROP TABLE messages')
    assert local.work_from_conn(db, 's') is None


def test_a_subagents_branch_is_its_parents(db):
    db.execute("INSERT INTO sessions VALUES ('child', 'subagent', 's')")
    db.execute("INSERT INTO sessions VALUES ('other', 'desktop', NULL)")
    run(db, 'git switch -c feat/child', "Switched to a new branch 'feat/child'", sid='child')
    run(db, 'git switch -c feat/other', "Switched to a new branch 'feat/other'", sid='other')
    assert list(local.work_from_conn(db, 's')['branches']) == ['feat/child']
    assert list(local.work_from_conn(db, 'other')['branches']) == ['feat/other']


def test_history_is_never_run(db, monkeypatch):
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('ran a command'))
    monkeypatch.setattr(os, 'system', lambda *a: pytest.fail('ran a command'))
    run(db, 'git switch -c "$(rm -rf /)"; git checkout -b `id`; git switch -c ok/name', '')
    assert list(local.work_from_conn(db, 's')['branches']) == ['ok/name']


# ---- one answer per chat ---------------------------------------------------------------------------

def test_one_answer_from_pull_requests_and_branches(repo, monkeypatch):
    repos = listed(repo)
    git(repo, 'switch', '-q', '-c', 'feat/a')
    commit(repo, 'a')
    git(repo, 'switch', '-q', 'main')
    nobody = status.Access()
    status._cache.clear()
    status._local()._repo_cache.clear(); status._local()._answer_cache.clear()
    monkeypatch.setattr(status, '_api', lambda *_: pytest.fail('asked GitHub'))
    assert status._resolve([], branch('feat/a'), nobody, repos) == {'state': 'draft', 'branch': 'feat/a'}
    assert status._resolve([], {'branches': {}, 'commits': []}, nobody, repos) == {'state': 'none'}
    assert status._resolve([], branch('feat/unknown-here'), nobody, repos) == {'state': 'unknown'}   # not "none"
    assert status._resolve(None, None, nobody, repos) == {'state': 'unknown'}
    assert status._resolve([], None, nobody, repos) == {'state': 'unknown'}
    assert status._resolve([], branch('feat/a'), nobody, ()) == {'state': 'none'}                    # no repository listed
    git(repo, 'merge', '-q', '--no-ff', '-m', "Merge branch 'feat/a'", 'feat/a')
    status._local()._repo_cache.clear()      # the route's own copy of the reader keeps refs for a few seconds
    assert status._resolve([], branch('feat/a'), nobody, repos)['state'] == 'merged'
    # A pull request the chat opened itself, still waiting on GitHub, comes before a merged branch.
    owner = status.Access(token='ghp_' + 'x' * 36)
    waiting = {'number': 7, 'state': 'open', 'draft': True, 'merged_at': None,
               'base': {'repo': {'full_name': 'owner/app'}}, 'head': {'ref': 'b', 'repo': {'full_name': 'owner/app'}}}
    monkeypatch.setattr(status, '_api', lambda endpoint, access, tag=None: (waiting, None))
    assert status._resolve([('owner/app', 7)], branch('feat/a'), owner, repos)['number'] == 7
    status._cache.clear()
