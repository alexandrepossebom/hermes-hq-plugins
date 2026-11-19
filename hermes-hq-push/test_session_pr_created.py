"""Which pull requests are a session's: only one its own tool call opened (session_pr_created)."""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('pr_created_test', Path(__file__).with_name('session_pr_created.py'))
created = importlib.util.module_from_spec(spec)
spec.loader.exec_module(created)

APP = 'https://github.com/owner/app/pull/'


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


def run(db, command, output, sid='s', name='terminal', exit_code=0):
    """One terminal call and its paired result, as Hermes stores them."""
    call = f'call-{next(_ids)}'
    db.execute('INSERT INTO messages (session_id, role, content, tool_calls) VALUES (?, ?, ?, ?)',
               (sid, 'assistant', '', json.dumps([{'id': call, 'type': 'function', 'function': {
                   'name': name, 'arguments': json.dumps({'command': command})}}])))
    db.execute('INSERT INTO messages (session_id, role, content, tool_call_id, tool_name) VALUES (?, ?, ?, ?, ?)',
               (sid, 'tool', json.dumps({'output': output, 'exit_code': exit_code, 'error': None}), call, name))
    return call


def say(db, role, content, sid='s'):
    db.execute('INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)', (sid, role, content))


def test_a_session_that_opened_nothing_has_none(db):
    run(db, 'git commit -m work', '[feature abc1234] work')
    assert created.created_from_conn(db, 's') == []


def test_an_unknown_session_is_not_none(db):
    assert created.created_from_conn(db, 'missing') is None
    assert created.created_from_conn(db, '') is None
    db.execute('DROP TABLE messages')
    assert created.created_from_conn(db, 's') is None   # unreadable history is never "no pull request"


def test_gh_pr_create_gives_the_link_its_own_result_printed(db):
    run(db, 'cd /work && git push -u origin feature && gh pr create --draft --title T --body B',
        'remote: done\n' + APP + '12\n')
    assert created.created_from_conn(db, 's') == [('owner/app', 12)]


def test_gh_with_repo_flag_and_env_prefix(db):
    run(db, 'GH_PROMPT_DISABLED=1 gh -R owner/app pr create --fill', APP + '3')
    assert created.created_from_conn(db, 's') == [('owner/app', 3)]


@pytest.mark.parametrize('command,output', [
    ('gh pr view 9', APP + '9'),                                   # looked at
    ('gh pr list', '9\tTitle\tbranch\n' + APP + '9'),               # listed
    ('gh pr review 9 --approve', APP + '9'),                       # reviewed
    ('echo ' + APP + '9', APP + '9'),                              # wrote the link itself
    ('gh pr create --help', APP + '9'),
    ('gh pr create --web', APP + '9'),
    ('curl -s https://api.github.com/repos/owner/app/pulls', '"html_url": "' + APP + '9"'),
])
def test_a_link_the_bot_only_read_is_not_its_pull_request(db, command, output):
    run(db, command, output)
    assert created.created_from_conn(db, 's') == []


def test_a_failed_create_and_a_link_inside_other_text_do_not_count(db):
    run(db, 'gh pr create --fill', APP + '5', exit_code=1)
    run(db, 'gh pr create --fill', 'a pull request already exists: ' + APP + '6')
    assert created.created_from_conn(db, 's') == []


def test_links_in_conversation_are_never_evidence(db):
    say(db, 'user', 'please review ' + APP + '40')
    say(db, 'assistant', 'I opened ' + APP + '41')
    run(db, 'gh pr create --fill', APP + '42')
    assert created.created_from_conn(db, 's') == [('owner/app', 42)]


def test_running_a_publisher_does_not_make_its_pull_requests_the_chats(db):
    """A script that publishes everyone's merged work prints many links: they are not this chat's."""
    run(db, 'python3 scripts/public-prs.py', '  8a62098a  ' + APP + '343  The shelf folds\nDone: 1 pull request(s) published.')
    say(db, 'user', '[IMPORTANT: Background process proc_1 completed normally (exit code 0).\nOutput:\n  1a2b3c4d  ' + APP + '348  Title')
    assert created.created_from_conn(db, 's') == []


def test_a_subagents_pull_request_is_its_parents_and_nobody_elses(db):
    db.execute("INSERT INTO sessions VALUES ('child', 'subagent', 's')")
    db.execute("INSERT INTO sessions VALUES ('grandchild', 'subagent', 'child')")
    db.execute("INSERT INTO sessions VALUES ('continued', 'desktop', 's')")   # not a subagent: its own chat
    db.execute("INSERT INTO sessions VALUES ('other', 'desktop', NULL)")
    run(db, 'gh pr create --draft --fill', APP + '6', sid='grandchild')
    run(db, 'gh pr create --fill', APP + '90', sid='continued')
    run(db, 'gh pr create --fill', APP + '91', sid='other')
    run(db, 'gh pr create --fill', APP + '345')
    assert created.created_from_conn(db, 's') == [('owner/app', 6), ('owner/app', 345)]
    assert created.created_from_conn(db, 'child') == [('owner/app', 6)]
    assert created.created_from_conn(db, 'other') == [('owner/app', 91)]


def test_only_the_newest_few_and_no_duplicates(db):
    for number in range(1, 10):
        run(db, 'gh pr create --fill', APP + str(number))
    run(db, 'gh pr create --fill', APP + '2')            # reported again: now the newest
    assert created.created_from_conn(db, 's') == [('owner/app', n) for n in (5, 6, 7, 8, 9, 2)]


def test_history_is_data(db, monkeypatch):
    import os
    import subprocess
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('ran a command'))
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('ran a command'))
    monkeypatch.setattr(os, 'system', lambda *a: pytest.fail('ran a command'))
    run(db, 'gh pr create --title "$(rm -rf /)" --body `id`; cat /etc/passwd', APP + '1')
    run(db, 'gh pr create "unterminated', APP + '2')
    assert created.created_from_conn(db, 's') == [('owner/app', 1)]
