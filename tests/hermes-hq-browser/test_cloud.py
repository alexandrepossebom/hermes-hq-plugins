"""Browser Use Cloud for a bot (cloud.py, and what fleet.py, choice.py, agent.py and cdp.py do about it). Hermes and
Browser Use are faked.

uv run --no-project --with fastapi --with httpx --with websockets --with uvicorn --with psutil --with pyyaml \
    python -m unittest -q test_cloud
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import test_browser as tb  # noqa: E402
from test_fleet import PORTS, Base, config  # noqa: E402

KEY = "bu_" + "k" * 40
SOCKET = "wss://fa7e6a94-8924-4de8-8b70-986aa28dded2.cdp.browser-use.com/devtools/browser/0b1c2d3e-aaaa-bbbb-cccc-0123456789ab"


class CloudChoiceTests(Base):
    def setUp(self):
        super().setUp()
        self.choice = tb.load("choice")

    def test_a_cloud_bot_gets_no_chrome_of_its_own(self):
        listed = [{"profile": "sam", "home": str(self.root / "sam"),
                   "config": {"browser": {"cloud_provider": "hq-browser-use", "cdp_url": ""}}}]
        state = {"sam": {"url": f"http://127.0.0.1:{PORTS}", "port": PORTS}}  # the Chrome it had: not "released"
        self.assertEqual(self.fleet.plan(listed, state, {}, lambda port: False), {"sam": {"skip": "cloud"}})

    def test_choosing_cloud_changes_only_that_bots_settings_and_its_own_browser_comes_back(self):
        url = f"http://127.0.0.1:{PORTS + 3}"
        home = self.bot("sam", {"model": {"default": "x"}, "browser": {"cdp_url": url, "use_real_profile": False}})
        self.fleet.write_state(self.root, "sam", {"profile": "sam", "port": PORTS + 3, "url": url,
                                                  "folder": str(home / "chrome-debug"), "status": "ready"})
        result = self.choice.set_cloud(self.fleet, self.root, "sam", home)
        self.assertEqual((result["mode"], result["blocked"], result["can_take_over"]), ("cloud", "cloud", True))
        saved = config(home)
        self.assertEqual(saved["browser"]["cloud_provider"], "hq-browser-use")
        self.assertEqual(saved["browser"]["cdp_url"], "")
        self.assertEqual(saved["model"], {"default": "x"})
        self.assertTrue(list((self.root / "plugin-backups" / "hermes-hq-browser-takeover").glob("sam-config-*.yaml")))
        self.assertEqual(self.choice.set_cloud(self.fleet, self.root, "sam", home)["mode"], "cloud")  # twice is fine

        back = self.choice.take_over(self.fleet, self.root, "sam", home, wait=0)
        self.assertNotEqual(back["mode"], "cloud")
        saved = config(home)
        self.assertEqual(saved["browser"]["cdp_url"], url)  # the address, and so the folder with its sign-ins
        self.assertEqual(saved["browser"]["cloud_provider"], "local")

    def test_a_bot_with_the_add_on_off_cannot_go_to_the_cloud(self):
        home = self.bot("sam", {"plugins": {"disabled": ["hermes-hq-browser"]}})
        with self.assertRaises(ValueError):
            self.choice.set_cloud(self.fleet, self.root, "sam", home)
        self.assertNotIn("browser", config(home))


class CloudKeyTests(Base):
    def setUp(self):
        super().setUp()
        self.cloud = tb.load("cloud")
        self.calls = []

        def call(method, path, body=None, *, key, timeout=30.0):
            self.calls.append((method, path, body))
            if key != KEY:
                raise self.cloud.CloudError("Browser Use didn’t accept the API key.")
            return {"id": "0a1f5017-9d24-4d81-b6d3-be599cd889f4"} if method == "POST" else {}
        self.cloud.call = call
        os.environ.pop("BROWSER_USE_API_KEY", None)

    def test_a_key_is_saved_only_when_browser_use_accepts_it_and_is_never_shown(self):
        self.assertFalse(self.cloud.status(self.root)["key_set"])
        with self.assertRaises(self.cloud.CloudError):
            self.cloud.save_key("bu_" + "x" * 40, self.root)
        self.assertFalse(self.cloud.status(self.root)["key_set"])
        shown = self.cloud.save_key(f"  {KEY} ", self.root)
        self.assertEqual((shown["key_set"], shown["key_hint"]), (True, "…kkkk"))
        self.assertNotIn(KEY, str(shown))
        path = self.cloud.store_path(self.root)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.cloud.save_key("", self.root)["key_set"])

    def test_a_bot_keeps_one_profile_and_another_account_starts_fresh(self):
        self.cloud.save_key(KEY, self.root)
        first = self.cloud.profile_id("sam", key=KEY, root=self.root)
        self.assertEqual(first, self.cloud.profile_id("sam", key=KEY, root=self.root))
        self.assertEqual(sum(1 for c in self.calls if c[:2] == ("POST", "/profiles")), 1)
        other = "bu_" + "z" * 40
        self.cloud.call = lambda method, path, body=None, *, key, timeout=30.0: {}
        self.cloud.save_key(other, self.root)
        self.assertEqual(self.cloud.read_store(self.root).get("profiles"), {})


class CloudBrowserTests(Base):
    def test_only_a_browser_use_socket_or_a_local_browser_is_ever_connected_to(self):
        cdp = tb.load("cdp")
        self.assertTrue(cdp.usable(SOCKET) and cdp.cloud(SOCKET))
        self.assertTrue(cdp.usable("ws://127.0.0.1:9222/devtools/browser/abc") and not cdp.cloud("ws://127.0.0.1:9222/devtools/browser/abc"))
        for other in ("wss://evil.example.com/devtools/browser/abc", SOCKET.replace("browser-use.com", "browser-use.com.evil.io"),
                      SOCKET.replace("wss://", "ws://"), "wss://cdp.browser-use.com/devtools/browser/abc", "", None):
            self.assertFalse(cdp.usable(other), other)

    def test_a_cloud_browser_is_a_browser_the_phone_can_be_shown(self):
        agent = tb.load("agent")
        self.assertEqual(agent.browser_of({"bb_session_id": "x", "session_name": "hq_1", "cdp_url": SOCKET}), ("cloud", SOCKET))
        self.assertIsNone(agent.browser_of({"bb_session_id": "x", "session_name": "bb_1", "cdp_url": "wss://connect.browserbase.com/x"}))
        self.assertEqual(agent.endpoint(("cloud", SOCKET)), SOCKET)
        self.assertTrue(agent._reachable(SOCKET))
