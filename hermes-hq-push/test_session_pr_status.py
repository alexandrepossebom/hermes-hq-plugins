import hashlib
import importlib.util
import io
import json
import os
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('pr_status_test', Path(__file__).with_name('session_pr_status.py'))
status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)

TOKEN = 'ghp_' + 'x' * 36
OWNER = status.Access(token=TOKEN)            # the signed-in owner's own token
PUBLIC = status.Access(public_repos={'owner/repo'})
NOBODY = status.Access()


@pytest.fixture(autouse=True)
def clear_cache():
    status._cache.clear()
    yield
    status._cache.clear()


@pytest.fixture(autouse=True)
def no_host_tools(monkeypatch):
    # The host's gh sign-in is never a credential for this endpoint.
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: pytest.fail('ran a host tool'))
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **kw: pytest.fail('ran a host tool'))


def pr(number=1, state='open', draft=False, merged_at=None, branch='feature', repo='owner/repo'):
    return {'number': number, 'state': state, 'draft': draft, 'merged_at': merged_at,
            'base': {'repo': {'full_name': repo}}, 'head': {'ref': branch, 'repo': {'full_name': repo}}}


def item(evidence, access=OWNER, sid='session-1'):
    return {'profile': 'default', 'id': sid, 'evidence': evidence, 'access': access}


@pytest.mark.parametrize('data,expected', [
    (pr(), 'open'), (pr(draft=True), 'draft'), (pr(state='closed'), 'closed'),
    (pr(state='closed', merged_at='2026-01-01'), 'merged'),
])
def test_explicit_number_states(monkeypatch, data, expected):
    monkeypatch.setattr(status, '_api', lambda endpoint, access: data)
    rows = status.resolve_batch([item({'repo': 'owner/repo', 'number': 1})])
    assert rows == [{'profile': 'default', 'id': 'session-1', 'state': expected, 'repo': 'owner/repo',
                     'branch': 'feature', 'number': 1, 'url': 'https://github.com/owner/repo/pull/1'}]


def test_noncode_omitted_and_uncertain_unknown(monkeypatch):
    monkeypatch.setattr(status, '_api', lambda *_: pytest.fail('called GitHub'))
    assert status.resolve_batch([item(None)]) == []
    assert status.resolve_batch([item({})]) == [{'profile': 'default', 'id': 'session-1', 'state': 'unknown'}]


@pytest.mark.parametrize('info', [
    {'repo': 'https://evil.example/o/r', 'number': 1}, {'repo': '../../evil', 'number': 1},
    {'repo': '-o/repo', 'number': 1}, {'repo': 'owner/repo?x=y', 'number': 1},
    {'repo': 'owner/repo', 'number': True}, {'repo': 'owner/repo', 'number': -1},
    {'repo': 'owner/repo', 'branch': 'bad\nbranch'},
])
def test_untrusted_evidence_never_reaches_github(monkeypatch, info):
    monkeypatch.setattr(status, '_api', lambda *_: pytest.fail('called GitHub'))
    assert status._resolve_evidence(info, OWNER)['state'] == 'unknown'


@pytest.mark.parametrize('info', [
    {'repo': 'owner/private', 'number': 3},                    # not declared public
    {'repo': 'owner/private', 'branch': 'feature/secret'},
    {'repo': 'owner/repo', 'branch': 'feature/local-only'},   # public repo, but a branch may be local-only
])
def test_unproven_private_repo_or_branch_never_leaves_without_owner_token(monkeypatch, info):
    monkeypatch.setattr(status, '_api', lambda *_: pytest.fail('sent unproven repo or branch'))
    for access in (NOBODY, PUBLIC):
        assert status._resolve_evidence(info, access) == {
            'state': 'unknown', **{k: v for k, v in info.items()},
            **({'url': f"https://github.com/{info['repo']}/pull/{info['number']}"} if 'number' in info else {})}


def test_public_repo_by_number_is_asked_anonymously(monkeypatch):
    seen = []
    monkeypatch.setattr(status, '_api', lambda endpoint, access: seen.append((endpoint, access.token)) or pr())
    assert status._resolve_evidence({'repo': 'Owner/Repo', 'number': 1}, PUBLIC)['state'] == 'open'
    assert seen == [('/repos/Owner/Repo/pulls/1', None)]


def test_forks_and_pagination_are_ambiguous(monkeypatch):
    info = {'repo': 'owner/repo', 'branch': 'feature'}
    monkeypatch.setattr(status, '_api', lambda *_: [pr(1), pr(2)])
    assert status._resolve_evidence(info, OWNER)['state'] == 'unknown'
    status._cache.clear()
    monkeypatch.setattr(status, '_api', lambda *_: [pr(n, branch='other') for n in range(1, 101)])
    assert status._resolve_evidence(info, OWNER)['state'] == 'unknown'
    status._cache.clear()
    monkeypatch.setattr(status, '_api', lambda *_: [])
    assert status._resolve_evidence(info, OWNER) == {'state': 'unknown', **info}


@pytest.mark.parametrize('fork,state', [(False, 'none'), (True, 'unknown')])
def test_empty_head_in_fork_vs_repository(monkeypatch, fork, state):
    def lookup(endpoint, access):
        assert access is OWNER
        if endpoint == '/repos/owner/repo':
            return {'full_name': 'owner/repo', 'fork': fork, 'default_branch': 'main'}
        assert 'head=owner%3Afeature' in endpoint
        return []
    monkeypatch.setattr(status, '_api', lookup)
    info = {'repo': 'owner/repo', 'branch': 'feature'}
    assert status._resolve_evidence(info, OWNER) == {'state': state, **info}


def test_explicit_number_must_match_repo_and_branch(monkeypatch):
    info = {'repo': 'owner/repo', 'number': 1, 'branch': 'feature'}
    monkeypatch.setattr(status, '_api', lambda *_: pr(repo='other/repo'))
    assert status._resolve_evidence(info, OWNER)['state'] == 'unknown'
    status._cache.clear()
    monkeypatch.setattr(status, '_api', lambda *_: pr(branch='other'))
    assert status._resolve_evidence(info, OWNER)['state'] == 'unknown'


def test_cache_ttl_is_per_access(monkeypatch):
    now = [1.0]
    calls = []
    monkeypatch.setattr(status.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(status, '_api', lambda endpoint, access: calls.append(access.key) or pr())
    info = {'repo': 'owner/repo', 'number': 1}
    status._resolve_evidence(info, OWNER); status._resolve_evidence(info, OWNER)
    assert len(calls) == 1
    status._resolve_evidence(info, PUBLIC)   # an anonymous caller never reads a token's cached answer
    assert len(calls) == 2 and calls[1] == 'anonymous'
    now[0] += status.TTL + 1
    status._resolve_evidence(info, OWNER)
    assert len(calls) == 3
    assert TOKEN not in repr(OWNER) and TOKEN not in OWNER.key
    with pytest.raises(ValueError):
        status.resolve_batch([item({})] * 33)


class _Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_requests_carry_no_host_identity(monkeypatch):
    seen = []

    def open_(request, timeout):
        seen.append((request.full_url, dict(request.header_items()), timeout))
        return _Response(json.dumps(pr()).encode())
    monkeypatch.setattr(status._opener, 'open', open_)
    monkeypatch.setenv('GH_TOKEN', 'host-token-must-not-be-used')
    monkeypatch.setenv('GITHUB_TOKEN', 'host-token-must-not-be-used')
    assert status._resolve_evidence({'repo': 'owner/repo', 'number': 1}, PUBLIC)['state'] == 'open'
    url, headers, timeout = seen[0]
    assert url == 'https://api.github.com/repos/owner/repo/pulls/1'
    assert 'Authorization' not in headers and timeout == status._TIMEOUT
    assert 'host-token-must-not-be-used' not in json.dumps(headers)
    status._cache.clear()
    assert status._resolve_evidence({'repo': 'owner/repo', 'number': 1}, OWNER)['state'] == 'open'
    assert seen[1][1]['Authorization'] == f'Bearer {TOKEN}'


def test_redirects_are_not_followed():
    handler = status._NoRedirect()
    assert handler.redirect_request(None, None, 301, 'Moved', {}, 'https://elsewhere.example/') is None
    with pytest.raises(ValueError):
        status._api('https://elsewhere.example/repos/a/b', NOBODY)


def test_failed_github_is_unknown(monkeypatch):
    def fail(*_):
        raise OSError('offline')
    monkeypatch.setattr(status, '_api', fail)
    assert status._resolve_evidence({'repo': 'owner/repo', 'number': 1}, OWNER)['state'] == 'unknown'


def test_exact_head_query_and_fork_identity(monkeypatch):
    from urllib.parse import urlsplit, parse_qs
    seen = []
    monkeypatch.setattr(status, '_api', lambda endpoint, access: seen.append(endpoint) or [pr(branch='feature/topic')])
    info = {'repo': 'owner/repo', 'branch': 'feature/topic'}
    assert status._resolve_evidence(info, OWNER)['state'] == 'open'
    assert parse_qs(urlsplit(seen[0]).query)['head'] == ['owner:feature/topic']
    wrong = pr(branch='feature/topic')
    wrong['head']['repo']['full_name'] = 'fork/repo'
    status._cache.clear()
    monkeypatch.setattr(status, '_api', lambda *_: [wrong])
    assert status._resolve_evidence(info, OWNER)['state'] == 'unknown'
    wrong['head']['repo']['full_name'] = 'owner/other'
    status._cache.clear()
    assert status._resolve_evidence(info, OWNER)['state'] == 'unknown'


def test_preserves_known_link_on_failure(monkeypatch):
    def fail(*_):
        raise OSError('offline')
    monkeypatch.setattr(status, '_api', fail)
    assert status._resolve_evidence({'repo': 'owner/repo', 'branch': 'feature', 'number': 4}, OWNER) == {
        'state': 'unknown', 'repo': 'owner/repo', 'branch': 'feature', 'number': 4,
        'url': 'https://github.com/owner/repo/pull/4'}


def test_ordinary_chats_omitted_from_batch():
    assert status.resolve_batch([item(None, sid='chat'), item({}, sid='coding')]) == [
        {'profile': 'default', 'id': 'coding', 'state': 'unknown'}]


def test_owner_token_must_be_private_and_owned(tmp_path):
    subject = 'uid:501'
    path = status.owner_token_path(tmp_path, subject)
    assert path.name == hashlib.sha256(subject.encode()).hexdigest() + '.token'
    assert status.access_for(tmp_path, subject).token is None
    path.parent.mkdir()
    path.write_text(TOKEN + '\n')
    os.chmod(path, 0o644)
    assert status.access_for(tmp_path, subject).token is None      # readable by others
    os.chmod(path, 0o600)
    assert status.access_for(tmp_path, subject).token == TOKEN
    assert status.access_for(tmp_path, 'uid:502').token is None    # another owner's sessions
    link = status.owner_token_path(tmp_path, 'uid:503')
    link.symlink_to(path)
    assert status.access_for(tmp_path, 'uid:503').token is None    # never followed


def test_config_public_repos_and_roots(tmp_path):
    assert status.load_config(tmp_path) == {'public_repos': frozenset(), 'receipt_roots': ()}
    (tmp_path / status.CONFIG_NAME).write_text(json.dumps({'public_repos': ['Owner/Repo'], 'receipt_roots': ['/a/b/']}))
    assert status.load_config(tmp_path) == {'public_repos': frozenset({'owner/repo'}), 'receipt_roots': ('/a/b',)}
    (tmp_path / status.CONFIG_NAME).write_text(json.dumps({'public_repos': ['../x'], 'receipt_roots': []}))
    assert status.load_config(tmp_path)['public_repos'] == frozenset()
    (tmp_path / status.CONFIG_NAME).write_text(json.dumps({'receipt_roots': ['relative/path']}))
    assert status.load_config(tmp_path)['receipt_roots'] == ()


def test_capability_only_on_supported_runtime(monkeypatch):
    monkeypatch.setattr(status, '_supported', None)
    real = status.importlib.import_module

    def missing(name, *args):
        if name == 'hermes_state_local_lineage':
            raise ImportError(name)
        return real(name, *args)
    monkeypatch.setattr(status.importlib, 'import_module', missing)
    assert status.supported() is False
    monkeypatch.setattr(status, '_supported', None)
    monkeypatch.setattr(status.importlib, 'import_module', real)
    assert status.supported() is True       # the runtime these tests import has every API


def _api_module(tmp_path, monkeypatch, name):
    import test_tasks
    monkeypatch.setenv('HERMES_HQ_PUSH_DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setenv('HERMES_HQ_PUSH_RELAY_URL', 'off')
    monkeypatch.setattr(test_tasks.push, '_watcher_started', True)
    api_spec = importlib.util.spec_from_file_location(name, Path(__file__).parent / 'dashboard/plugin_api.py')
    api = importlib.util.module_from_spec(api_spec)
    api_spec.loader.exec_module(api)
    monkeypatch.setattr(api, '_loop_watched', True)
    return api


def test_status_and_route_follow_runtime_support(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    api = _api_module(tmp_path, monkeypatch, 'pr_status_capability_test')
    monkeypatch.setattr(api.session_pr_status, '_supported', False)
    assert api.status()['sessionPRStatus'] is False
    app = FastAPI()
    app.include_router(api.router)
    response = TestClient(app).post('/sessions/pr-status', json={'sessions': [{'profile': 'default', 'id': 's'}]})
    assert response.status_code == 501
    monkeypatch.setattr(api.session_pr_status, '_supported', True)
    assert api.status()['sessionPRStatus'] is True


def test_http_route_and_validation(tmp_path, monkeypatch):
    from fastapi import FastAPI, Depends, HTTPException, Header, Request
    from types import SimpleNamespace
    from gateway.session_contract import SessionActor
    from gateway.config import Platform
    from hermes_cli import web_server_sessions, profiles
    from fastapi.testclient import TestClient
    api = _api_module(tmp_path, monkeypatch, 'pr_status_api_test')
    batches = []

    def fake_resolve(items):
        batches.append(items)
        return [{'profile': i['profile'], 'id': i['id'], 'state': 'none'} for i in items]
    monkeypatch.setattr(api.session_pr_status, 'resolve_batch', fake_resolve)
    calls = []
    home = tmp_path / 'default'

    class Owner:
        db = SimpleNamespace(db_path=str(home / 'state.db'))
        sessions = {'s-1': SimpleNamespace(source=SimpleNamespace(platform=Platform.LOCAL, user_id='alice'))}

        def authorize(self, actor, ref, capability):
            calls.append((actor, ref, capability))
    owner = Owner()
    from gateway import session_authorities, session_history_mount
    from hermes_state import SessionDB
    home.mkdir()
    db = SessionDB(db_path=home / 'state.db')
    db.create_session('s-1', source='desktop')
    call = {'id': 'c1', 'type': 'function', 'function': {'name': 'write_file',
            'arguments': json.dumps({'path': '/work/app.py', 'content': 'x'})}}
    db.append_message('s-1', 'assistant', '', tool_calls=[call])
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
    assert batches[-1][0]['evidence'] == {} and batches[-1][0]['access'].token is None
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


def test_live_session_whose_stored_row_names_someone_else_is_not_read(tmp_path, monkeypatch):
    from fastapi import FastAPI, Request
    from types import SimpleNamespace
    from gateway.session_contract import SessionActor
    from gateway.config import Platform
    from gateway import session_authorities, session_history_mount
    from hermes_cli import web_server_sessions, profiles
    from hermes_state import SessionDB
    from fastapi.testclient import TestClient
    api = _api_module(tmp_path, monkeypatch, 'pr_status_live_row_test')
    monkeypatch.setattr(api.session_pr_status, 'evidence_in', lambda *_: pytest.fail('read evidence'))
    home = tmp_path / 'default'
    home.mkdir()
    db = SessionDB(db_path=home / 'state.db')
    db.create_session('s-1', source='desktop', user_id='uid:foreign')
    db.close()
    owner = SimpleNamespace(db=SimpleNamespace(db_path=str(home / 'state.db')),
                            sessions={'s-1': SimpleNamespace(source=SimpleNamespace(platform=Platform.LOCAL, user_id='alice'))})
    monkeypatch.setattr(session_authorities, 'require_serving', lambda _: None)
    monkeypatch.setattr(session_history_mount, 'is_history_mount', lambda _: False)
    monkeypatch.setattr(profiles, 'get_profile_dir', lambda _: home)
    monkeypatch.setattr(web_server_sessions, '_session_mutation_context', lambda req, profile:
                        (owner, SessionActor('alice', str(home), frozenset({'session:read'}), 'http')))
    app = FastAPI()
    app.include_router(api.router)
    assert TestClient(app).post('/sessions/pr-status', json={'sessions': [{'profile': 'default', 'id': 's-1'}]}).json() == {
        'sessions': []}


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
