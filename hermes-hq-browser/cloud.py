"""Browser Use Cloud for a bot (Bot Info › Browser › Browser Use Cloud on the phone).

A bot set to it browses in a cloud browser on the person's own Browser Use account (https://browser-use.com)
instead of a Chrome on this machine: ``browser.cloud_provider: hq-browser-use`` in that bot's config.yaml, served by
the provider below. Hermes drives it with the same browser tools; agent.py lists it like any other browser, so Watch
Browser, Take Control and ``browser_ask_user`` work on it.

Why not Hermes' own ``browser-use`` provider: it starts every browser with no profile (signed out each time). This
one gives each bot a Browser Use profile of its own, so sign-ins stay.

The person's API key and each bot's profile id live in ``<hermes root>/hermes-hq-browser/cloud.json`` (owner-only),
written by the dashboard, read by every agent process. The key never leaves this machine except to Browser Use.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path

log = logging.getLogger("hermes-hq-browser")

PROVIDER = "hq-browser-use"
API = "https://api.browser-use.com/api/v4"
SCHEMA = "hermes-hq-browser.cloud.v1"
TIMEOUT_MINUTES = 120   # the longest a browser Hermes forgot to close keeps running (and billing); 240 is their cap
WINDOW = (1280, 1056)   # the screen; the page gets about 1280 x 800 of it, the size a bot's own Chrome has
KEY = re.compile(r"^[A-Za-z0-9_\-.]{16,256}$")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_lock = threading.Lock()


class CloudError(Exception):
    """Plain words for the person (or the bot)."""


def _root() -> Path:
    from hermes_constants import get_default_hermes_root
    return Path(get_default_hermes_root())


def store_path(root: Path | None = None) -> Path:
    folder = (root or _root()) / "hermes-hq-browser"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    return folder / "cloud.json"


def read_store(root: Path | None = None) -> dict:
    try:
        path = store_path(root)
        if path.is_symlink() or path.stat().st_uid != os.getuid():
            return {}
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_store(value: dict, root: Path | None = None) -> None:
    path = store_path(root)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(value, handle)
    os.replace(temp, path)


def api_key(root: Path | None = None) -> str | None:
    """The key saved from Hermes HQ, else Hermes' own ``BROWSER_USE_API_KEY``."""
    saved = read_store(root).get("key")
    if isinstance(saved, str) and KEY.match(saved):
        return saved
    try:
        from agent.secret_scope import get_secret
        found = get_secret("BROWSER_USE_API_KEY")
    except Exception:  # noqa: BLE001 - no scope here, or an older Hermes
        found = os.environ.get("BROWSER_USE_API_KEY")
    return found if isinstance(found, str) and KEY.match(found.strip()) else None


def call(method: str, path: str, body: dict | None = None, *, key: str, timeout: float = 30.0):
    """One Browser Use API call. Raises ``CloudError`` in plain words."""
    request = urllib.request.Request(
        API + path, method=method, data=None if body is None else json.dumps(body).encode(),
        headers={"X-Browser-Use-API-Key": key, "Content-Type": "application/json", "User-Agent": "hermes-hq-browser"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - a fixed https host
            raw = response.read(1 << 20)
        return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        raise CloudError(_explain(error.code)) from None
    except (OSError, ValueError):
        raise CloudError("Couldn’t reach Browser Use. Check the connection and try again.") from None


def _explain(status: int) -> str:
    return {
        401: "Browser Use didn’t accept the API key. Check it in Hermes HQ › Bot Info › Browser.",
        403: "Browser Use didn’t accept the API key. Check it in Hermes HQ › Bot Info › Browser.",
        402: "The Browser Use account is out of credits (or hit its spending limit). Add credits at browser-use.com.",
        429: "Too many Browser Use browsers are open at once on this account. Close one, or try again in a minute.",
    }.get(status, f"Browser Use refused the request ({status}).")


def check_key(key: str) -> None:
    """Raises ``CloudError`` unless Browser Use accepts this key."""
    if not KEY.match(key or ""):
        raise CloudError("That doesn’t look like a Browser Use API key.")
    call("GET", "/profiles?pageSize=1", key=key, timeout=15.0)


def save_key(key: str | None, root: Path | None = None) -> dict:
    """Save (after asking Browser Use that it works) or, with None/"", forget the key. Bots' profile ids are kept:
    they belong to the account, and the same key may come back."""
    key = (key or "").strip()
    if key:
        check_key(key)
    with _lock:
        value = read_store(root)
        if key:
            if value.get("key") != key:
                value["profiles"] = {}  # another account: its profiles are not these
            value["key"] = key
        else:
            value.pop("key", None)
        write_store(value, root)
    return status(root)


def status(root: Path | None = None) -> dict:
    key = api_key(root)
    return {"schema": SCHEMA, "provider": PROVIDER, "key_set": key is not None,
            "key_hint": f"…{key[-4:]}" if key else None}


def profile_id(profile: str, *, key: str, root: Path | None = None) -> str | None:
    """The bot's Browser Use profile (its cookies and sign-ins, kept between browsers), made the first time. None
    when it can't be made right now: the bot still gets a browser, without saved sign-ins."""
    with _lock:
        known = (read_store(root).get("profiles") or {}).get(profile)
        if isinstance(known, str) and UUID.match(known):
            return known
        try:
            made = call("POST", "/profiles", {"name": f"Hermes HQ: {profile}"[:100]}, key=key).get("id")
        except CloudError:
            log.warning("hermes-hq-browser: couldn't make %s's Browser Use profile", profile)
            return None
        if not (isinstance(made, str) and UUID.match(made)):
            return None
        value = read_store(root)
        value.setdefault("profiles", {})[profile] = made
        write_store(value, root)
        return made


def _profile_name() -> str:
    from hermes_constants import get_default_hermes_root, get_hermes_home
    home, default = Path(get_hermes_home()).resolve(), Path(get_default_hermes_root()).resolve()
    return "default" if home == default else home.name


def provider():
    """The Hermes browser provider (built here: its base class only exists inside Hermes)."""
    from agent.browser_provider import BrowserProvider

    class HermesHQBrowserUse(BrowserProvider):
        @property
        def name(self) -> str:
            return PROVIDER

        @property
        def display_name(self) -> str:
            return "Browser Use Cloud (Hermes HQ)"

        def is_available(self) -> bool:
            return api_key() is not None

        def create_session(self, task_id: str) -> dict:
            key = api_key()
            if key is None:
                raise ValueError("Browser Use Cloud needs an API key. Add it in Hermes HQ › Bot Info › Browser.")
            bot = _profile_name()
            body = {"timeout": TIMEOUT_MINUTES, "proxyCountryCode": None,
                    "browserScreenWidth": WINDOW[0], "browserScreenHeight": WINDOW[1],
                    "metadata": {"source": "hermes-hq", "bot": bot[:64]}}
            saved = profile_id(bot, key=key)
            if saved:
                body["profileId"] = saved
            try:
                made = call("POST", "/browsers", body, key=key)
            except CloudError as error:
                raise RuntimeError(str(error)) from None
            if not made.get("id") or not made.get("cdpUrl"):
                raise RuntimeError("Browser Use didn’t start a browser. Try again.")
            log.info("hermes-hq-browser: started a Browser Use browser for %s", bot)
            return {"session_name": f"hq_{task_id}_{uuid.uuid4().hex[:8]}", "bb_session_id": made["id"],
                    "cdp_url": made["cdpUrl"], "expires_at": made.get("timeoutAt"),
                    "features": {"browser_use": True, "hermes_hq": True}}

        def close_session(self, session_id: str) -> bool:
            return _stop(session_id, timeout=10.0)

        def emergency_cleanup(self, session_id: str) -> None:
            _stop(session_id, timeout=5.0)

    return HermesHQBrowserUse()


def _stop(session_id: str, timeout: float) -> bool:
    """Stopping is also what saves the bot's sign-ins into its Browser Use profile."""
    key = api_key()
    if key is None or not UUID.match(str(session_id or "")):
        return False
    try:
        call("PATCH", f"/browsers/{session_id}", {"action": "stop"}, key=key, timeout=timeout)
        return True
    except CloudError:
        log.warning("hermes-hq-browser: couldn't stop Browser Use browser %s", session_id)
        return False
