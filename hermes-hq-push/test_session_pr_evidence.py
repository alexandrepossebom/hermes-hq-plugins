import json
import sqlite3
import subprocess

import pytest
from hermes_state import SessionDB

import session_pr_evidence as subject


@pytest.fixture
def history(tmp_path, monkeypatch):
    import hermes_cli.profiles as profiles
    monkeypatch.setattr(profiles, 'get_profile_dir', lambda name: tmp_path / name)

    def add(profile, session, command=None, output='', name='terminal', args=None, exit_code=0):
        home = tmp_path / profile
        home.mkdir(exist_ok=True)
        db = SessionDB(db_path=home / 'state.db')
        if not db.get_session(session):
            db.create_session(session, source='desktop')
        call_id = f'call-{db.get_session(session)["message_count"]}'
        call = {'id': call_id, 'type': 'function', 'function': {'name': name,
                'arguments': json.dumps(args if args is not None else {'command': command})}}
        db.append_message(session, 'assistant', '', tool_calls=[call])
        db.append_message(session, 'tool', json.dumps({'output': output, 'exit_code': exit_code}),
                          tool_call_id=call_id, tool_name=name)
        db.close()
    return add


def test_create_and_worktree(history):
    history('bot', 's', 'git -C /tmp/example worktree add -b feature/ship /tmp/wt')
    history('bot', 's', 'cd /tmp/wt && gh pr create --title x', 'https://github.com/acme/app/pull/42\n')
    assert subject.evidence('bot', 's') == {'repo': 'acme/app', 'branch': 'feature/ship', 'number': 42}


def test_profile_session_scoped_and_mentions_not_pr(history):
    history('one', 'x', 'git status', 'On branch main\n')
    history('one', 'other', 'gh pr create', 'https://github.com/a/b/pull/10')
    history('two', 'x', 'gh pr create', 'https://github.com/c/d/pull/11')
    history('one', 'x', 'gh pr review 77', 'https://github.com/a/b/pull/77')
    assert subject.evidence('one', 'x') is None
    assert subject.evidence('one', 'other') == {'repo': 'a/b', 'number': 10}
    assert subject.evidence('two', 'x') == {'repo': 'c/d', 'number': 11}
    assert subject.evidence('../one', 'x') is None


def test_commit_and_remote_outputs(history):
    history('one', 'x', 'git -C /tmp/wt remote get-url origin', 'git@github.com:owner/repo.git\n')
    history('one', 'x', 'git -C /tmp/wt commit -m hello', '[feature/long-lived abcdef0] hello\n 1 file changed\n')
    assert subject.evidence('one', 'x') == {'repo': 'owner/repo', 'branch': 'feature/long-lived'}


def test_worktree_existing_branch_and_ambiguous(history):
    history('one', 'x', 'git -C /tmp/repo worktree add /tmp/wt feature/existing', "Preparing worktree (checking out 'feature/existing')\n")
    assert subject.evidence('one', 'x') == {'branch': 'feature/existing'}
    history('one', 'x', 'git -C /tmp/repo worktree add -b feature/other /tmp/wt2')
    assert subject.evidence('one', 'x') == {}


def test_failures_and_unrelated_tool_output(history):
    history('one', 'x', 'gh pr create', 'https://github.com/a/b/pull/8', exit_code=1)
    history('one', 'x', 'echo https://github.com/a/b/pull/9', 'https://github.com/a/b/pull/9')
    assert subject.evidence('one', 'x') == {}


def test_write_file_is_coding_but_prose_is_not(history):
    history('one', 'x', name='write_file', args={'path': '/tmp/main.py', 'content': 'x'})
    history('one', 'y', name='write_file', args={'path': '/tmp/notes.txt', 'content': 'x'})
    assert subject.evidence('one', 'x') == {}
    assert subject.evidence('one', 'y') is None


def test_does_not_interpret_user_text_or_run_commands(history, monkeypatch):
    history('one', 'x', 'echo "git checkout -b fake; gh pr create"', 'https://github.com/a/b/pull/6')
    monkeypatch.setattr('subprocess.run', lambda *a, **kw: pytest.fail('must not run transcript or git'))
    assert subject.evidence('one', 'x') is None


def test_missing_db_no_creation(tmp_path, monkeypatch):
    import hermes_cli.profiles as profiles
    monkeypatch.setattr(profiles, 'get_profile_dir', lambda name: tmp_path / name)
    assert subject.evidence('absent', 'nope') is None
    assert not (tmp_path / 'absent').exists()


def test_global_repo_and_output_attribution(history):
    history('one', 'x', 'gh -R acme/app pr create --head feat/a', 'https://github.com/acme/app/pull/17')
    assert subject.evidence('one', 'x') == {'repo': 'acme/app', 'number': 17, 'branch': 'feat/a'}
    history('one', 'y', 'echo https://github.com/a/b/pull/9; gh pr create',
            'https://github.com/a/b/pull/9')
    assert subject.evidence('one', 'y') == {}


def test_unpaired_active_call_and_patch(tmp_path, history):
    history('one', 'x', name='patch', args={'patch': '*** Begin Patch\n*** Update File: src/app.ts\n*** End Patch'})
    assert subject.evidence('one', 'x') == {}
    db = SessionDB(db_path=tmp_path / 'one' / 'state.db')
    db.create_session('active', source='desktop')
    db.append_message('active', 'assistant', '', tool_calls=[{'id': 'live', 'type': 'function',
        'function': {'name': 'terminal', 'arguments': json.dumps({'command': 'git commit -m x'})}}])
    db.close()
    assert subject.evidence('one', 'active') == {}


def test_existing_worktree_branch_from_command(history):
    history('one', 'x', 'git -C /repo worktree add /work/tree feature/one', 'HEAD is now at aaaaaaa title')
    assert subject.evidence('one', 'x') == {'branch': 'feature/one'}


def make_repo(path, remote):
    subprocess.run(['git', 'init', '-q', str(path)], check=True)
    subprocess.run(['git', '-C', str(path), 'remote', 'add', 'origin', remote], check=True)
    return path


def snapshot(tmp_path, profile, session, cwd, branch):
    with sqlite3.connect(tmp_path / profile / 'state.db') as db:
        db.execute('UPDATE sessions SET cwd=?, git_repo_root=?, git_branch=? WHERE id=?',
                   (str(cwd), str(cwd), branch, session))


def test_snapshot_branch_without_reading_the_recorded_checkout(tmp_path, history, monkeypatch):
    # The recorded cwd is history: its .git/config is never opened, so its remote is not evidence.
    repo = make_repo(tmp_path / 'repo', 'git@github.com:acme/app.git')
    history('one', 'x', name='write_file', args={'path': str(repo / 'src' / 'app.py')})
    snapshot(tmp_path, 'one', 'x', repo, 'feature/historical')
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: pytest.fail('must not execute git'))
    assert subject.evidence('one', 'x') == {'branch': 'feature/historical'}
    history('one', 'x', f'git -C {repo} remote get-url origin', 'git@github.com:acme/app.git\n')
    assert subject.evidence('one', 'x') == {'repo': 'acme/app', 'branch': 'feature/historical'}


def test_recorded_git_workdir_and_worktree(tmp_path, history):
    repo = make_repo(tmp_path / 'repo', 'https://github.com/acme/app.git')
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Example', '-c', 'user.email=x@example.invalid',
                    'commit', '--allow-empty', '-qm', 'initial'], check=True)
    worktree = tmp_path / 'worktree'
    subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '-qb', 'feature/a', str(worktree)], check=True)
    history('one', 'x', f'git -C {worktree} commit -m x', '[feature/a abcdef0] x')
    assert subject.evidence('one', 'x') == {'branch': 'feature/a'}
    history('one', 'x', f'git -C {worktree} remote get-url origin', 'https://github.com/acme/app.git')
    assert subject.evidence('one', 'x') == {'repo': 'acme/app', 'branch': 'feature/a'}
    history('one', 'y', args={'command': 'git checkout -b feature/b', 'workdir': str(repo)})
    assert subject.evidence('one', 'y') == {'branch': 'feature/b'}
    history('one', 'z', f'git -C {repo} checkout feature/existing', "Switched to branch 'feature/existing'")
    history('one', 'z', name='write_file', args={'path': str(repo / 'app.py')})
    assert subject.evidence('one', 'z') == {'branch': 'feature/existing'}


def test_unrelated_repo_and_branch_never_join(tmp_path, history):
    repo_a = make_repo(tmp_path / 'a', 'https://github.com/acme/a.git')
    repo_b = make_repo(tmp_path / 'b', 'https://github.com/acme/b.git')
    history('one', 'x', f'git -C {repo_a} remote get-url origin', 'https://github.com/acme/a.git')
    history('one', 'x', f'git -C {repo_b} commit -m x', '[feature/b abcdef0] x')
    assert subject.evidence('one', 'x') == {}
    # A file written elsewhere names no repository (its checkout is never read): no repo is joined.
    history('one', 'y', name='write_file', args={'path': str(repo_b / 'src.py')})
    snapshot(tmp_path, 'one', 'y', repo_a, 'feature/a')
    assert 'repo' not in subject.evidence('one', 'y')
    history('one', 'z', name='patch', args={'patch': f'*** Begin Patch\n*** Update File: {repo_b}/app.py\n*** End Patch'})
    snapshot(tmp_path, 'one', 'z', repo_a, 'feature/a')
    assert 'repo' not in subject.evidence('one', 'z')
    # Git work in another checkout than the branch's is a second identity.
    history('one', 'w', f'git -C {repo_b} commit -m x', 'nothing to commit', exit_code=1)
    snapshot(tmp_path, 'one', 'w', repo_a, 'feature/a')
    assert subject.evidence('one', 'w') == {}


def test_noncoding_snapshot_and_inaccessible_history(tmp_path, history):
    repo = make_repo(tmp_path / 'a', 'https://github.com/acme/a.git')
    history('one', 'x', 'git status', 'On branch feature/a')
    snapshot(tmp_path, 'one', 'x', repo, 'feature/a')
    assert subject.evidence('one', 'x') is None
    with sqlite3.connect(tmp_path / 'one' / 'state.db') as db:
        db.execute('DROP TABLE messages')
    assert subject.evidence('one', 'x') is None


def test_large_sessions_fail_closed_and_large_prose_is_not_fetched(tmp_path, history):
    history('one', 'large', 'git commit -m x', '[feature/a abcdef0] x')
    with sqlite3.connect(tmp_path / 'one' / 'state.db') as db:
        db.executemany("INSERT INTO messages (session_id, role, content, timestamp) VALUES ('large', 'tool', ?, 0)",
                       [('x',)] * subject._MAX_ROWS)
    assert subject.evidence('one', 'large') == {'branch': 'feature/a'}  # unrelated results ignored
    history('one', 'oversized', 'git commit -m x', 'x' * (subject._MAX_FIELD + 1))
    assert subject.evidence('one', 'oversized') == {}
    history('one', 'prose', 'git status', 'ok')
    with sqlite3.connect(tmp_path / 'one' / 'state.db') as db:
        db.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES ('prose','user',?,0)",
                   ('x' * 1000000,))
    assert subject.evidence('one', 'prose') is None


def test_no_path_from_history_is_dereferenced(tmp_path, history, monkeypatch):
    """Every path a transcript or the session row records is compared as text; none is opened,
    stat'ed, listed or resolved (a FIFO, a network mount or another user's checkout stays untouched)."""
    trap = tmp_path / 'trap'
    repo = make_repo(trap / 'repo', 'https://github.com/acme/secret.git')
    (trap / 'wt').mkdir()
    (trap / 'wt' / '.git').write_text(f'gitdir: {trap}/elsewhere\n')
    history('one', 'x', f'cd {trap}/wt && git -C ../repo worktree add -b feature/t {trap}/wt2')
    history('one', 'x', name='write_file', args={'path': str(trap / 'repo' / 'a.py')})
    history('one', 'x', name='patch', args={'patch': f'*** Update File: {trap}/link/b.py'})
    history('one', 'x', args={'command': 'git commit -m x', 'workdir': str(trap / 'repo')}, output='[feature/t 1234567] x')
    history('one', 'x', 'git -C ~/repo commit -m y', '[feature/t 1234567] y')
    snapshot(tmp_path, 'one', 'x', repo, 'feature/t')
    monkeypatch.setenv('HERMES_HQ_PUBLIC_PR_RECEIPTS', '1')
    db = sqlite3.connect((tmp_path / 'one' / 'state.db').as_uri() + '?mode=ro', uri=True, isolation_level=None)
    db.execute('BEGIN')
    touched = []
    import builtins
    import os
    import os.path
    def spy(name, real):
        def wrapper(path, *args, **kwargs):
            if str(trap) in os.fsdecode(path) if isinstance(path, (str, bytes, os.PathLike)) else False:
                touched.append((name, str(path)))
            return real(path, *args, **kwargs)
        return wrapper
    for module, name in [(os, 'open'), (os, 'stat'), (os, 'lstat'), (os, 'readlink'), (os, 'scandir'),
                         (os, 'listdir'), (builtins, 'open'), (os.path, 'realpath'), (os.path, 'expanduser')]:
        monkeypatch.setattr(module, name, spy(name, getattr(module, name)))
    result = subject.evidence_from_conn(db, 'x', receipt_roots=())
    monkeypatch.undo()
    db.close()
    assert touched == []
    assert result == {'branch': 'feature/t'}


def test_evidence_reads_the_callers_snapshot(tmp_path, history):
    history('one', 'x', 'git status', 'On branch main')
    db = sqlite3.connect((tmp_path / 'one' / 'state.db').as_uri() + '?mode=ro', uri=True, isolation_level=None)
    db.execute('BEGIN')
    db.execute('SELECT count(*) FROM sessions').fetchone()
    history('one', 'x', 'gh pr create', 'https://github.com/a/b/pull/10')   # committed after the snapshot
    assert subject.evidence_from_conn(db, 'x') is None
    db.execute('ROLLBACK')
    assert subject.evidence_from_conn(db, 'x') == {'repo': 'a/b', 'number': 10}
    db.close()
