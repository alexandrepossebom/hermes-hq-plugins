"""A chat with more than one pull request or branch (0.9.75): one honest answer, and every item listed."""
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('pr_multi_status_test', Path(__file__).with_name('session_pr_status.py'))
status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)

OWNER = status.Access(token='ghp_' + 'x' * 36)
ENV = {**os.environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.com', 'GIT_COMMITTER_NAME': 't',
       'GIT_COMMITTER_EMAIL': 't@example.com', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull}


@pytest.fixture(autouse=True)
def clear():
    local = status._local()
    for kept in (status._cache, local._repo_cache, local._answer_cache, local._facts, local._by_main, local._static):
        kept.clear()
    yield
    for kept in (status._cache, local._repo_cache, local._answer_cache, local._facts, local._by_main, local._static):
        kept.clear()


def pr(number, state, branch='feature', repo='owner/repo'):
    merged = state == 'merged'
    return {'number': number, 'state': 'open' if state in ('open', 'draft') else 'closed', 'draft': state == 'draft',
            'merged_at': '2026-01-01T00:00:00Z' if merged else None,
            'base': {'repo': {'full_name': repo}}, 'head': {'ref': branch, 'repo': {'full_name': repo}}}


def github(monkeypatch, states, branches=None):
    """GitHub answers *states* (number -> state); ``unknown`` is a failed answer."""
    asked = []

    def api(endpoint, access, tag=None):
        number = int(endpoint.rsplit('/', 1)[1])
        asked.append(number)
        if states[number] == 'unknown':
            raise OSError('offline')
        return pr(number, states[number], (branches or {}).get(number, f'b{number}')), None
    monkeypatch.setattr(status, '_api', api)
    return asked


def created(*numbers):
    return [('owner/repo', n) for n in numbers]


NO_WORK = {'branches': {}, 'commits': []}


@pytest.mark.parametrize('states,expected', [
    ({1: 'open', 2: 'open'}, ('open', 2)),
    ({1: 'merged', 2: 'open'}, ('open', 2)),
    ({1: 'open', 2: 'merged'}, ('open', 1)),           # only one merged: the other is still waiting
    ({1: 'merged', 2: 'closed'}, ('merged', 1)),       # a follow-up given up doesn't undo what landed
    ({1: 'closed', 2: 'merged'}, ('merged', 2)),
    ({1: 'unknown', 2: 'merged'}, ('unknown', 1)),     # the unreadable one may be the one still waiting
    ({1: 'merged', 2: 'unknown'}, ('unknown', 2)),
    ({1: 'merged', 2: 'merged'}, ('merged', 2)),
    ({1: 'closed', 2: 'closed'}, ('closed', 2)),
])
def test_two_pull_requests_one_honest_answer(monkeypatch, states, expected):
    github(monkeypatch, states)
    answer, items = status._resolve_detail(created(*states), NO_WORK, OWNER)
    assert (answer['state'], answer.get('number')) == expected
    assert [(i['number'], i['state'], i['source']) for i in items] == [
        (n, states[n], 'github') for n in sorted(states, reverse=True)]       # every one, newest first
    assert all('_head' not in i for i in items) and '_head' not in answer


def test_the_batch_lists_items_only_when_there_are_some(monkeypatch):
    github(monkeypatch, {1: 'merged', 2: 'open'})
    rows = [{'profile': 'default', 'id': 'two', 'created': created(1, 2), 'work': NO_WORK, 'access': OWNER},
            {'profile': 'default', 'id': 'quiet', 'created': [], 'work': NO_WORK, 'access': OWNER}]
    two, quiet = status.resolve_batch(rows)
    assert two['state'] == 'open' and two['number'] == 2
    assert [(i['number'], i['state']) for i in two['items']] == [(2, 'open'), (1, 'merged')]
    assert two['items'][0] == {'state': 'open', 'repo': 'owner/repo', 'number': 2,
                               'url': 'https://github.com/owner/repo/pull/2', 'source': 'github'}
    assert quiet == {'profile': 'default', 'id': 'quiet', 'state': 'none'}     # the old shape, unchanged


def test_left_out_pull_requests_are_never_taken_as_finished(monkeypatch):
    github(monkeypatch, {n: 'merged' for n in range(2, 14)})
    kept = status._sibling('hermes_hq_session_pr_created', 'session_pr_created.py').Created(created(*range(2, 14)))
    kept.truncated = True                     # #1, older, was left out: it may still be open
    assert status._resolve_detail(kept, NO_WORK, OWNER)[0] == {'state': 'unknown'}
    kept.truncated = False
    assert status._resolve_detail(kept, NO_WORK, OWNER)[0]['state'] == 'merged'
    assert status._resolve_detail([], {**NO_WORK, 'truncated': True}, OWNER)[0] == {'state': 'unknown'}


def test_a_mark_covers_an_open_pull_request_but_its_item_still_says_open(monkeypatch):
    github(monkeypatch, {1: 'open'})
    answer, items = status._resolve_detail(created(1), NO_WORK, OWNER, closed=True)
    assert answer['state'] == 'closed' and answer['manual'] and answer['number'] == 1
    assert items[0]['state'] == 'open'
    github(monkeypatch, {2: 'merged'})
    assert status._resolve_detail(created(2), NO_WORK, OWNER, closed=True)[0]['state'] == 'merged'


def test_fresh_asks_again_about_a_waiting_pull_request_only(monkeypatch):
    asked = github(monkeypatch, {1: 'open', 2: 'merged'})
    status._resolve_detail(created(1, 2), NO_WORK, OWNER)
    assert sorted(asked) == [1, 2]
    status._resolve_detail(created(1, 2), NO_WORK, OWNER, fresh=True)
    assert sorted(asked) == [1, 2]                         # just asked: the cache stands
    for key, (expires, result, tag) in list(status._cache.items()):
        status._cache[key] = (expires - status.FRESH_AFTER, result, tag)     # 15 s later
    status._resolve_detail(created(1, 2), NO_WORK, OWNER)
    assert sorted(asked) == [1, 2]                         # not fresh: three minutes still stand
    status._resolve_detail(created(1, 2), NO_WORK, OWNER, fresh=True)
    assert sorted(asked) == [1, 1, 2]                      # the open one again; merged is final


# ---- a pull request and the chat's own branch it came from --------------------------------------------

def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True, env=ENV).stdout.strip()


def commit(repo, name):
    (repo / name).write_text(name)
    git(repo, 'add', '-A')
    git(repo, 'commit', '-q', '-m', f'add {name}')


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / 'app'
    path.mkdir()
    git(path, 'init', '-q', '-b', 'main')
    commit(path, 'README')
    return path


def listed(repo):
    return status._local().load_repos([{'path': str(repo)}])


def test_a_closed_pull_request_whose_branch_reached_main_is_merged(repo, monkeypatch):
    git(repo, 'switch', '-q', '-c', 'feat/a')
    commit(repo, 'a')
    git(repo, 'switch', '-q', 'main')
    git(repo, 'merge', '-q', '--no-ff', '-m', "Merge branch 'feat/a'", 'feat/a')
    github(monkeypatch, {5: 'closed'}, {5: 'feat/a'})
    work = {'branches': {'feat/a': []}, 'commits': []}
    answer, items = status._resolve_detail(created(5), work, OWNER, listed(repo))
    assert answer == {'state': 'merged', 'branch': 'feat/a', 'repo': 'owner/repo', 'number': 5,
                      'url': 'https://github.com/owner/repo/pull/5'}
    assert len(items) == 1 and items[0]['source'] == 'github'          # one item, not a PR and a branch


def test_branches_and_pull_requests_listed_together(repo, monkeypatch):
    git(repo, 'switch', '-q', '-c', 'feat/done')
    commit(repo, 'a')
    git(repo, 'switch', '-q', 'main')
    git(repo, 'merge', '-q', '--no-ff', '-m', "Merge branch 'feat/done'", 'feat/done')
    git(repo, 'switch', '-q', '-c', 'feat/waiting')
    commit(repo, 'b')
    git(repo, 'switch', '-q', 'main')
    github(monkeypatch, {3: 'merged'}, {3: 'feat/elsewhere'})
    work = {'branches': {'feat/done': [], 'feat/waiting': [], 'feat/nowhere': []}, 'commits': []}
    answer, items = status._resolve_detail(created(3), work, OWNER, listed(repo))
    assert answer == {'state': 'draft', 'branch': 'feat/waiting'}           # waiting work wins
    assert [(i.get('number') or i['branch'], i['state'], i['source']) for i in items] == [
        (3, 'merged', 'github'), ('feat/waiting', 'draft', 'git'), ('feat/done', 'merged', 'git'),
        ('feat/nowhere', 'unknown', 'git')]
    assert all('direct' not in i for i in items)


def test_an_unreadable_pull_request_with_branch_work_keeps_the_branch_answer(repo, monkeypatch):
    """GitHub can't be asked about a private repository's PR (no token): the chat's own branch, which is
    most likely the one it was opened from, still says where the work stands (as before 0.9.75)."""
    git(repo, 'switch', '-q', '-c', 'feat/a')
    commit(repo, 'a')
    git(repo, 'switch', '-q', 'main')
    git(repo, 'merge', '-q', '--no-ff', '-m', "Merge branch 'feat/a'", 'feat/a')
    monkeypatch.setattr(status, '_api', lambda *_: pytest.fail('asked GitHub'))
    answer, items = status._resolve_detail(created(9), {'branches': {'feat/a': []}, 'commits': []},
                                           status.Access(), listed(repo))
    assert answer['state'] == 'merged' and answer['branch'] == 'feat/a'
    assert [(i.get('number'), i['state']) for i in items] == [(9, 'unknown'), (None, 'merged')]
