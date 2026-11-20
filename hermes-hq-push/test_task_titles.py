import threading
import time
import json
from contextlib import closing

import pytest
import tasks
from tasks import task_titles


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(task_titles, "schedule", lambda store: None)
    return tasks.Store(tmp_path / "tasks.db")


def create(store, title="Please call the dentist about a cleaning"):
    return store.create({"title": title}, "you", {"kind": "app"})


def test_recommendation_is_stored_and_explicit_patch_applies_it(store, monkeypatch):
    task = create(store)
    old_rev = store.rev()
    monkeypatch.setattr(task_titles, "generate", lambda title: "Call dentist about cleaning")
    assert task_titles.process_one(store)
    result = store.get(task["id"])
    assert result["title"] == task["title"]
    assert result["suggested_title"] == "Call dentist about cleaning"
    assert result["suggested_for_title"] == task["title"]
    assert store.rev() == old_rev + 1
    updated = store.update_route(task["id"], result["rev"], {"title": result["suggested_title"]})
    assert updated["title"] == "Call dentist about cleaning"
    assert updated["suggested_title"] is None and updated["suggested_for_title"] is None


def test_unavailable_is_nonfatal_and_retryable(store, monkeypatch):
    task = create(store)
    def unavailable(title):
        raise RuntimeError("unavailable")
    monkeypatch.setattr(task_titles, "generate", unavailable)
    assert task_titles.process_one(store)
    assert store.get(task["id"])["suggested_title"] is None
    with closing(store._write_db()) as db, db:
        assert db.execute("select count(*) from task_title_queue").fetchone()[0] == 1
        db.execute("update task_title_queue set next_attempt=0")
    monkeypatch.setattr(task_titles, "generate", lambda title: "Call dentist")
    assert task_titles.process_one(store)
    assert store.get(task["id"])["suggested_title"] == "Call dentist"


def test_late_completion_never_overwrites_or_recommends_for_renamed_task(store, monkeypatch):
    task = create(store)
    started, finish = threading.Event(), threading.Event()
    def generate(title):
        started.set()
        assert finish.wait(3)
        return "Call dentist"
    monkeypatch.setattr(task_titles, "generate", generate)
    worker = threading.Thread(target=task_titles.process_one, args=(store,))
    worker.start()
    assert started.wait(3)
    store.update_route(task["id"], task["rev"], {"title": "Book a different appointment"})
    finish.set()
    worker.join(3)
    assert not worker.is_alive()
    result = store.get(task["id"])
    assert result["title"] == "Book a different appointment"
    assert result["suggested_title"] is None


def test_transaction_rollback_and_duplicate_claims(store, monkeypatch):
    with closing(store._write_db()) as db:
        store.create({"title": "Rolled back"}, "you", {}, db=db)
        db.rollback()
    assert not task_titles.process_one(store)
    task = create(store)
    calls = []
    started, finish = threading.Event(), threading.Event()
    def generate(title):
        calls.append(title)
        started.set()
        assert finish.wait(3)
        return "Call dentist"
    monkeypatch.setattr(task_titles, "generate", generate)
    worker = threading.Thread(target=task_titles.process_one, args=(store,))
    worker.start()
    assert started.wait(3)
    assert not task_titles.process_one(store)
    finish.set()
    worker.join(3)
    assert calls == [task["title"]]
    assert store.get(task["id"])["suggested_title"] == "Call dentist"


def test_transactional_insert_and_same_title_do_not_requeue(store, monkeypatch):
    with closing(store._write_db()) as db, db:
        row = store.create({"title": "Buy milk"}, "you", {}, db=db)
    monkeypatch.setattr(task_titles, "generate", lambda title: title)
    assert task_titles.process_one(store)
    saved = store.get(row["id"])
    assert saved["suggested_title"] == saved["suggested_for_title"] == saved["title"] == "Buy milk"
    assert not task_titles.process_one(store)


def test_duplicate_task_add_enqueues_once(tmp_path, monkeypatch):
    from test_tasks import wired
    monkeypatch.setattr(task_titles, "schedule", lambda store: None)
    store, ctx, _ = wired(tmp_path)
    first = json.loads(ctx.tools["tasks_add"]({"title": "Call the dentist"}))
    again = json.loads(ctx.tools["tasks_add"]({"title": "call dentist"}))
    assert again["already"]["id"] == first["id"]
    with closing(store._db()) as db:
        assert db.execute("select count(*) from task_title_queue").fetchone()[0] == 1
    store.delete(first["id"])
    with closing(store._db()) as db:
        assert db.execute("select count(*) from task_title_queue").fetchone()[0] == 0


def test_auxiliary_receives_only_the_task_title(monkeypatch):
    from types import SimpleNamespace
    import sys
    from types import ModuleType
    agent = ModuleType("agent")
    agent.__path__ = []
    auxiliary_client = ModuleType("agent.auxiliary_client")
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", auxiliary_client)
    calls = []
    def call_llm(**kwargs):
        calls.append(kwargs)
        message = SimpleNamespace(content='{"title":"Call dentist"}')
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])
    monkeypatch.setattr(auxiliary_client, "call_llm", call_llm, raising=False)
    assert task_titles.generate("Call the dentist") == "Call dentist"
    assert calls[0]["task"] == "title_generation"
    assert json.loads(calls[0]["messages"][1]["content"]) == {"task_title": "Call the dentist"}
    assert "tools" not in calls[0]


def test_create_is_nonblocking(tmp_path, monkeypatch):
    started, finish = threading.Event(), threading.Event()
    def generate(title):
        started.set()
        finish.wait(3)
        return "Buy milk"
    monkeypatch.setattr(task_titles, "generate", generate)
    store = tasks.Store(tmp_path / "nonblocking.db")
    before = time.monotonic()
    task = create(store)
    assert time.monotonic() - before < 1
    assert started.wait(3)
    assert store.get(task["id"])["title"] == task["title"]
    finish.set()
    for _ in range(100):
        if store.get(task["id"])["suggested_title"]:
            break
        time.sleep(.01)
    assert store.get(task["id"])["suggested_title"] == "Buy milk"


def test_shape8_database_migrates_without_backfilling(tmp_path, monkeypatch):
    monkeypatch.setattr(task_titles, "schedule", lambda s: None)
    dbpath = tmp_path / "old.db"
    original = tasks.Store(dbpath)
    old = {"task": create(original, "Existing task"), "rev": original.rev()}
    with closing(original._write_db()) as db, db:
        db.execute("drop trigger task_title_queue_delete")
        db.execute("drop table task_title_queue")
        db.execute("alter table tasks drop column suggested_title")
        db.execute("alter table tasks drop column suggested_for_title")
        db.execute("update meta set value='8' where key='shape'")
    migrated = tasks.Store(dbpath)
    task = migrated.get(old['task']['id'])
    assert task['title'] == 'Existing task'
    assert task['suggested_title'] is None and task['suggested_for_title'] is None
    assert migrated.rev() == old['rev'] + 1
    assert migrated.list_response(old['rev']).get('unchanged') is not True
    assert not task_titles.process_one(migrated)
    rev = migrated.rev()
    assert tasks.Store(dbpath).rev() == rev


def test_chat_and_list_offer_create_queue_entries(tmp_path, monkeypatch):
    from test_tasks import wired, call
    monkeypatch.setattr(task_titles, 'schedule', lambda s: None)
    s, ctx, _ = wired(tmp_path)
    chat = call(ctx, 'tasks_add', {'title': 'Chat task'})
    offer = call(ctx, 'tasks_offer', {'list': 'Work', 'tasks': [{'title': 'List task'}]})['offer']
    accepted = s.answer_offer(offer['id'], 'accept')
    ids = {chat['id'], *accepted['offer']['added']}
    with closing(s._db()) as db:
        assert {r[0] for r in db.execute('select task_id from task_title_queue')} == ids
    monkeypatch.setattr(task_titles, 'generate', lambda title: 'Short ' + title)
    assert task_titles.process_one(s) and task_titles.process_one(s)
    assert all(s.get(i)['suggested_title'] for i in ids)


def test_goal_offer_create_queues_after_acceptance(tmp_path, monkeypatch):
    from test_goal_bots import Wired, a_goal, goals
    monkeypatch.setattr(task_titles, 'schedule', lambda s: None)
    w = Wired(tmp_path)
    goal = a_goal(w.store)
    offer = w.call('goals_offer', {'kind': 'tasks', 'goal': goal['id'], 'tasks': [{'title': 'Share invite'}]})
    answer = goals.answer_offer(w.store, offer['id'], 'accept')
    task_id = answer['offer']['added'][0]
    monkeypatch.setattr(task_titles, 'generate', lambda title: 'Send invite')
    assert task_titles.process_one(w.store)
    assert w.store.get(task_id)['suggested_title'] == 'Send invite'


from test_tasks import client


def test_dashboard_app_and_reminders_import_create_recommendations(client, tmp_path, monkeypatch):
    monkeypatch.setattr(task_titles, 'schedule', lambda s: None)
    base = '/api/plugins/hermes-hq-push'
    created = []
    for body in ({'title': 'App task'}, {'title': 'Imported task', 'source': {'kind': 'reminders', 'reminder_id': 'EK-123'}}):
        response = client.post(base + '/tasks', json=body)
        assert response.status_code == 200
        created.append(response.json())
    # The route and the worker use independently opened connections to the same isolated DB.
    store = tasks.Store(tmp_path / 'data' / 'tasks.db')
    monkeypatch.setattr(task_titles, 'generate', lambda title: 'Short ' + title)
    assert task_titles.process_one(store) and task_titles.process_one(store)
    listing = client.get(base + '/tasks').json()
    by_id = {row['id']: row for row in listing['tasks']}
    for task in created:
        assert by_id[task['id']]['suggested_title'] == 'Short ' + task['title']
    assert by_id[created[1]['id']]['source']['reminder_id'] == 'EK-123'


def test_stale_edit_after_recommendation_is_rejected(store, monkeypatch):
    task = create(store)
    monkeypatch.setattr(task_titles, 'generate', lambda title: 'Call dentist')
    assert task_titles.process_one(store)
    with pytest.raises(tasks.TaskError) as exc:
        store.update_route(task['id'], task['rev'], {'title': 'Old edit'})
    assert exc.value.status_code == 409
    assert store.get(task['id'])['suggested_title'] == 'Call dentist'


def test_retry_respects_backoff_and_keeps_revision(store, monkeypatch):
    task = create(store)
    before = store.rev()
    monkeypatch.setattr(task_titles, 'generate', lambda title: None)
    assert task_titles.process_one(store)
    assert store.rev() == before and store.get(task['id'])['rev'] == task['rev']
    assert not task_titles.process_one(store)
    with closing(store._db()) as db:
        next_attempt = db.execute('select next_attempt from task_title_queue').fetchone()[0]
    assert next_attempt > time.time()
