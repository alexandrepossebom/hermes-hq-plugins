"""Allow in 1Password (0.9.59): the job (waiting → choose → done), where the key is kept, Disconnect, and Hermes's
backend reading a key saved a moment ago. ``~/.hermes/hermes-agent/.venv/bin/python -m pytest -q
test_onepassword_allow.py`` from the plugin folder. conftest.py pins HERMES_HOME to a throwaway folder; ``op`` is never
run here (every call that would is replaced), so the computer's own 1Password is never asked anything."""
import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.environ.get("HERMES_AGENT", os.path.expanduser("~/.hermes/hermes-agent")))

from hermes_constants import get_default_hermes_root  # noqa: E402

HERE = Path(__file__).parent
_spec = importlib.util.spec_from_file_location("onepassword_allow_under_test", HERE / "onepassword_allow.py")
allow = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(allow)

ROOT = get_default_hermes_root()
assert ROOT.resolve() != (Path.home() / ".hermes").resolve()
KEY = "ops_" + "A1b2C3d4" * 8
OTHER = "ops_" + "Z9y8X7w6" * 8
VAULTS = [{"id": "vaultaaaaaaaaaaaaaaaaaaaaa", "name": "Hermes", "items": 3},
          {"id": "vaultbbbbbbbbbbbbbbbbbbbbb", "name": "Work", "items": 40}]
ACCOUNT = [{"id": "ACCOUNTAAAAAAAAAAAAAAAAAAA", "url": "my.1password.com", "email": "someone@example.com"}]


def home(name):
    path = ROOT if name == "default" else ROOT / "profiles" / name
    path.mkdir(parents=True, exist_ok=True)
    if not (path / "config.yaml").exists():
        (path / "config.yaml").write_text("{}\n")   # what makes a folder a bot to Hermes
    return path


@pytest.fixture
def bots(tmp_path, monkeypatch):
    monkeypatch.setattr(allow, "find_op", lambda: "/usr/bin/true")
    monkeypatch.delenv(allow.TOKEN_ENV, raising=False)
    with allow._lock:
        allow._jobs.clear()
    homes = [("default", home("default")), ("finance", home("finance"))]
    for _, path in homes:
        (path / ".env").unlink(missing_ok=True)
    monkeypatch.setattr(allow, "bot_homes", lambda: homes)
    yield tmp_path, homes
    for _, path in homes:
        (path / ".env").unlink(missing_ok=True)
    os.environ.pop(allow.TOKEN_ENV, None)   # Hermes's save also sets it for the process's own bot


def settle(key, *states, limit=5.0):
    until = time.time() + limit
    while time.time() < until:
        job = allow.job(key)
        if job["state"] in states:
            return job
        time.sleep(0.01)
    raise AssertionError(f"job stayed {allow.job(key)['state']}")


def allowed(data_dir):
    started = allow.start(data_dir, lister=lambda account: [dict(v) for v in VAULTS], account_lister=lambda: ACCOUNT)
    assert started["state"] == "waiting"
    return settle(started["job"], "choose", "failed")


# MARK: op's words

def test_op_runs_as_the_person_never_with_a_saved_key():
    env = allow.op_env({"PATH": "/bin", "HOME": "/h", "OP_SERVICE_ACCOUNT_TOKEN": KEY, "OP_SESSION_x": "s",
                        "OP_CONNECT_TOKEN": "c", "OP_CONNECT_HOST": "h", "XAI_API_KEY": "k"})
    assert env == {"PATH": "/bin", "HOME": "/h", "NO_COLOR": "1", "OP_BIOMETRIC_UNLOCK_ENABLED": "true"}


def test_a_key_never_survives_in_an_error():
    assert KEY not in allow.scrub(f"\x1b[31m[ERROR]\x1b[0m bad {KEY}")
    assert KEY not in allow.problem(f"[ERROR] something odd {KEY}").detail


@pytest.mark.parametrize("words, code", [
    ("[ERROR] 2026/11/16 authorization prompt dismissed, please try again", "denied"),
    ("[ERROR] 2026/11/16 authorization timeout", "timeout"),
    ("[ERROR] connecting to desktop app: connection reset, make sure 1Password CLI is installed correctly", "integration-off"),
    ("[ERROR] You've reached the maximum number of service accounts", "limit"),
    ("[ERROR] (403) Forbidden: You do not have permission", "permission"),
    ("[ERROR] the moon is wrong", "op-error"),
])
def test_ops_failures_in_the_apps_words(words, code):
    assert allow.problem(words).code == code


def test_private_vaults_are_never_offered(monkeypatch):
    listed = [{"id": "vaultppppppppppppppppppppp", "name": "Private"}, {"id": VAULTS[1]["id"], "name": "Work"},
              {"id": VAULTS[0]["id"], "name": "Hermes"}, {"id": "vaultsssssssssssssssssssss", "name": "Shared", "type": "EVERYONE"}]
    kinds = {"vaultppppppppppppppppppppp": "PERSONAL", VAULTS[1]["id"]: "USER_CREATED", VAULTS[0]["id"]: "USER_CREATED"}

    def answer(args, timeout=0):
        if args[:2] == ["vault", "list"]:
            return listed
        return {"id": args[2], "type": kinds[args[2]], "items": 7}
    monkeypatch.setattr(allow, "op_json", answer)
    assert [(v["name"], v["items"]) for v in allow.open_vaults("ACCOUNT")] == [("Hermes", 7), ("Work", 7)]


def test_the_key_is_read_only_for_the_picked_vaults(monkeypatch):
    seen = {}

    class Done:
        returncode, stdout, stderr = 0, f"{KEY}\n", ""

    def run(args, timeout=0):
        seen["args"] = args
        return Done()
    monkeypatch.setattr(allow, "run_op", run)
    assert allow.make_key("ACCOUNT", ["v1", "v2"]) == KEY
    assert seen["args"][:2] == ["service-account", "create"] and "--raw" in seen["args"]
    assert [a for a in seen["args"] if a.endswith(":read_items")] == ["v1:read_items", "v2:read_items"]
    assert not any("write_items" in a or "can-create" in a for a in seen["args"])


# MARK: Hermes's backend
# Built inside the bot's own scope, as every Hermes turn and Settings call builds it (an earlier test here made the
# process serve two bots, after which Hermes refuses a key read outside one). These run before the job's tests: once
# the process serves two bots Hermes freezes the first bot's keys as they were then.

def test_hermes_reads_a_key_saved_a_moment_ago_and_takes_allowed_for_the_card(bots):
    _, homes = bots
    from agent.vault_backends.onepassword import OnePasswordLoginBackend
    assert allow.install() in ("installed", "already") and allow.install() == "already"
    with allow._scope(""):
        locked = OnePasswordLoginBackend({})
        assert not locked._service_token
        with pytest.raises(RuntimeError):
            locked.unlock(allow.ALLOWED)                 # nothing saved yet: the card can't be answered with it
        (homes[0][1] / ".env").write_text(f"{allow.TOKEN_ENV}={KEY}\n")
        locked.unlock(allow.ALLOWED)                     # the turn that was waiting
        assert locked._service_token == KEY and locked.is_unlocked()
        assert OnePasswordLoginBackend({}).is_unlocked()  # and every one built after it
    with allow._scope("finance"):
        assert not OnePasswordLoginBackend({}).is_unlocked()   # another bot's .env has none


def test_on_the_phone_sends_the_bot_to_ask_for_the_login(bots):
    from agent.vault_backends.onepassword import OnePasswordLoginBackend
    allow.install()
    with allow._scope(""):
        backend = OnePasswordLoginBackend({})
        with pytest.raises(RuntimeError) as error:
            backend.unlock(allow.PHONE)
    assert "save_login" in str(error.value) and len(str(error.value)) <= 300   # Hermes hands the bot 300 characters
    assert allow.state(bots[0])["phone"] is True


def test_another_token_name_in_config_is_left_alone(bots):
    _, homes = bots
    from agent.vault_backends.onepassword import OnePasswordLoginBackend
    allow.install()
    (homes[0][1] / ".env").write_text(f"{allow.TOKEN_ENV}={KEY}\n")
    with allow._scope(""):
        assert not OnePasswordLoginBackend({"service_account_token_env": "MY_OWN_OP_TOKEN"})._service_token


# MARK: The job

def test_allow_then_pick_saves_the_key_for_every_bot(bots):
    data_dir, homes = bots
    job = allowed(data_dir)
    assert job["state"] == "choose" and [v["name"] for v in job["vaults"]] == ["Hermes", "Work"]
    assert job["account"] == "my.1password.com" and "email" not in str(job)
    made = {}
    allow.finish(job["job"], [VAULTS[0]["id"], "not-a-listed-vault"],
                 key_maker=lambda account, ids: made.setdefault("ids", ids) and KEY)
    done = settle(job["job"], "done", "failed")
    assert done["state"] == "done" and done["saved"] == 2 and made["ids"] == [VAULTS[0]["id"]]
    assert KEY not in str(done)
    for _, path in homes:
        assert allow.env_token(path) == KEY
    state = allow.state(data_dir)
    assert state["connected"] and state["ours"] and state["vaults"] == ["Hermes"] and state["bots"] == 2
    assert KEY not in (data_dir / allow.STATE).read_text()


def test_a_new_hermes_vault_when_asked(bots):
    data_dir, _ = bots
    job = allowed(data_dir)
    made = {}
    allow.finish(job["job"], [], create=True, vault_maker=lambda account: {"id": "newvault0000000000000000", "name": "Hermes", "items": 0},
                 key_maker=lambda account, ids: made.setdefault("ids", ids) and KEY)
    assert settle(job["job"], "done", "failed")["state"] == "done" and made["ids"] == ["newvault0000000000000000"]


def test_no_vault_picked_is_refused(bots):
    data_dir, _ = bots
    job = allowed(data_dir)
    with pytest.raises(allow.AllowError) as error:
        allow.finish(job["job"], ["nope"])
    assert error.value.code == "no-vault" and allow.job(job["job"])["state"] == "choose"


def test_not_allowed_says_so(bots):
    data_dir, _ = bots

    def denied(account):
        raise allow.problem("[ERROR] authorization prompt dismissed, please try again")
    started = allow.start(data_dir, lister=denied, account_lister=lambda: ACCOUNT)
    failed = settle(started["job"], "failed")
    assert failed["code"] == "denied" and "Allow" in failed["detail"]


def test_no_account_means_the_integration_is_off(bots):
    data_dir, _ = bots
    started = allow.start(data_dir, lister=lambda account: VAULTS, account_lister=lambda: [])
    assert settle(started["job"], "failed")["code"] == "integration-off"


def test_asking_again_while_1password_waits_is_the_same_request(bots):
    data_dir, _ = bots
    hold = __import__("threading").Event()
    started = allow.start(data_dir, lister=lambda account: hold.wait(5) and VAULTS, account_lister=lambda: ACCOUNT)
    again = allow.start(data_dir, lister=lambda account: VAULTS, account_lister=lambda: ACCOUNT)
    hold.set()
    assert again["job"] == started["job"] and settle(started["job"], "choose")


def test_a_bots_own_key_is_kept_and_disconnect_removes_only_ours(bots):
    data_dir, homes = bots
    (homes[1][1] / ".env").write_text(f"{allow.TOKEN_ENV}={OTHER}\n")
    job = allowed(data_dir)
    allow.finish(job["job"], [VAULTS[1]["id"]], key_maker=lambda account, ids: KEY)
    assert settle(job["job"], "done", "failed")["saved"] == 1
    assert allow.env_token(homes[0][1]) == KEY and allow.env_token(homes[1][1]) == OTHER
    assert allow.disconnect(data_dir) == {"ok": True, "removed": 1}
    assert allow.env_token(homes[0][1]) == "" and allow.env_token(homes[1][1]) == OTHER
    assert allow.state(data_dir)["connected"] is False


def test_allowing_again_replaces_our_own_key(bots):
    data_dir, homes = bots
    for key in (KEY, OTHER):
        job = allowed(data_dir)
        allow.finish(job["job"], [VAULTS[0]["id"]], key_maker=lambda account, ids, key=key: key)
        assert settle(job["job"], "done", "failed")["saved"] == 2
    assert allow.env_token(homes[0][1]) == OTHER


# MARK: From the phone: the account password, once, and nobody at the computer

CLI = [{"id": "ACCOUNTAAAAAAAAAAAAAAAAAAA", "url": "my.1password.example", "shorthand": "my", "user": "USERAAAAAAAAAAAAAAAAAAAAAA"}]


def test_a_session_keeps_the_1password_app_out_of_it():
    env = allow.op_env({"PATH": "/bin"}, session={"OP_SESSION_my": "tok"})
    assert env["OP_BIOMETRIC_UNLOCK_ENABLED"] == "false" and env["OP_SESSION_my"] == "tok"
    assert allow.op_env({"PATH": "/bin"}, app=False)["OP_BIOMETRIC_UNLOCK_ENABLED"] == "false"


def test_the_password_goes_on_stdin_and_makes_a_session(monkeypatch):
    seen = {}

    class Done:
        returncode, stdout, stderr = 0, "session-token\n", ""

    def run(args, timeout=0, app=True, secret=None):
        seen.update(args=args, app=app, secret=secret)
        return Done()
    monkeypatch.setattr(allow, "run_op", run)
    account, session = allow.sign_in("correct horse", lister=lambda: CLI)
    assert seen["args"] == ["signin", "--raw", "--account", "my"] and seen["app"] is False
    assert seen["secret"] == "correct horse" and "correct horse" not in " ".join(seen["args"])
    assert account["id"] == CLI[0]["id"]
    assert session == {"OP_SESSION_my": "session-token", "OP_SESSION_USERAAAAAAAAAAAAAAAAAAAAAA": "session-token"}


def test_a_wrong_password_says_so_and_no_account_means_the_computer(monkeypatch):
    class Refused:
        returncode, stdout, stderr = 1, "", "[ERROR] 2026/11/16 Unauthorized: (401) authentication required"
    monkeypatch.setattr(allow, "run_op", lambda *a, **k: Refused())
    with pytest.raises(allow.AllowError) as error:
        allow.sign_in("nope", lister=lambda: CLI)
    assert error.value.code == "password" and "nope" not in error.value.detail
    with pytest.raises(allow.AllowError) as error:
        allow.sign_in("anything", lister=lambda: [])
    assert error.value.code == "no-password"


def test_from_the_phone_the_whole_thing_runs_under_that_session(bots):
    data_dir, homes = bots
    session, under = {"OP_SESSION_my": "tok"}, []
    started = allow.start(data_dir, password="correct horse", signer=lambda password, account: (CLI[0], dict(session)),
                          lister=lambda account: under.append(dict(getattr(allow._signed_in, "session", None) or {})) or VAULTS)
    job = settle(started["job"], "choose", "failed")
    assert job["state"] == "choose" and under == [session]
    assert "tok" not in str(job) and "correct horse" not in str(job) and "session" not in job
    allow.finish(job["job"], [VAULTS[0]["id"]],
                 key_maker=lambda account, ids: under.append(dict(allow._signed_in.session or {})) or KEY)
    assert settle(job["job"], "done", "failed")["state"] == "done" and under == [session, session]
    assert "session" not in allow._jobs[job["job"]]            # the phone's sign-in ends with the key
    assert allow.env_token(homes[0][1]) == KEY


def test_a_password_from_the_phone_outranks_an_allow_nobody_clicks(bots):
    data_dir, _ = bots
    hold = __import__("threading").Event()
    waiting = allow.start(data_dir, lister=lambda account: hold.wait(5) and VAULTS, account_lister=lambda: ACCOUNT)
    by_phone = allow.start(data_dir, password="pw", signer=lambda password, account: (CLI[0], {}), lister=lambda account: VAULTS)
    hold.set()
    assert by_phone["job"] != waiting["job"] and settle(by_phone["job"], "choose")["state"] == "choose"


# MARK: The Private vault's logins, moved on the person's say

def test_private_logins_move_to_main_and_the_key_reads_main(bots):
    data_dir, homes = bots
    private = {"id": "privatevault0000000000001", "name": "Private", "logins": [f"item{n:022d}" for n in range(7)]}
    started = allow.start(data_dir, password="pw", signer=lambda password, account: (CLI[0], {"OP_SESSION_my": "tok"}),
                          lister=lambda account: [dict(v) for v in VAULTS], private_lister=lambda account: dict(private))
    job = settle(started["job"], "choose", "failed")
    assert job["private"] == {"name": "Private", "logins": 7} and "item000" not in str(job)   # how many, never which
    moved, made, keyed = [], [], {}

    def mover(account, item, source, destination):
        moved.append((item, source, destination, dict(allow._signed_in.session or {})))
        return item != private["logins"][3]                       # one refuses to move

    def maker(account, name=allow.NEW_VAULT):
        made.append(name)
        return {"id": "mainvault00000000000000001", "name": name, "items": 0}
    allow.finish(job["job"], [VAULTS[0]["id"]], move_private=True, mover=mover, vault_maker=maker,
                 key_maker=lambda account, ids: keyed.setdefault("ids", ids) and KEY)
    done = settle(job["job"], "done", "failed")
    assert done["state"] == "done" and done["moved"] == 6 and done["unmoved"] == 1 and done["total"] == 7
    assert made == ["Main"] and len(moved) == 7
    assert all(m[1] == private["id"] and m[2] == "mainvault00000000000000001" and m[3] == {"OP_SESSION_my": "tok"} for m in moved)
    assert keyed["ids"] == [VAULTS[0]["id"], "mainvault00000000000000001"]
    assert [v["name"] for v in done["vaults"]] == ["Hermes", "Main"] and "private" not in done


def test_private_alone_is_enough_and_an_existing_main_is_used(bots):
    data_dir, _ = bots
    with_main = [dict(v) for v in VAULTS] + [{"id": "mainvaultexisting000000001", "name": "Main", "items": 2}]
    started = allow.start(data_dir, password="pw", signer=lambda password, account: (CLI[0], {}),
                          lister=lambda account: with_main,
                          private_lister=lambda account: {"id": "privatevault0000000000001", "name": "Private", "logins": ["a" * 26]})
    job = settle(started["job"], "choose", "failed")
    keyed = {}
    allow.finish(job["job"], [], move_private=True, mover=lambda *a: True,
                 vault_maker=lambda *a: pytest.fail("Main is already there"),
                 key_maker=lambda account, ids: keyed.setdefault("ids", ids) and KEY)
    assert settle(job["job"], "done", "failed")["state"] == "done" and keyed["ids"] == ["mainvaultexisting000000001"]


def test_nothing_moves_unless_asked(bots):
    data_dir, _ = bots
    started = allow.start(data_dir, password="pw", signer=lambda password, account: (CLI[0], {}),
                          lister=lambda account: [dict(v) for v in VAULTS],
                          private_lister=lambda account: {"id": "privatevault0000000000001", "name": "Private", "logins": ["a" * 26]})
    job = settle(started["job"], "choose", "failed")
    allow.finish(job["job"], [VAULTS[0]["id"]], mover=lambda *a: pytest.fail("not asked to move"),
                 key_maker=lambda account, ids: KEY)
    done = settle(job["job"], "done", "failed")
    assert done["state"] == "done" and "moved" not in done


# MARK: One login out of Private, when a bot needs it

FOUND = [{"id": "itembestbuy00000000000001", "title": "Best Buy", "user": "me@example.com", "vault": "Private",
          "vault_id": "privatevault0000000000001"}]


def connected(data_dir):
    allow.write_state(data_dir, {"hashes": ["x"], "vaults": ["Shared", "Hermes"], "bots": ["default"]})


def test_a_site_is_matched_by_its_name_not_its_page():
    assert allow.site_host("https://www.bestbuy.com/identity/signin?x=1") == "bestbuy.com"
    assert allow.same_site("bestbuy.com", "https://www.bestbuy.com/") and allow.same_site("bestbuy.com", "bestbuy.com")
    assert allow.same_site("bestbuy.com", "https://signin.bestbuy.com/login")
    assert not allow.same_site("bestbuy.com", "https://notbestbuy.com") and not allow.same_site("bestbuy.com", "")


def test_only_logins_the_bots_cant_read_are_offered(monkeypatch):
    rows = [{"id": "itembestbuy00000000000001", "title": "Best Buy", "additional_information": "me@example.com",
             "vault": {"id": "privatevault0000000000001", "name": "Private"}, "urls": [{"href": "https://www.bestbuy.com"}]},
            {"id": "itembestbuy00000000000002", "title": "Best Buy (shared)", "vault": {"id": "v2", "name": "Hermes"},
             "urls": [{"href": "https://www.bestbuy.com"}]},
            {"id": "itemother0000000000000003", "title": "Bank", "vault": {"id": "privatevault0000000000001", "name": "Private"},
             "urls": [{"href": "https://bank.example"}]}]
    monkeypatch.setattr(allow, "op_json", lambda args, timeout=0: rows)
    found = allow.logins_for("ACCOUNT", "bestbuy.com", {"Hermes", "Shared"})
    assert [(m["title"], m["user"], m["vault"]) for m in found] == [("Best Buy", "me@example.com", "Private")]


def test_one_approval_moves_that_one_login_into_the_bots_vault(bots):
    data_dir, _ = bots
    connected(data_dir)
    started = allow.move_start(data_dir, "pw", "https://www.bestbuy.com/identity/signin",
                               signer=lambda password, account: (CLI[0], {"OP_SESSION_my": "tok"}),
                               finder=lambda account, host, readable: [dict(m) for m in FOUND] if host == "bestbuy.com" and readable == {"Shared", "Hermes"} else [])
    job = settle(started["job"], "found", "failed")
    assert job["state"] == "found" and job["site"] == "bestbuy.com"
    assert job["matches"] == [{"id": FOUND[0]["id"], "title": "Best Buy", "user": "me@example.com", "vault": "Private"}]
    assert "tok" not in str(job) and "pw" not in str(job) and "vault_id" not in str(job)
    moved = []
    with pytest.raises(allow.AllowError):
        allow.move_finish(job["job"], "not-one-of-them")                       # only a login it was shown
    allow.move_finish(job["job"], FOUND[0]["id"], destination=lambda account, readable: {"id": "hermesvault", "name": "Hermes"},
                      mover=lambda account, item, source, dest: moved.append((item, source, dest, dict(allow._signed_in.session or {}))) or True)
    done = settle(job["job"], "done", "failed")
    assert done["state"] == "done" and done["moved_to"] == "Hermes"
    assert moved == [(FOUND[0]["id"], "privatevault0000000000001", "hermesvault", {"OP_SESSION_my": "tok"})]   # that one, nothing else
    assert "session" not in allow._jobs[job["job"]]


def test_no_login_for_the_site_says_so_and_needs_the_bots_connected_first(bots):
    data_dir, _ = bots
    with pytest.raises(allow.AllowError) as error:
        allow.move_start(data_dir, "pw", "https://www.bestbuy.com")
    assert error.value.code == "not-connected"
    connected(data_dir)
    started = allow.move_start(data_dir, "pw", "https://www.bestbuy.com", signer=lambda p, a: (CLI[0], {}),
                               finder=lambda account, host, readable: [])
    failed = settle(started["job"], "failed")
    assert failed["code"] == "none" and "bestbuy.com" in failed["detail"]


def test_the_moved_login_goes_to_hermes_else_the_first_vault_the_key_reads(monkeypatch):
    monkeypatch.setattr(allow, "op_json", lambda args, timeout=0: [{"id": "a1", "name": "Shared"}, {"id": "h1", "name": "Hermes"}])
    assert allow.key_vault("ACCOUNT", ["Shared", "Hermes"]) == {"id": "h1", "name": "Hermes"}
    assert allow.key_vault("ACCOUNT", ["Shared"]) == {"id": "a1", "name": "Shared"}
    with pytest.raises(allow.AllowError):
        allow.key_vault("ACCOUNT", ["Gone"])
