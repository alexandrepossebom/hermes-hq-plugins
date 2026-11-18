"""Live check: a one-time code asked for a sign-in that sits in a frame reaches that frame (gateway_logins, 0.9.62).

DoorDash's sign-in is a frame from identity.doordash.com inside www.doordash.com. Chrome keeps a same-site frame in
the page's own process, so it has no target of its own and Hermes's vault (which only looks at whole tabs) never saw
its code boxes: naming identity.doordash.com was "no browser tab is open", naming www.doordash.com was "no code
field" (owner, 2026-11-18). This runs the real Hermes vault tool, its real CDP supervisor and a real headless Chrome
against two local pages built the same way: a same-site frame (as DoorDash) and a cross-site frame (its own process).
Only the person is replaced: the code prompt answers with a made-up code and records the site it named.

Run with Hermes's own python (it is kept out of the add-on's folder, which ships to people):
    ~/.hermes/hermes-agent/venv/bin/python tests/hermes-hq-push/live_login_in_frame.py [path/to/gateway_logins.py]
It prints one line per case and exits 1 when a case fails. Nothing touches the computer's own ~/.hermes or browser.
"""
from __future__ import annotations

import http.server
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

AGENT = os.environ.get("HERMES_AGENT", "")
AGENT = AGENT if os.path.isdir(AGENT) else os.path.expanduser("~/.hermes/hermes-agent")
PLUGIN = Path(__file__).resolve().parents[2] / "hermes-hq-push"
sys.path.insert(0, AGENT)
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="hq-frame-live-")
os.environ.setdefault("HERMES_DISABLE_LAZY_INSTALLS", "1")
CHROME = os.environ.get("CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
TASK = "hq-frame-live"
CODE = "424242"

VERIFY = b"""<!doctype html><title>verify</title><p>Enter the 6 digit code</p>
<input id=code name=code inputmode=numeric autocomplete=one-time-code maxlength=6>"""
SIGNIN = b"""<!doctype html><title>sign in</title><form><input id=email name=email type=email autocomplete=username>
<input id=pw name=password type=password autocomplete=current-password><input type=submit></form>"""
PASSWORD = "pw-not-real-7c1e"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _server(port: int):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            host = (self.headers.get("Host") or "").split(":")[0]
            if self.path.startswith("/verify"):
                body = VERIFY
            elif self.path.startswith("/signin"):
                body = SIGNIN
            elif self.path.startswith("/login"):    # a saved login whose sign-in is a same-site frame
                body = f'<!doctype html><title>shop</title><iframe src="http://identity.shop.localhost:{port}/signin"></iframe>'.encode()
            elif self.path.startswith("/same"):     # as DoorDash: identity.<site> inside www.<site>
                body = f'<!doctype html><title>shop</title><iframe title="Login/Signup" src="http://identity.shop.localhost:{port}/verify"></iframe>'.encode()
            elif self.path.startswith("/cross"):    # a sign-in from another site: Chrome gives it its own process
                body = f'<!doctype html><title>shop</title><iframe title="Login" src="http://127.0.0.1:{port}/verify"></iframe>'.encode()
            else:
                body = f"<!doctype html><title>{host}</title>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _chrome():
    port, profile = _free_port(), tempfile.mkdtemp(prefix="hq-frame-chrome-")
    proc = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
                             "--no-first-run", "--no-default-browser-check", "--site-per-process", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as r:
                return proc, profile, port, json.load(r)["webSocketDebuggerUrl"]
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("Chrome did not start")


def _open(debug_port: int, url: str) -> None:
    req = urllib.request.Request(f"http://127.0.0.1:{debug_port}/json/new?{url}", method="PUT")
    urllib.request.urlopen(req, timeout=5).read()


def _code_in_frame(cap, frame_origin: str, field: str = "code") -> str:
    """What the frame's code box (or another field) holds, read on its own over CDP (not the code under test)."""
    targets = cap.call("Target.getTargets")["result"]["targetInfos"]
    for t in targets:
        if t["type"] not in ("page", "iframe") or not t["url"].startswith("http"):
            continue
        sid = cap.call("Target.attachToTarget", {"targetId": t["targetId"], "flatten": True})["result"]["sessionId"]
        try:
            tree = cap.call("Page.getFrameTree", session_id=sid)["result"]["frameTree"]
            stack = [tree]
            while stack:
                node = stack.pop()
                stack.extend(node.get("childFrames") or [])
                frame = node["frame"]
                if frame.get("securityOrigin") != frame_origin:
                    continue
                try:
                    ctx = cap.call("Page.createIsolatedWorld", {"frameId": frame["id"], "worldName": "check"},
                                   session_id=sid)["result"]["executionContextId"]
                except RuntimeError:
                    continue   # the frame lives in another process: read it from its own target
                got = cap.call("Runtime.evaluate", {"expression": f"document.getElementById('{field}').value",
                                                    "contextId": ctx, "returnByValue": True}, session_id=sid)
                return got["result"]["result"].get("value") or ""
        finally:
            cap.call("Target.detachFromTarget", {"sessionId": sid})
    return "<frame not found>"


def _reset(cap) -> None:
    for t in cap.call("Target.getTargets")["result"]["targetInfos"]:
        if t["type"] == "page" and t["url"].startswith("http"):
            cap.call("Target.closeTarget", {"targetId": t["targetId"]})
    time.sleep(0.3)


def main() -> int:
    logins_path = Path(sys.argv[1] if len(sys.argv) > 1 else PLUGIN / "gateway_logins.py").resolve()
    spec = importlib.util.spec_from_file_location("gateway_logins_live", logins_path)
    logins = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(logins)

    from agent.vault_backends import unlock
    from tools import browser_vault_tool as tool
    from tools.browser_supervisor import SUPERVISOR_REGISTRY

    asked: list[str] = []
    unlock.set_unlock_prompt_callback(lambda *a: None)          # someone can answer here (can_prompt_here)
    unlock.set_code_prompt_callback(lambda site, hint: asked.append(site) or CODE)
    print("named tab:", logins.install_named_tab(), "| frames:", getattr(logins, "install_frames", lambda: "absent")())

    port = _free_port()
    srv = _server(port)
    chrome, profile, debug_port, ws = _chrome()
    shop = f"http://www.shop.localhost:{port}"
    same_frame, cross_frame = f"http://identity.shop.localhost:{port}", f"http://127.0.0.1:{port}"
    failures = 0
    try:
        sup = SUPERVISOR_REGISTRY.get_or_start(task_id=TASK, cdp_url=ws)
        cases = [  # (page, the site the bot names, the frame the code belongs in)
            ("/same", same_frame, same_frame, "same-site frame, bot names the frame (DoorDash, 7:45:19)"),
            ("/same", shop, same_frame, "same-site frame, bot names the page (DoorDash, 7:46:07)"),
            ("/cross", cross_frame, cross_frame, "cross-site frame, bot names the frame"),
            # Another site's code box is never the named site's: nothing is asked, nothing is typed.
            ("/cross", shop, None, "another site's frame, bot names the page: left alone"),
        ]
        for path, named, frame, label in cases:
            _open(debug_port, "about:blank")
            _open(debug_port, shop + path)
            time.sleep(1.5)
            cap = SUPERVISOR_REGISTRY.capture(TASK)
            asked.clear()
            getattr(logins, "_code_asked", {}).clear()   # 0.9.59's look-in-the-mail nudge, where it exists
            args = {"action": "enter_code", "origin": named}
            logins.remember_site("browser_vault", args, TASK)
            try:
                raw = tool.browser_vault_enter_code(task_id=TASK)
                result = json.loads(raw)
            except Exception as exc:  # the plugin's NamedSiteNotOpen surfaces as the tool's error
                result = {"success": False, "error": f"{type(exc).__name__}: {exc}"}
            if hasattr(logins, "frame_done"):
                logins.frame_done(TASK)
            if frame is None:
                typed = _code_in_frame(cap, cross_frame)
                ok = not result.get("success") and typed == "" and asked == []
            else:
                typed = _code_in_frame(cap, frame)
                ok = bool(result.get("success")) and typed == CODE and asked == [frame.split("://", 1)[1]]
            failures += not ok
            shown = {k: result.get(k) for k in ("success", "error_type", "error", "origin") if result.get(k) is not None}
            print(f"{'PASS' if ok else 'FAIL'}  {label}: result={shown} asked={asked} frame holds={typed!r}")
            _reset(cap)
        # A saved login (filled by its handle, no name asked) whose sign-in is a same-site frame.
        from agent.vault_store import get_vault_store
        meta = get_vault_store().add_item("login", "Shop", {"identifier_type": "email", "identifier": "me@example.com",
                                                             "password": PASSWORD}, origin=same_frame)
        _open(debug_port, shop + "/login")
        time.sleep(1.5)
        cap = SUPERVISOR_REGISTRY.capture(TASK)
        try:
            result = json.loads(tool.browser_vault_fill(meta.id, task_id=TASK))
        except Exception as exc:
            result = {"success": False, "error": f"{type(exc).__name__}: {exc}"}
        if hasattr(logins, "frame_done"):
            logins.frame_done(TASK)
        typed = _code_in_frame(cap, same_frame, "pw")
        ok = bool(result.get("success")) and typed == PASSWORD and PASSWORD not in json.dumps(result)
        failures += not ok
        shown = {k: result.get(k) for k in ("success", "error_type", "error", "filled_fields") if result.get(k) is not None}
        print(f"{'PASS' if ok else 'FAIL'}  saved login, sign-in in a same-site frame: result={shown} "
              f"password in frame={'yes' if typed == PASSWORD else repr(typed)}")
        _reset(cap)
        # The page's own tab must be the vault's page again afterwards (nothing left pointing into a frame).
        _open(debug_port, shop + "/plain")
        time.sleep(1.0)
        sup.focus_page(shop)
        href = sup.evaluate_runtime("location.href").get("result")
        ok = href == shop + "/plain"
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  afterwards the vault reads the page again: {href}")
    finally:
        SUPERVISOR_REGISTRY.stop(TASK) if hasattr(SUPERVISOR_REGISTRY, "stop") else None
        chrome.terminate()
        chrome.wait(timeout=10)
        srv.shutdown()
        shutil.rmtree(profile, ignore_errors=True)
        shutil.rmtree(os.environ["HERMES_HOME"], ignore_errors=True)
    print("ALL PASS" if not failures else f"{failures} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
