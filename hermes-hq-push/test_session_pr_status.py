import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('pr_status_test', Path(__file__).with_name('session_pr_status.py'))
status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)


@pytest.fixture(autouse=True)
def clear_cache():
    status._cache.clear()
    yield
    status._cache.clear()


def pr(number=1, state='open', draft=False, merged_at=None, branch='feature', repo='owner/repo'):
    return {'number': number, 'state': state, 'draft': draft, 'merged_at': merged_at,
            'base': {'repo': {'full_name': repo}}, 'head': {'ref': branch, 'repo': {'full_name': repo}}}


@pytest.mark.parametrize('data,expected', [
    (pr(), 'open'), (pr(draft=True), 'draft'), (pr(state='closed'), 'closed'),
    (pr(state='closed', merged_at='2026-01-01'), 'merged'),
])
def test_explicit_number_states(monkeypatch, data, expected):
    monkeypatch.setattr(status, '_evidence', lambda *_: {'repo': 'owner/repo', 'number': 1})
    monkeypatch.setattr(status, '_api', lambda endpoint: data)
    row = status.resolve('default', 'session-1')
    assert row == {'profile': 'default', 'id': 'session-1', 'state': expected, 'repo': 'owner/repo',
                   'branch': 'feature', 'number': 1, 'url': 'https://github.com/owner/repo/pull/1'}


def test_noncode_unknown_and_errors(monkeypatch):
    monkeypatch.setattr(status, '_evidence', lambda *_: None)
    assert status.resolve('default', 'a') is None
    monkeypatch.setattr(status, '_evidence', lambda *_: {})
    assert status.resolve('default', 'b')['state'] == 'unknown'
    def fail(*_):
        raise OSError('do not expose')
    monkeypatch.setattr(status, '_evidence', fail)
    assert status.resolve('default', 'c') is None


@pytest.mark.parametrize('info', [
    {'repo': 'https://evil.example/o/r', 'number': 1}, {'repo': '../../evil', 'number': 1},
    {'repo': '-o/repo', 'number': 1}, {'repo': 'owner/repo?x=y', 'number': 1},
    {'repo': 'owner/repo', 'number': True}, {'repo': 'owner/repo', 'number': -1},
    {'repo': 'owner/repo', 'branch': 'bad\nbranch'},
])
def test_untrusted_evidence_never_invokes_gh(monkeypatch, info):
    monkeypatch.setattr(status, '_evidence', lambda *_: info)
    monkeypatch.setattr(status, '_api', lambda *_: pytest.fail('called GitHub'))
    assert status.resolve('default', 'a')['state'] == 'unknown'


def test_forks_and_pagination_are_ambiguous(monkeypatch):
    monkeypatch.setattr(status, '_evidence', lambda *_: {'repo': 'owner/repo', 'branch': 'feature'})
    monkeypatch.setattr(status, '_api', lambda _: [pr(1), pr(2)])
    assert status.resolve('default', 'a')['state'] == 'unknown'
    monkeypatch.setattr(status, '_api', lambda _: [pr(n, branch='other') for n in range(1, 101)])
    assert status.resolve('default', 'b')['state'] == 'unknown'
    monkeypatch.setattr(status, '_api', lambda _: [])
    assert status.resolve('default', 'c') == {'profile': 'default', 'id': 'c', 'state': 'unknown', 'repo': 'owner/repo', 'branch': 'feature'}


@pytest.mark.parametrize('fork,state', [(False, 'none'), (True, 'unknown')])
def test_empty_head_in_fork_vs_repository(monkeypatch, fork, state):
    def lookup(endpoint):
        if endpoint == '/repos/owner/repo':
            return {'full_name': 'owner/repo', 'fork': fork, 'default_branch': 'main'}
        assert 'head=owner%3Afeature' in endpoint
        return []
    monkeypatch.setattr(status, '_api', lookup)
    info = {'repo': 'owner/repo', 'branch': 'feature'}
    assert status._resolve_evidence(info) == {'state': state, **info}


def test_explicit_number_must_match_repo_and_branch(monkeypatch):
    monkeypatch.setattr(status, '_evidence', lambda *_: {'repo': 'owner/repo', 'number': 1, 'branch': 'feature'})
    monkeypatch.setattr(status, '_api', lambda _: pr(repo='other/repo'))
    assert status.resolve('default', 'a')['state'] == 'unknown'
    monkeypatch.setattr(status, '_api', lambda _: pr(branch='other'))
    assert status.resolve('default', 'b')['state'] == 'unknown'


def test_cache_ttl_and_validation(monkeypatch):
    now = [1.0]
    calls = []
    monkeypatch.setattr(status.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(status, '_evidence', lambda *args: calls.append(args))
    status.resolve('default', 'a'); status.resolve('default', 'a')
    assert len(calls) == 1
    now[0] += status.TTL + 1
    status.resolve('default', 'a')
    assert len(calls) == 2
    assert status.resolve('../bad', 'a')['state'] == 'unknown'
    assert status.resolve('default', '../bad')['state'] == 'unknown'
    assert len(calls) == 2
    with pytest.raises(ValueError):
        status.resolve_batch([{'profile': 'default', 'id': 'a'}] * 33)


def test_gh_explicit_host_no_shell_and_timeout(monkeypatch):
    seen = []
    def run(args, **kwargs):
        seen.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, json.dumps(pr()).encode())
    monkeypatch.setattr(status.subprocess, 'run', run)
    assert status._resolve_evidence({'repo': 'owner/repo', 'number': 1})['state'] == 'open'
    args, kwargs = seen[0]
    assert args == ['gh', 'api', '--hostname', 'github.com', '/repos/owner/repo/pulls/1']
    assert kwargs['timeout'] == status._TIMEOUT and kwargs['check'] is True
    assert 'shell' not in kwargs


def test_failed_gh_is_unknown(monkeypatch):
    monkeypatch.setattr(status, '_evidence', lambda *_: {'repo': 'owner/repo', 'number': 1})
    def fail(*_):
        raise subprocess.TimeoutExpired('gh', 8)
    monkeypatch.setattr(status, '_api', fail)
    assert status.resolve('default', 'a')['state'] == 'unknown'


def test_http_route_and_validation(tmp_path, monkeypatch):
    from fastapi import FastAPI, Depends, HTTPException, Header, Request
    from types import SimpleNamespace
    from gateway.session_contract import SessionActor
    from gateway.config import Platform
    from hermes_cli import web_server_sessions, profiles
    from fastapi.testclient import TestClient
    import test_tasks
    monkeypatch.setenv('HERMES_HQ_PUSH_DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setenv('HERMES_HQ_PUSH_RELAY_URL', 'off')
    monkeypatch.setattr(test_tasks.push, '_watcher_started', True)
    api_spec = importlib.util.spec_from_file_location('pr_status_api_test', Path(__file__).parent / 'dashboard/plugin_api.py')
    api = importlib.util.module_from_spec(api_spec)
    api_spec.loader.exec_module(api)
    monkeypatch.setattr(api, '_loop_watched', True)
    monkeypatch.setattr(api.session_pr_status, 'resolve_batch', lambda rows: [{**r, 'state': 'none'} for r in rows])
    calls = []
    home = tmp_path / 'default'
    class Owner:
        db = SimpleNamespace(db_path=str(home / 'state.db'))
        sessions = {'s-1': SimpleNamespace(source=SimpleNamespace(platform=Platform.LOCAL, user_id='alice'))}
        def authorize(self, actor, ref, capability):
            calls.append((actor, ref, capability))
            if actor.subject != 'alice' or ref.session_id != 's-1':
                raise ValueError('not readable')
            assert actor.capabilities == frozenset({'session:read'})
    owner = Owner()
    from gateway import session_authorities, session_history_mount
    from hermes_state import SessionDB
    home.mkdir()
    db = SessionDB(db_path=home / 'state.db')
    db.close()
    monkeypatch.setattr(session_authorities, 'require_serving', lambda _: None)
    monkeypatch.setattr(session_history_mount, 'is_history_mount', lambda _: False)
    monkeypatch.setattr(profiles, 'get_profile_dir', lambda _: home)
    monkeypatch.setattr(web_server_sessions, '_session_mutation_context', lambda req, profile:
                        (owner, SessionActor(req.state.who, str(home), frozenset({'session:read', 'session:operator'}), 'http')))
    def auth(request: Request, authorization: str = Header(default='')):
        if authorization not in ('Bearer alice', 'Bearer bob'):
            raise HTTPException(401)
        request.state.who = authorization.split()[1]
    app = FastAPI()
    app.include_router(api.router, prefix='/api/plugins/hermes-hq-push', dependencies=[Depends(auth)])
    client = TestClient(app)
    url = '/api/plugins/hermes-hq-push/sessions/pr-status'
    payload = {'sessions': [{'profile': 'default', 'id': 's-1'}]}
    assert client.post(url, json=payload).status_code == 401
    headers = {'Authorization': 'Bearer alice'}
    response = client.post(url, json=payload, headers=headers)
    assert response.status_code == 200
    assert response.json() == {'sessions': [{'profile': 'default', 'id': 's-1', 'state': 'none'}]}
    owner.sessions['s-1'].source = SimpleNamespace(platform=Platform.TELEGRAM, user_id='alice')
    assert client.post(url, json=payload, headers=headers).json() == {'sessions': []}
    owner.sessions['s-1'].source = SimpleNamespace(platform=Platform.LOCAL, user_id='alice')
    assert client.post(url, json=payload, headers={'Authorization': 'Bearer bob'}).json() == {'sessions': []}
    assert calls == []  # no restore, mount, touch or operator authorization
    assert client.post(url, json={'sessions': [payload['sessions'][0], {'profile': 'default', 'id': 'other'}]}, headers=headers).json() == {
        'sessions': [{'profile': 'default', 'id': 's-1', 'state': 'none'}]}
    monkeypatch.setattr(web_server_sessions, '_session_mutation_context', lambda *_: (None, None))
    assert client.post(url, json=payload, headers=headers).json() == {'sessions': []}
    assert client.post(url, json={'sessions': payload['sessions'] * 33}, headers=headers).status_code == 422
    assert client.post(url, json={'sessions': [{'profile': '../bad', 'id': 's'}]}, headers=headers).status_code == 422
    assert client.post(url, json={'sessions': [{'profile': 'default', 'id': 1}]}, headers=headers).status_code == 422


def test_exact_head_query_and_fork_identity(monkeypatch):
    from urllib.parse import urlsplit, parse_qs
    seen = []
    def lookup(endpoint):
        seen.append(endpoint)
        return [pr(branch='feature/topic')]
    monkeypatch.setattr(status, '_api', lookup)
    result = status._resolve_evidence({'repo': 'owner/repo', 'branch': 'feature/topic'})
    assert result['state'] == 'open'
    assert parse_qs(urlsplit(seen[0]).query)['head'] == ['owner:feature/topic']
    wrong = pr(branch='feature/topic')
    wrong['head']['repo']['full_name'] = 'fork/repo'
    monkeypatch.setattr(status, '_api', lambda _: [wrong])
    assert status._resolve_evidence({'repo': 'owner/repo', 'branch': 'feature/topic'})['state'] == 'unknown'
    wrong['head']['repo']['full_name'] = 'owner/other'
    assert status._resolve_evidence({'repo': 'owner/repo', 'branch': 'feature/topic'})['state'] == 'unknown'


def test_preserves_known_link_on_failure(monkeypatch):
    def fail(_):
        raise OSError('offline')
    monkeypatch.setattr(status, '_api', fail)
    assert status._resolve_evidence({'repo': 'owner/repo', 'branch': 'feature', 'number': 4}) == {
        'state': 'unknown', 'repo': 'owner/repo', 'branch': 'feature', 'number': 4,
        'url': 'https://github.com/owner/repo/pull/4'}


def test_ordinary_chats_omitted_from_batch(monkeypatch):
    monkeypatch.setattr(status, '_evidence', lambda p, sid: None if sid == 'chat' else {})
    assert status.resolve_batch([{'profile': 'default', 'id': 'chat'},
                                 {'profile': 'default', 'id': 'coding'}]) == [
        {'profile': 'default', 'id': 'coding', 'state': 'unknown'}]


def test_installed_authority_rejects_other_user_without_operator(monkeypatch):
    from types import SimpleNamespace
    from gateway.config import Platform
    from gateway.session_authority import SessionAuthority
    from gateway.session_contract import SessionActor, SessionRef
    from gateway import session_authorities, session_retirement
    monkeypatch.setattr(session_authorities, 'require_serving', lambda _: None)
    monkeypatch.setattr(session_retirement, 'touch_restored', lambda *_: None)
    authority = SimpleNamespace(profile_id='p', sessions={
        's': SimpleNamespace(source=SimpleNamespace(platform=Platform.LOCAL, user_id='alice'))})
    ref = SessionRef('p', 's')
    SessionAuthority.authorize(authority, SessionActor('alice', 'p', frozenset({'session:read'}), 'http'), ref, 'session:read')
    with pytest.raises(Exception):
        SessionAuthority.authorize(authority, SessionActor('bob', 'p', frozenset({'session:read'}), 'http'), ref, 'session:read')
