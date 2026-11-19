"""The owner's "closed" mark and the repositories linked per project (session_pr_marks and its routes)."""
import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from hermes_state import SessionDB

spec = importlib.util.spec_from_file_location('pr_marks_test', Path(__file__).with_name('session_pr_marks.py'))
marks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(marks)
spec = importlib.util.spec_from_file_location('pr_marks_status_test', Path(__file__).with_name('session_pr_status.py'))
status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)

ENV = {**os.environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.com', 'GIT_COMMITTER_NAME': 't',
       'GIT_COMMITTER_EMAIL': 't@example.com', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull}


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True, env=ENV).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / 'app'
    path.mkdir()
    git(path, 'init', '-q', '-b', 'main')
    (path / 'README').write_text('x')
    git(path, 'add', '-A'); git(path, 'commit', '-q', '-m', 'first')
    return path.resolve()


def test_marks_are_kept_and_taken_back(tmp_path):
    assert marks.closed(tmp_path) == frozenset()
    marks.set_closed(tmp_path, 'default', 's-1', True)
    marks.set_closed(tmp_path, 'ivy', 's-1', True)
    assert marks.closed(tmp_path) == {'default/s-1', 'ivy/s-1'}
    assert oct((tmp_path / marks.MARKS_NAME).stat().st_mode & 0o777) == '0o600'
    marks.set_closed(tmp_path, 'default', 's-1', False)
    assert marks.closed(tmp_path) == {'ivy/s-1'}
    (tmp_path / marks.MARKS_NAME).write_text('not json')
    assert marks.closed(tmp_path) == frozenset()


def test_a_mark_covers_what_is_not_merged_and_never_what_is():
    nobody = status.Access()
    assert status._resolve([], {'branches': {}, 'commits': []}, nobody, (), None, True) == {'state': 'closed', 'manual': True}
    assert status._resolve(None, None, nobody, (), None, True) == {'state': 'closed', 'manual': True}
    assert status._resolve([], {'branches': {}, 'commits': []}, nobody, (), None, False) == {'state': 'none'}
    for state, covered in (('draft', True), ('closed', True), ('open', False), ('merged', False)):
        import unittest.mock as mock
        with mock.patch.object(status, '_computed', lambda *a, state=state: {'state': state, 'branch': 'b'}):
            answer = status._resolve([], {}, nobody, (), None, True)
        assert (answer == {'state': 'closed', 'branch': 'b', 'manual': True}) is covered
        assert covered or answer == {'state': state, 'branch': 'b'}


def test_linking_asks_git_and_takes_only_a_repository(tmp_path, repo):
    data = tmp_path / 'data'
    data.mkdir()
    for bad, why in (('relative/path', 'invalid_path'), (str(tmp_path / 'missing'), 'not_a_repository'),
                     (str(tmp_path), 'not_a_repository'), ('/x\x00y', 'invalid_path')):
        with pytest.raises(ValueError, match=why):
            marks.link(data, '/proj', bad)
    with pytest.raises(ValueError, match='invalid_public_repo'):
        marks.link(data, '/proj', str(repo), 'not a repo')
    assert marks.links(data) == {}
    assert marks.link(data, '/proj', str(repo) + '/') == {
        'project': '/proj', 'path': str(repo), 'main': 'main', 'public_repo': None, 'mirror': False}
    git(repo, 'remote', 'add', 'origin', 'git@github.com:Owner/App.git')
    assert marks.link(data, '/proj', str(repo))['public_repo'] == 'Owner/App'           # read from its remote
    # A publisher's marker in the git folder: main is published elsewhere, so the remote is not the public copy.
    (repo / '.git' / marks.MARKER).write_text(git(repo, 'rev-parse', 'main') + '\n')
    linked = marks.link(data, '/proj', str(repo), 'owner/public')
    assert linked == {'project': '/proj', 'path': str(repo), 'main': 'main', 'public_repo': 'owner/public', 'mirror': True}
    assert marks.link(data, '/proj', str(repo))['public_repo'] is None
    # A worktree's folder links the repository it belongs to.
    git(repo, 'worktree', 'add', '-q', '-b', 'feat/x', str(tmp_path / 'wt'))
    assert marks.link(data, '/other', str(tmp_path / 'wt'))['path'] == str(repo)
    assert [r['path'] for r in marks.repos(data)] == [str(repo)]                        # one folder, asked once
    marks.unlink(data, '/proj'); marks.unlink(data, '/proj')
    assert list(marks.links(data)) == ['/other']


def test_a_linked_repository_is_read_and_its_public_copy_may_be_asked(tmp_path, repo):
    data = tmp_path / 'data'
    data.mkdir()
    (data / status.CONFIG_NAME).write_text(json.dumps({'repos': [{'path': '/by/hand'}, {'path': str(repo)}]}))
    marks.link(data, '/proj', str(repo), 'Owner/Public')
    config = status.load_config(data)
    assert [r['path'] for r in config['repos']] == [str(repo), '/by/hand']
    assert config['public_repos'] == {'owner/public'}
    assert marks.listed(data, config) == [
        {'project': '/proj', 'path': str(repo), 'main': 'main', 'public_repo': 'Owner/Public', 'mirror': False},
        {'project': None, 'path': '/by/hand', 'main': 'main', 'public_repo': None, 'mirror': False}]


def _api(tmp_path, monkeypatch, name):
    import test_tasks
    monkeypatch.setenv('HERMES_HQ_PUSH_DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setenv('HERMES_HQ_PUSH_RELAY_URL', 'off')
    monkeypatch.setattr(test_tasks.push, '_watcher_started', True)
    api_spec = importlib.util.spec_from_file_location(name, Path(__file__).parent / 'dashboard/plugin_api.py')
    api = importlib.util.module_from_spec(api_spec)
    api_spec.loader.exec_module(api)
    monkeypatch.setattr(api, '_loop_watched', True)
    return api


def test_routes_mark_only_the_callers_chats_and_link_only_for_the_owner(tmp_path, monkeypatch, repo):
    from hermes_cli import profiles, web_server_cron
    from hermes_cli.plugin_host_http_auth import with_hosted_admission
    from gateway.owner_identity import host_owner_subject
    home = tmp_path / 'default'
    home.mkdir()
    db = SessionDB(db_path=home / 'state.db')
    for sid, user in (('mine', host_owner_subject()), ('foreign', 'telegram:42')):
        db.create_session(sid, source='desktop', user_id=user)
        db.end_session(sid, end_reason='done')
    db.close()
    monkeypatch.setattr(profiles, 'get_profile_dir', lambda name: home)
    monkeypatch.setattr(web_server_cron, '_cron_profile_home', lambda name: (name, home))
    api = _api(tmp_path, monkeypatch, 'pr_marks_api_test')
    monkeypatch.setattr(api.session_pr_status, '_api', lambda *_: pytest.fail('asked GitHub'))
    assert api.status()['sessionPRMarks'] is True and api.status()['sessionPRRepos'] is True
    app = FastAPI()
    app.include_router(api.router)
    owner = TestClient(with_hosted_admission(app, True))       # as a plugin host sees an admitted request
    nobody = TestClient(with_hosted_admission(app, False))
    rows = {'sessions': [{'profile': 'default', 'id': 'mine'}, {'profile': 'default', 'id': 'foreign'}]}
    assert owner.post('/sessions/pr-status', json=rows).json() == {'sessions': [{'profile': 'default', 'id': 'mine', 'state': 'none'}]}
    # Not admitted: nothing is marked, nothing is listed, nothing is linked.
    assert nobody.put('/sessions/pr-mark', json={'profile': 'default', 'id': 'mine', 'closed': True}).status_code == 404
    assert nobody.get('/sessions/pr-repos').status_code == 401
    assert nobody.put('/sessions/pr-repos', json={'project': '/p', 'path': str(repo)}).status_code == 401
    assert nobody.delete('/sessions/pr-repos', params={'project': '/p'}).status_code == 401
    assert not (tmp_path / 'data' / 'session-pr-marks.json').exists() and not (tmp_path / 'data' / 'session-pr-links.json').exists()
    # The owner: a mark on their own chat only.
    assert owner.put('/sessions/pr-mark', json={'profile': 'default', 'id': 'foreign', 'closed': True}).status_code == 404
    assert owner.put('/sessions/pr-mark', json={'profile': 'default', 'id': 'missing', 'closed': True}).status_code == 404
    assert owner.put('/sessions/pr-mark', json={'profile': '../x', 'id': 'mine', 'closed': True}).status_code == 422
    assert owner.put('/sessions/pr-mark', json={'profile': 'default', 'id': 'mine', 'closed': 'yes'}).status_code == 422
    assert owner.put('/sessions/pr-mark', json={'profile': 'default', 'id': 'mine', 'closed': True}).json() == {'ok': True, 'closed': True}
    assert owner.post('/sessions/pr-status', json=rows).json() == {'sessions': [
        {'profile': 'default', 'id': 'mine', 'state': 'closed', 'manual': True}]}
    assert owner.put('/sessions/pr-mark', json={'profile': 'default', 'id': 'mine', 'closed': False}).json() == {'ok': True, 'closed': False}
    assert owner.post('/sessions/pr-status', json=rows).json()['sessions'][0]['state'] == 'none'
    # Links.
    assert owner.get('/sessions/pr-repos').json() == {'repos': []}
    assert owner.put('/sessions/pr-repos', json={'project': '/p', 'path': str(tmp_path)}).json() == {'detail': 'not_a_repository'}
    assert owner.put('/sessions/pr-repos', json={'project': '/p', 'path': str(tmp_path)}).status_code == 422
    linked = owner.put('/sessions/pr-repos', json={'project': '/p', 'path': str(repo), 'public_repo': None})
    assert linked.status_code == 200 and linked.json() == {'repo': {
        'project': '/p', 'path': str(repo), 'main': 'main', 'public_repo': None, 'mirror': False}}
    assert owner.get('/sessions/pr-repos').json() == {'repos': [linked.json()['repo']]}
    assert owner.delete('/sessions/pr-repos', params={'project': '/p'}).json() == {'ok': True}
    assert owner.get('/sessions/pr-repos').json() == {'repos': []}


def test_links_in_the_serving_process_need_a_sign_in(tmp_path, monkeypatch, repo):
    """Served by Hermes itself (not a plugin host): a signed-in login is the owner; no login is refused."""
    api = _api(tmp_path, monkeypatch, 'pr_marks_inprocess_test')

    def login(request: Request):
        if request.headers.get('authorization') == 'Bearer valid':
            request.state.session = SimpleNamespace(user_id='phone', provider='basic', issuer='')
    app = FastAPI()
    app.state.auth_required = True
    app.include_router(api.router, dependencies=[Depends(login)])
    client = TestClient(app)
    assert client.get('/sessions/pr-repos').status_code == 401
    assert client.put('/sessions/pr-repos', json={'project': '/p', 'path': str(repo)}).status_code == 401
    signed = {'Authorization': 'Bearer valid'}
    assert client.put('/sessions/pr-repos', json={'project': '/p', 'path': str(repo)}, headers=signed).status_code == 200
    assert [r['project'] for r in client.get('/sessions/pr-repos', headers=signed).json()['repos']] == ['/p']
