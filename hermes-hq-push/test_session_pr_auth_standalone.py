"""Standalone PR authorization uses real Hermes principals and state.db, without writes."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.testclient import TestClient
import pytest
from hermes_state import SessionDB


def test_standalone_owner_scoping(tmp_path, monkeypatch):
    from hermes_cli import profiles, web_server_cron
    import test_tasks

    home = tmp_path / 'default'
    home.mkdir()
    db = SessionDB(db_path=home / 'state.db')
    for sid, source, user in [('owned', 'tui', 'basic:alice'), ('foreign', 'tui', 'uid:foreign'),
                              ('unowned', 'tui', None), ('message', 'telegram', 'basic:alice'),
                              ('bound', 'tui', 'basic:alice')]:
        db.create_session(sid, source=source, user_id=user)
        db.end_session(sid, end_reason='done')
    from hermes_state_keys import local_identity
    from hermes_state_local import POLICY_PREFIX
    closed = local_identity(str(home), 'basic:alice', 'request-1')
    db.create_session(closed, source='desktop', user_id='basic:alice')
    policy = {'profile_id': str(home), 'principal_id': 'basic:alice', 'request_id': 'request-1',
              'session_id': closed, 'route': 'test-local-route', 'entry': {'session_id': closed}}
    db._execute_write(lambda conn: conn.execute('UPDATE sessions SET chat_id=?,session_key=? WHERE id=?',
                                                (closed, policy['route'], closed)))
    db._execute_write(lambda conn: conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)',
                                                (POLICY_PREFIX + closed, json.dumps(policy))))
    db.end_session(closed, end_reason='done')
    call = {'id': 'call-1', 'type': 'function', 'function': {'name': 'write_file',
            'arguments': json.dumps({'path': '/tmp/example.py', 'content': 'x'})}}
    db.append_message('owned', 'assistant', '', tool_calls=[call])
    db.append_message('owned', 'tool', json.dumps({'output': 'ok', 'exit_code': 0}),
                      tool_call_id='call-1', tool_name='write_file')
    db._execute_write(lambda conn: conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)',
                     ('gateway.history_control.v1.bound', json.dumps([str(home), 'uid:foreign']))))
    with db._read_ctx() as conn:
        before = list(conn.execute('SELECT key,value FROM state_meta ORDER BY key'))
    db.close()
    monkeypatch.setattr(profiles, 'get_profile_dir', lambda name: home if name == 'default' else tmp_path / name)
    monkeypatch.setattr(web_server_cron, '_cron_profile_home', lambda name: (name, profiles.get_profile_dir(name)))
    monkeypatch.setenv('HERMES_HQ_PUSH_DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setenv('HERMES_HQ_PUSH_RELAY_URL', 'off')
    monkeypatch.setattr(test_tasks.push, '_watcher_started', True)
    spec = importlib.util.spec_from_file_location('standalone_pr_api', Path(__file__).parent / 'dashboard/plugin_api.py')
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    monkeypatch.setattr(api, '_loop_watched', True)
    seen, read = [], []
    real_resolve_batch = api.session_pr_status.resolve_batch
    real_created_in = api.session_pr_status.created_in
    monkeypatch.setattr(api.session_pr_status, 'resolve_batch', lambda items: seen.append(
        [{'profile': i['profile'], 'id': i['id']} for i in items]) or seen[-1])
    # Authorization alone decides which sessions' history is read at all.
    monkeypatch.setattr(api.session_pr_status, 'created_in', lambda conn, sid: read.append(sid) or [])

    def login(request: Request, authorization: str = Header(default='')):
        if authorization != 'Bearer valid':
            raise HTTPException(401)
        request.state.session = SimpleNamespace(user_id='alice', provider='basic', issuer='')

    app = FastAPI()
    app.state.auth_required = True
    app.include_router(api.router, dependencies=[Depends(login)])
    client = TestClient(app)
    rows = [{'profile': 'default', 'id': sid} for sid in ('owned', 'foreign', 'unowned', 'message', 'bound', 'missing', closed)]
    assert client.post('/sessions/pr-status', json={'sessions': rows}).status_code == 401
    response = client.post('/sessions/pr-status', json={'sessions': rows}, headers={'Authorization': 'Bearer valid'})
    assert response.status_code == 200
    assert response.json() == {'sessions': [rows[0], rows[-1]]}
    assert seen == [[rows[0], rows[-1]]]
    assert read == ['owned', closed]
    monkeypatch.setattr(api.session_pr_status, 'resolve_batch', real_resolve_batch)
    monkeypatch.setattr(api.session_pr_status, 'created_in', real_created_in)
    api.session_pr_status._cache.clear()
    assert client.post('/sessions/pr-status', json={'sessions': rows}, headers={'Authorization': 'Bearer valid'}).json() == {
        'sessions': [{'profile': 'default', 'id': 'owned', 'state': 'none'},
                     {'profile': 'default', 'id': closed, 'state': 'none'}]}
    db = SessionDB(db_path=home / 'state.db', read_only=True)
    with db._read_ctx() as conn:
        assert list(conn.execute('SELECT key,value FROM state_meta ORDER BY key')) == before
    db.close()

    def native(request: Request):
        from gateway.owner_identity import host_owner_subject
        request.state.native_http_principal = {'subject': host_owner_subject(), 'profile_id': str(tmp_path / 'other')}
    other = FastAPI()
    other.include_router(api.router, dependencies=[Depends(native)])
    assert TestClient(other).post('/sessions/pr-status', json={'sessions': rows}).json() == {'sessions': []}


def test_authorization_and_evidence_share_one_snapshot(tmp_path, monkeypatch):
    """A write landing between the ownership check and the evidence read is invisible to both:
    the session is judged and read as it was at one instant."""
    from hermes_cli import profiles, web_server_cron
    import hermes_state_local_migration as migration
    import test_tasks

    home = tmp_path / 'default'
    home.mkdir()
    db = SessionDB(db_path=home / 'state.db')
    db.create_session('chat', source='tui', user_id='basic:alice')
    db.append_message('chat', 'user', 'hello')
    db.end_session('chat', end_reason='done')
    db.close()
    monkeypatch.setattr(profiles, 'get_profile_dir', lambda name: home)
    monkeypatch.setattr(web_server_cron, '_cron_profile_home', lambda name: (name, home))
    monkeypatch.setenv('HERMES_HQ_PUSH_DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setenv('HERMES_HQ_PUSH_RELAY_URL', 'off')
    monkeypatch.setattr(test_tasks.push, '_watcher_started', True)
    spec = importlib.util.spec_from_file_location('snapshot_pr_api', Path(__file__).parent / 'dashboard/plugin_api.py')
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    monkeypatch.setattr(api, '_loop_watched', True)
    monkeypatch.setattr(api.session_pr_status, '_api', lambda *_: pytest.fail('asked GitHub'))
    real_row = migration.legacy_history_row

    def check_then_race(row):
        # Another writer commits right after ownership was judged: a pull request appears.
        writer = SessionDB(db_path=home / 'state.db')
        call = {'id': 'c1', 'type': 'function', 'function': {'name': 'terminal',
                'arguments': json.dumps({'command': 'gh pr create'})}}
        writer.append_message('chat', 'assistant', '', tool_calls=[call])
        writer.append_message('chat', 'tool', json.dumps({'output': 'https://github.com/a/b/pull/7', 'exit_code': 0}),
                              tool_call_id='c1', tool_name='terminal')
        writer.close()
        return real_row(row)
    monkeypatch.setattr(migration, 'legacy_history_row', check_then_race)

    def login(request: Request):
        request.state.session = SimpleNamespace(user_id='alice', provider='basic', issuer='')
    app = FastAPI()
    app.state.auth_required = True
    app.include_router(api.router, dependencies=[Depends(login)])
    client = TestClient(app)
    body = {'sessions': [{'profile': 'default', 'id': 'chat'}]}
    assert client.post('/sessions/pr-status', json=body).json() == {'sessions': [
        {'profile': 'default', 'id': 'chat', 'state': 'none'}]}   # the judged snapshot: nothing opened yet
    monkeypatch.setattr(migration, 'legacy_history_row', real_row)
    assert client.post('/sessions/pr-status', json=body).json() == {'sessions': [
        {'profile': 'default', 'id': 'chat', 'state': 'unknown', 'repo': 'a/b', 'number': 7,
         'url': 'https://github.com/a/b/pull/7'}]}
