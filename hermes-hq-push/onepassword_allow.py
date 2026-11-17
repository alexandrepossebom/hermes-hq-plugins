"""Allow in 1Password (0.9.59): one click in the 1Password app instead of the master password, and it lasts.

Hermes unlocks 1Password with the master password, typed into a prompt, and forgets it after half an hour idle; a bot
that needs a login asks again. The owner (2026-11-16) wanted what another agent app does: 1Password itself pops up
"Allow", one click, and the bots have access from then on.

How it works, all with 1Password's own command-line tool (``op``) on this computer:

1. ``op vault list`` with no saved sign-in makes the 1Password app ask the person at this computer to allow it
   (Touch ID or a click; "Integrate with 1Password CLI" in 1Password › Settings › Developer). No master password.
2. The app shows the vaults a key can be made for (1Password never lets one read the Private vault), and the person
   picks; or a new vault named Hermes is made for them to move logins into.
3. ``op service-account create … --vault <id>:read_items --raw`` makes a read-only key for those vaults only. It goes
   into each bot's ``.env`` as ``OP_SERVICE_ACCOUNT_TOKEN``, the name Hermes's 1Password backend already reads: with
   it the backend is never locked.

The key is written by Hermes's own ``save_env_value`` and never sent to the app, logged, or kept anywhere else; this
module's own file (``onepassword-allow.json``) holds the vault names and a hash of the key, so Disconnect removes only
a key it wrote. A bot whose ``.env`` already has a different key keeps it.

``install()`` makes Hermes's backend read a bot's ``.env`` when it is built, so a key saved a moment ago works in the
turn that is waiting and in the messaging gateway's process, with no restart; and lets the app answer the unlock card
a bot is waiting on with ``ALLOWED`` in place of a master password once the key is saved.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import secrets
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("hermes-hq-push")

STATE = "onepassword-allow.json"
TOKEN_ENV = "OP_SERVICE_ACCOUNT_TOKEN"
# What the app answers the unlock card with once the key is saved. Never a real master password: 1Password's are typed
# by a person, and this has a control character no keyboard field sends.
ALLOWED = "\x1dhermes-hq:allowed-in-1password"
# What the app answers the unlock card with when the person is on their phone, away from this computer, and will give
# the login from 1Password there (owner, 2026-11-16: "it needs to open up in the 1Password app on my phone because
# that's where I am"). 1Password's own Allow only ever shows on the computer; the phone's 1Password fills the app's
# login sheet. So the unlock is refused with words that send the bot to ask for that login (``ON_PHONE``).
PHONE = "\x1dhermes-hq:fill-on-phone"
ON_PHONE = ("Not unlocked: the person is on their phone, away from this computer. Ask for this login instead: call "
            "browser_vault action=save_login with origin set to the sign-in page's URL. They will fill it from "
            "1Password on their phone, and it is saved for that site.")
MARK = "_hermes_hq_onepassword_allow"
NEW_VAULT = "Hermes"
# Where the Private vault's logins go when the person asks for them (owner, 2026-11-16: "We need the main vault to
# work"). 1Password never lets a key read the Private vault itself, so its logins are moved, on the person's say, into
# a normal vault of theirs with this name, which the key reads.
MAIN_VAULT = "Main"
MOVERS = 3
WAIT = 180.0        # how long the 1Password app's "Allow" may sit unanswered
QUICK = 30.0
JOB_LIFE = 15 * 60
_TOKEN = re.compile(r"ops_[A-Za-z0-9_=-]{20,4096}")
_ID = re.compile(r"[A-Za-z0-9]{10,64}")
# Vault kinds a key can't be made for (op: "You can't grant a service account access to your Personal or Private vault").
_CLOSED = {"PERSONAL", "PRIVATE", "EVERYONE", "TRANSFER"}
_ENV_KEEP = ("PATH", "HOME", "USER", "LOGNAME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "SystemRoot", "TMPDIR", "TMP",
             "TEMP", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR", "OP_CONFIG_DIR")

_jobs: dict[str, dict] = {}
_lock = threading.Lock()


class AllowError(Exception):
    def __init__(self, status_code: int, detail: str, code: str = "failed"):
        super().__init__(detail)
        self.status_code, self.detail, self.code = status_code, detail, code


# MARK: op

def find_op() -> str | None:
    import shutil
    found = shutil.which("op")
    if found:
        return found
    for path in ("/opt/homebrew/bin/op", "/usr/local/bin/op", "/usr/bin/op"):
        if os.access(path, os.X_OK):
            return path
    return None


# The sign-in this thread's ``op`` calls run under: None = through the 1Password app (it shows Allow on this computer);
# a dict = a session made from the account password sent from the phone (``sign_in``), with the app left out of it.
_signed_in = threading.local()


class using:
    """``with using(session):`` this thread's op calls use that session (None: the 1Password app)."""
    def __init__(self, session: dict | None):
        self.session = session

    def __enter__(self):
        self.before = getattr(_signed_in, "session", None)
        _signed_in.session = self.session

    def __exit__(self, *exc):
        _signed_in.session = self.before


def op_env(source: dict | None = None, session: dict | None = None, app: bool = True) -> dict:
    """What ``op`` runs with: the person's own sign-in, through the 1Password app or (``session``, or ``app`` False)
    without it. Any saved key, other session or Connect server is left out, or op would use that instead."""
    source = os.environ if source is None else source
    env = {key: source[key] for key in _ENV_KEEP if key in source}
    env["NO_COLOR"] = "1"
    env["OP_BIOMETRIC_UNLOCK_ENABLED"] = "true" if app and session is None else "false"
    env.update(session or {})
    return env


def scrub(text: str) -> str:
    """op's words with no colour codes and never a key."""
    text = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", str(text or "")).replace("\x1b", "")
    return _TOKEN.sub("ops_…", text).strip()


def run_op(args: list[str], timeout: float = QUICK, app: bool = True, secret: str | None = None) -> subprocess.CompletedProcess:
    op = find_op()
    if op is None:
        raise AllowError(409, "1Password’s command-line tool isn’t installed on this computer.", "no-cli")
    env = op_env(session=getattr(_signed_in, "session", None), app=app)
    try:
        if secret is not None:   # on stdin, never in the arguments
            return subprocess.run([op, *args], env=env, input=secret, capture_output=True, text=True, timeout=timeout)
        return subprocess.run([op, *args], env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AllowError(408, "1Password didn’t answer in time. Open 1Password on this computer and try again.",
                         "timeout") from None
    except OSError as error:
        raise AllowError(500, f"1Password’s command-line tool didn’t start ({type(error).__name__}).", "no-cli") from None


def problem(stderr: str) -> AllowError:
    """op's failure in the app's words."""
    text = scrub(stderr)
    low = text.lower()
    if "authorization timeout" in low:
        return AllowError(408, "1Password asked on this computer and nobody answered within a minute. Try again when "
                               "you’re at it.", "timeout")
    if any(word in low for word in ("authorization prompt dismissed", "authorization denied", "not approved",
                                    "user cancel", "prompt dismissed")):
        return AllowError(403, "1Password wasn’t allowed. Try again, then click Allow in 1Password.", "denied")
    if any(word in low for word in ("connecting to desktop app", "desktop app integration", "connect to the 1password",
                                    "1password app", "biometric", "no accounts configured", "not currently signed in",
                                    "account is not signed in")):
        return AllowError(409, "1Password isn’t ready on this computer. Open 1Password, then turn on Settings › "
                               "Developer › Integrate with 1Password CLI.", "integration-off")
    if any(word in low for word in ("service account limit", "service-account limit", "maximum number of service accounts",
                                    "too many service accounts")):
        return AllowError(409, "This 1Password account has no room for another service account. Remove one at "
                               "1Password.com › Developer › Service Accounts, then try again.", "limit")
    if any(word in low for word in ("permission", "not allowed", "not authorized", "not permitted", "(403)", "forbidden",
                                    "can't create service accounts", "cannot create service accounts",
                                    "service accounts are not available", "service accounts aren't available")):
        return AllowError(403, "This 1Password account can’t make a key for bots. An owner or administrator has to "
                               "allow service accounts for it.", "permission")
    return AllowError(502, (text.splitlines() or ["1Password’s command-line tool failed."])[-1][:300], "op-error")


def op_json(args: list[str], timeout: float = QUICK):
    done = run_op(args, timeout)
    if done.returncode != 0:
        raise problem(done.stderr)
    try:
        return json.loads(done.stdout or "null")
    except ValueError:
        raise AllowError(502, "1Password answered with something this add-on couldn’t read.", "op-error") from None


def accounts() -> list[dict]:
    """The accounts the 1Password app on this computer is signed in to. Asks nobody anything."""
    raw = op_json(["account", "list", "--format=json"])
    out = []
    for row in raw if isinstance(raw, list) else []:
        if isinstance(row, dict) and row.get("account_uuid"):
            out.append({"id": str(row["account_uuid"]), "url": str(row.get("url") or ""),
                        "email": str(row.get("email") or "")})
    return out


# MARK: From the phone, with nobody at this computer

def cli_accounts() -> list[dict]:
    """The accounts 1Password's tool itself knows (added to it once, with their Secret Key): the ones it can sign in
    to with the account password alone, no 1Password app and nobody at this computer."""
    done = run_op(["account", "list", "--format=json"], app=False)
    if done.returncode != 0:
        return []
    try:
        raw = json.loads(done.stdout or "null")
    except ValueError:
        return []
    out = []
    for row in raw if isinstance(raw, list) else []:
        if isinstance(row, dict) and row.get("account_uuid") and row.get("shorthand"):
            out.append({"id": str(row["account_uuid"]), "url": str(row.get("url") or "").replace("https://", ""),
                        "shorthand": str(row["shorthand"]), "user": str(row.get("user_uuid") or "")})
    return out


_cli_known = {"at": 0.0, "ok": False}


def password_works() -> bool:
    """Whether a password sent from the phone can sign in here (kept a minute: the Passwords page asks often)."""
    if time.time() - _cli_known["at"] > 60:
        try:
            _cli_known.update(at=time.time(), ok=bool(cli_accounts()))
        except AllowError:
            _cli_known.update(at=time.time(), ok=False)
    return bool(_cli_known["ok"])


def sign_in(password: str, account: str = "", lister=cli_accounts) -> tuple[dict, dict]:
    """Sign in to 1Password with the account password, as the person would in a terminal: (the account, the session
    for ``using``). The password goes to op on stdin and is not kept; the session lives in memory until the key is
    made (owner, 2026-11-16: "It can't go to the computer. No one is ever on their computer")."""
    found = lister()
    chosen = next((row for row in found if row["id"] == account), None) if account else (found[0] if found else None)
    if chosen is None:
        raise AllowError(409, "1Password on this computer can’t be signed in to from here. Allow it at the computer "
                              "instead.", "no-password")
    with using(None):
        done = run_op(["signin", "--raw", "--account", chosen["shorthand"]], WAIT, app=False, secret=password)
    token = (done.stdout or "").strip()
    if done.returncode != 0 or not token:
        low = scrub(done.stderr).lower()
        if any(word in low for word in ("unauthorized", "(401)", "incorrect", "invalid", "authentication", "wrong password")):
            raise AllowError(401, "1Password didn’t take that password. Try again.", "password")
        raise problem(done.stderr or "1Password didn’t sign in.")
    names = {chosen["shorthand"], chosen["user"]} - {""}
    return chosen, {f"OP_SESSION_{name}": token for name in names}


def open_vaults(account: str) -> list[dict]:
    """The vaults a key can be made for, by name. The first call here is the one 1Password asks the person to allow."""
    raw = op_json(["vault", "list", "--account", account, "--format=json"], WAIT)
    out = []
    _signed_in.private = None
    for row in (raw if isinstance(raw, list) else [])[:60]:
        if not isinstance(row, dict) or not _ID.fullmatch(str(row.get("id") or "")):
            continue
        kind, items = str(row.get("type") or "").upper(), row.get("items")
        if not kind:
            try:
                more = op_json(["vault", "get", str(row["id"]), "--account", account, "--format=json"])
            except AllowError:
                more = None
            if isinstance(more, dict):
                kind, items = str(more.get("type") or "").upper(), more.get("items", items)
        if kind in ("PERSONAL", "PRIVATE"):
            _signed_in.private = {"id": str(row["id"]), "name": str(row.get("name") or "Private")[:120]}
        if kind in _CLOSED:
            continue
        out.append({"id": str(row["id"]), "name": str(row.get("name") or "Vault")[:120],
                    "items": items if isinstance(items, int) else None})
    out.sort(key=lambda vault: (vault["name"].lower() != NEW_VAULT.lower(), vault["name"].lower()))
    return out


def private_logins(account: str) -> dict | None:
    """The Private vault the last ``open_vaults`` on this thread saw, with its logins: {id, name, logins: [ids]}."""
    found = getattr(_signed_in, "private", None)
    if not found:
        return None
    raw = op_json(["item", "list", "--vault", found["id"], "--categories", "Login", "--account", account,
                   "--format=json"], WAIT)
    ids = [str(row["id"]) for row in raw if isinstance(row, dict) and _ID.fullmatch(str(row.get("id") or ""))] \
        if isinstance(raw, list) else []
    return {**found, "logins": ids}


def move_login(account: str, item: str, source: str, destination: str) -> bool:
    done = run_op(["item", "move", item, "--current-vault", source, "--destination-vault", destination,
                   "--account", account], WAIT)
    return done.returncode == 0


def make_vault(account: str, name: str = NEW_VAULT) -> dict:
    raw = op_json(["vault", "create", name, "--account", account, "--format=json",
                   "--description", "Logins your Hermes bots may fill."], WAIT)
    if not isinstance(raw, dict) or not _ID.fullmatch(str(raw.get("id") or "")):
        raise AllowError(502, "1Password didn’t say which vault it made.", "op-error")
    return {"id": str(raw["id"]), "name": str(raw.get("name") or name), "items": 0}


def key_name(now: float | None = None) -> str:
    host = re.sub(r"[^A-Za-z0-9 ._-]+", "", platform.node().split(".")[0])[:40].strip() or "computer"
    return f"Hermes HQ {host} {time.strftime('%Y-%m-%d %H%M', time.localtime(now))}"


def make_key(account: str, vault_ids: list[str]) -> str:
    """A read-only key for these vaults. op prints it once; it is returned to the caller and nowhere else."""
    args = ["service-account", "create", key_name(), "--account", account, "--raw"]
    for vault in vault_ids:
        args += ["--vault", f"{vault}:read_items"]
    done = run_op(args, WAIT)
    if done.returncode != 0:
        raise problem(done.stderr)
    found = _TOKEN.search(done.stdout or "")
    if not found:
        raise AllowError(502, "1Password didn’t hand back a key.", "op-error")
    return found.group(0)


# MARK: Where the key is kept

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def read_state(data_dir: Path) -> dict:
    try:
        state = json.loads((Path(data_dir) / STATE).read_text())
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(data_dir: Path, state: dict) -> None:
    path = Path(data_dir) / STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def bot_homes() -> list[tuple[str, Path]]:
    """Every bot on this computer: (profile name, its folder)."""
    from hermes_cli.profiles import list_profiles
    out = []
    for info in list_profiles(lazy_skill_count=True):
        name, path = str(getattr(info, "name", "") or ""), getattr(info, "path", None)
        if name and path:
            out.append((name, Path(path)))
    return out


def env_token(home: Path) -> str:
    """The key in a bot's own ``.env``, read from the file (Hermes's reader, which notices a change at once)."""
    from agent.secret_scope import load_env_file
    return str(load_env_file(Path(home) / ".env").get(TOKEN_ENV) or "").strip()


def _scope(profile: str):
    from hermes_cli.web_server_profiles import _config_profile_scope
    return _config_profile_scope(profile)


def save_key(data_dir: Path, token: str, homes=None) -> list[str]:
    """Write the key into every bot's ``.env``, except a bot that has a different key someone set by hand. Answers
    the bots that got it."""
    from hermes_cli.config import save_env_value
    ours = set(read_state(data_dir).get("hashes") or [])
    saved = []
    for name, home in (bot_homes() if homes is None else homes):
        had = env_token(home)
        if had and had != token and _hash(had) not in ours:
            log.info("hermes-hq-push: 1Password key not written for %s: it has its own", name)
            continue
        with _scope(name):
            save_env_value(TOKEN_ENV, token)
        saved.append(name)
    return saved


def remove_key(data_dir: Path, homes=None) -> list[str]:
    """Disconnect: take the key this add-on wrote out of every bot's ``.env``. Another key is left alone."""
    from hermes_cli.config import remove_env_value
    state = read_state(data_dir)
    ours = set(state.get("hashes") or [])
    removed = []
    for name, home in (bot_homes() if homes is None else homes):
        had = env_token(home)
        if had and _hash(had) in ours:
            with _scope(name):
                remove_env_value(TOKEN_ENV)
            removed.append(name)
    write_state(data_dir, {})
    return removed


# MARK: What the app asks

def supported() -> bool:
    return find_op() is not None


def status() -> dict:
    """`onePasswordAllow` (0.9.59): POST /vault/onepassword/allow starts the 1Password app's own "Allow"."""
    return {"onePasswordAllow": supported()}


def state(data_dir: Path, profile: str = "") -> dict:
    """What the Passwords page shows. Network-free and asks 1Password nothing."""
    saved = read_state(data_dir)
    try:
        with _scope(profile):
            from hermes_constants import get_hermes_home
            token = env_token(get_hermes_home())
    except Exception:
        token = ""
    ours = bool(token) and _hash(token) in set(saved.get("hashes") or [])
    return {"ok": True, "supported": supported(), "phone": True, "password": supported() and password_works(),
            "connected": bool(token), "ours": ours,
            "vaults": [str(name) for name in saved.get("vaults") or []] if ours else [],
            "name": str(saved.get("name") or "") if ours else "",
            "bots": len(saved.get("bots") or []) if ours else 0}


def _public(job: dict) -> dict:
    out = {key: job[key] for key in ("job", "state", "vaults", "detail", "code", "saved", "account", "moved", "total",
                                     "unmoved") if key in job}
    if job.get("private"):   # how many logins the Private vault holds, never which
        out["private"] = {"name": job["private"]["name"], "logins": len(job["private"]["logins"])}
    return out


def start(data_dir: Path, account: str = "", lister=open_vaults, account_lister=accounts, password: str | None = None,
          signer=sign_in, private_lister=private_logins) -> dict:
    """Ask 1Password. Answers a job at once: ``waiting`` while the 1Password app shows Allow (or, with ``password``
    from the phone, while 1Password signs in with it and nobody is needed at this computer), then ``choose`` with
    the vaults, or ``failed`` with why."""
    if not supported():
        raise AllowError(409, "1Password’s command-line tool isn’t installed on this computer.", "no-cli")
    key = secrets.token_urlsafe(12)
    job = {"job": key, "state": "waiting", "at": time.time(), "data_dir": str(data_dir)}
    with _lock:
        for old in [k for k, v in _jobs.items() if time.time() - v["at"] > JOB_LIFE]:
            _jobs.pop(old, None)
        # The app asked again while 1Password still shows Allow (the sheet was closed and opened): the same request.
        asked = next((v for v in _jobs.values() if v["state"] == "waiting"), None)
        if asked is not None and password is None and not asked.get("by_password"):
            return _public(asked)
        if asked is not None and not asked.get("by_password"):
            _jobs.pop(asked["job"], None)   # a password from the phone outranks an Allow nobody is there to click
        elif asked is not None:
            raise AllowError(409, "1Password is signing in. Give it a moment.", "busy")
        if any(v["state"] in ("making", "moving") for v in _jobs.values()):
            raise AllowError(409, "A key is being saved. Give it a moment.", "busy")
        if password is not None:
            job["by_password"] = True
        _jobs[key] = job
    secret = password

    def work():
        nonlocal secret
        try:
            session = None
            if secret is not None:
                try:
                    chosen, session = signer(secret, account)
                finally:
                    secret = None
            else:
                found = account_lister()
                chosen = next((row for row in found if row["id"] == account), None) if account else (found[0] if found else None)
                if chosen is None:
                    raise AllowError(409, "1Password isn’t ready on this computer. Open 1Password, then turn on Settings › "
                                          "Developer › Integrate with 1Password CLI.", "integration-off")
            with using(session):
                vaults = lister(chosen["id"])
                private = None
                if private_lister is not private_logins:   # not listed from the app: nothing there moves a vault now
                    try:
                        private = private_lister(chosen["id"])
                    except AllowError:
                        private = None
            with _lock:
                job.update(state="choose", vaults=vaults, account=chosen["url"], account_id=chosen["id"], at=time.time(),
                           session=session, private=private if private and private.get("logins") else None)
        except AllowError as error:
            with _lock:
                job.update(state="failed", detail=error.detail, code=error.code)
        except Exception as error:
            log.warning("hermes-hq-push: 1Password allow failed (%s)", type(error).__name__)
            with _lock:
                job.update(state="failed", detail="1Password couldn’t be asked on this computer.", code="failed")

    first = _public(job)
    threading.Thread(target=work, name="hq-onepassword-allow", daemon=True).start()
    return first


def finish(key: str, vault_ids: list[str], create: bool = False, vault_maker=make_vault, key_maker=make_key,
           saver=save_key, move_private: bool = False, mover=move_login) -> dict:
    """The person picked: make the key for those vaults (and a new Hermes vault when asked) and save it. With
    ``move_private`` the Private vault's logins are first moved into their Main vault (made if there is none), which
    the key then reads."""
    with _lock:
        job = _jobs.get(key)
        if job is None:
            raise AllowError(404, "That request is gone. Start again.", "gone")
        if job["state"] != "choose":
            raise AllowError(409, "That request isn’t waiting for a vault.", "busy")
        known = {vault["id"]: vault for vault in job.get("vaults") or []}
        picked = [vault for vault in dict.fromkeys(vault_ids) if vault in known]
        private = job.get("private") if move_private else None
        if not picked and not create and not private:
            raise AllowError(400, "Pick a vault.", "no-vault")
        job.update(state="making", at=time.time())
    account, data_dir = job["account_id"], Path(job["data_dir"])

    def work():
        try:
            names = [known[vault]["name"] for vault in picked]
            ids = list(picked)
            session = job.pop("session", None)   # the phone's sign-in ends with the key
            with using(session):
                if create:
                    made = vault_maker(account)
                    ids.append(made["id"])
                    names.append(made["name"])
                if private:
                    main = next((v for v in known.values() if v["name"].lower() == MAIN_VAULT.lower()), None) \
                        or vault_maker(account, MAIN_VAULT)
                    if main["id"] not in ids:
                        ids.append(main["id"])
                        names.append(main["name"])
                    logins = list(private["logins"])
                    with _lock:
                        job.update(state="moving", moved=0, total=len(logins), unmoved=0, at=time.time())

                    def move(item):
                        with using(session):
                            try:
                                ok = mover(account, item, private["id"], main["id"])
                            except AllowError:
                                ok = False
                        with _lock:
                            job["moved" if ok else "unmoved"] += 1
                            job["at"] = time.time()

                    from concurrent.futures import ThreadPoolExecutor
                    with ThreadPoolExecutor(max_workers=MOVERS, thread_name_prefix="hq-onepassword-move") as pool:
                        list(pool.map(move, logins))
                    with _lock:
                        job.update(state="making", at=time.time())
                token = key_maker(account, ids)
            try:
                saved = read_state(data_dir)
                write_state(data_dir, {"hashes": [*(saved.get("hashes") or [])[-4:], _hash(token)], "vaults": names,
                                       "name": key_name(), "account": job.get("account", ""), "made": int(time.time())})
                bots = saver(data_dir, token)
                write_state(data_dir, {**read_state(data_dir), "bots": bots})
            finally:
                del token
            if not bots:
                raise AllowError(409, "Every bot here already has a 1Password key of its own, so nothing was changed.",
                                 "kept")
            with _lock:
                job.update(state="done", saved=len(bots), vaults=[{"id": i, "name": n} for i, n in zip(ids, names)],
                           at=time.time())
                job.pop("private", None)
        except AllowError as error:
            with _lock:
                job.update(state="failed", detail=error.detail, code=error.code)
        except Exception as error:
            log.warning("hermes-hq-push: 1Password key not saved (%s)", type(error).__name__)
            with _lock:
                job.update(state="failed", detail="The key couldn’t be saved on this computer.", code="failed")

    first = _public(job)
    threading.Thread(target=work, name="hq-onepassword-key", daemon=True).start()
    return first


def job(key: str) -> dict:
    with _lock:
        found = _jobs.get(key)
        if found is None:
            raise AllowError(404, "That request is gone. Start again.", "gone")
        return _public(found)


def disconnect(data_dir: Path) -> dict:
    removed = remove_key(data_dir)
    return {"ok": True, "removed": len(removed)}


# MARK: Hermes's 1Password backend

def install() -> str:
    """Hermes's backend reads the key from the bot's ``.env`` when it has none (a key saved a moment ago, by another
    process), and takes ``ALLOWED`` on the unlock card in place of a master password once that key is there."""
    try:
        from agent.vault_backends.onepassword import OnePasswordLoginBackend as Backend
    except Exception:
        return "unavailable"
    if getattr(Backend, MARK, False):
        return "already"
    built, unlock = Backend.__init__, Backend.unlock

    def fresh(self) -> str:
        try:
            from hermes_constants import get_hermes_home
            if str(self.cfg.get("service_account_token_env") or TOKEN_ENV) != TOKEN_ENV:
                return ""
            return env_token(get_hermes_home())
        except Exception:
            return ""

    def __init__(self, *args, **kwargs):
        built(self, *args, **kwargs)
        if not getattr(self, "_service_token", ""):
            self._service_token = fresh(self)

    def allowed_unlock(self, master_password):
        if master_password == PHONE:
            raise RuntimeError(ON_PHONE)
        if master_password != ALLOWED:
            return unlock(self, master_password)
        if not getattr(self, "_service_token", ""):
            self._service_token = fresh(self)
        if not self._service_token:
            raise RuntimeError("1Password isn’t connected yet. Allow it in 1Password again.")

    Backend.__init__, Backend.unlock = __init__, allowed_unlock
    setattr(Backend, MARK, True)
    return "installed"
