"""fleet.py: the plugin in every bot and a Chrome per bot. Hermes is faked; Chrome is real when present.

uv run --no-project --with fastapi --with httpx --with websockets --with uvicorn --with psutil --with pyyaml \
    python -m unittest -q test_fleet
"""
from __future__ import annotations

import os
import shutil
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import test_browser as tb  # noqa: E402

import yaml  # noqa: E402

PORTS = 19400  # well clear of real bot Chromes (9222 and up)


def config(home: Path) -> dict:
    return yaml.safe_load((home / "config.yaml").read_text()) or {}


def write(home: Path, value: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(yaml.safe_dump(value))


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        tb.FakeHermes(self.root)
        self.fleet = tb.load("fleet")
        self.fleet.FIRST_PORT, self.fleet.LAST_PORT = PORTS, PORTS + 40
        self.source = self.root / "plugins" / "hermes-hq-browser"
        self.source.mkdir(parents=True)
        (self.source / "plugin.yaml").write_text('name: hermes-hq-browser\nversion: "3.3.0"\n')
        (self.source / "agent.py").write_text("# new\n")
        (self.source / "tests").mkdir()
        write(self.root, {"plugins": {"enabled": ["hermes-hq-browser"]}})

    def tearDown(self):
        self.dir.cleanup()

    def bot(self, name: str, value: dict | None = None) -> Path:
        home = self.root / "profiles" / name
        write(home, value if value is not None else {})
        return home


class SpreadTests(Base):
    def test_a_new_bot_gets_the_plugin_enabled_without_switching_off_its_other_plugins(self):
        home = self.bot("jim")
        (home / "plugins" / "other").mkdir(parents=True)
        (home / "plugins" / "other" / "plugin.yaml").write_text("name: other\n")
        activated = []
        changed = self.fleet.spread(self.root, self.source, activated.append)
        self.assertEqual(changed, ["jim"])
        self.assertEqual(activated, ["jim"])
        self.assertEqual((home / "plugins" / "hermes-hq-browser" / "agent.py").read_text(), "# new\n")
        self.assertFalse((home / "plugins" / "hermes-hq-browser" / "tests").exists())
        self.assertEqual(sorted(config(home)["plugins"]["enabled"]), ["hermes-hq-browser", "other"])
        self.assertEqual(self.fleet.spread(self.root, self.source, activated.append), [])  # nothing left to do

    def test_an_older_copy_is_updated_a_newer_one_kept_and_a_disabled_bot_left_alone(self):
        older, newer, off = self.bot("older"), self.bot("newer"), self.bot("off", {"plugins": {"disabled": ["hermes-hq-browser"]}})
        for home, version in ((older, "3.2.1"), (newer, "9.0.0"), (off, "1.0.0")):
            (home / "plugins" / "hermes-hq-browser").mkdir(parents=True)
            (home / "plugins" / "hermes-hq-browser" / "plugin.yaml").write_text(f'version: "{version}"\n')
            (home / "plugins" / "hermes-hq-browser" / "agent.py").write_text("# old\n")
            if home is not off:
                write(home, {"plugins": {"enabled": ["hermes-hq-browser"]}})
        activated = []
        self.assertEqual(self.fleet.spread(self.root, self.source, activated.append), ["older"])
        self.assertEqual(activated, [])  # already enabled: the update loads when its process next starts
        self.assertEqual((older / "plugins" / "hermes-hq-browser" / "agent.py").read_text(), "# new\n")
        self.assertEqual((newer / "plugins" / "hermes-hq-browser" / "agent.py").read_text(), "# old\n")
        self.assertEqual((off / "plugins" / "hermes-hq-browser" / "agent.py").read_text(), "# old\n")
        self.assertNotIn("enabled", config(off)["plugins"])


class RenameTests(Base):
    """The plugin was dispatch-browser before the app became Hermes HQ: the old name is retired, its state kept."""

    def test_a_bot_running_the_old_plugin_switches_to_the_new_one_and_keeps_its_settings(self):
        home = self.bot("jim", {"plugins": {"enabled": ["dispatch-browser", "other"],
                                            "entries": {"dispatch-browser": {"granted_capabilities": ["x"], "note": 1}}}})
        (home / "plugins" / "dispatch-browser").mkdir(parents=True)
        (home / "plugins" / "dispatch-browser" / "plugin.yaml").write_text('version: "3.3.1"\n')
        activated = []
        self.assertEqual(self.fleet.spread(self.root, self.source, activated.append), ["jim"])
        plugins = config(home)["plugins"]
        self.assertEqual(sorted(plugins["enabled"]), ["hermes-hq-browser", "other"])
        self.assertEqual(plugins["disabled"], ["dispatch-browser"])  # an old dashboard can't copy it back
        self.assertEqual(plugins["entries"]["hermes-hq-browser"], {"note": 1})
        self.assertEqual(plugins["entries"]["dispatch-browser"]["note"], 1)  # the old entry is left as it was
        self.assertTrue((home / "plugins" / "dispatch-browser" / "plugin.yaml").exists())  # nothing deleted
        self.assertTrue((home / "plugins" / "hermes-hq-browser" / "agent.py").exists())
        self.assertEqual(activated, ["jim"])
        self.assertEqual(self.fleet.spread(self.root, self.source, activated.append), [])  # once only
        self.assertEqual(self.plan_of(home), "manage")

    def test_a_bot_that_had_switched_the_old_plugin_off_keeps_the_new_one_off(self):
        home = self.bot("off", {"plugins": {"disabled": ["dispatch-browser"]}})
        self.assertEqual(self.fleet.spread(self.root, self.source), ["off"])
        plugins = config(home)["plugins"]
        self.assertEqual(sorted(plugins["disabled"]), ["dispatch-browser", "hermes-hq-browser"])
        self.assertNotIn("hermes-hq-browser", plugins.get("enabled") or [])
        self.assertFalse((home / "plugins" / "hermes-hq-browser").exists())
        self.assertEqual(self.plan_of(home), "off")

    def plan_of(self, home: Path) -> str:
        bots = [{"profile": home.name, "home": str(home), "config": config(home)}]
        decision = self.fleet.plan(bots, {}, {}, lambda port: False)[home.name]
        return decision.get("skip") or "manage"

    def test_the_old_state_folder_is_copied_once_and_left_in_place(self):
        old = self.root / "dispatch-browser"
        (old / "chromes").mkdir(parents=True)
        (old / "browsers").mkdir()
        (old / "chromes" / "jim.json").write_text('{"port": 9230, "url": "http://127.0.0.1:9230", "status": "released"}')
        (old / "browsers" / "0123456789abcdef01234567.json").write_text("{}")
        self.assertEqual(self.fleet.read_state(self.root, "jim")["status"], "released")  # its opt-out survives
        new = self.root / "hermes-hq-browser"
        self.assertTrue((new / "chromes" / "jim.json").exists())
        self.assertFalse((new / "browsers").exists())  # live records from running processes aren't carried over
        self.assertTrue((old / "chromes" / "jim.json").exists())
        (old / "chromes" / "sam.json").write_text("{}")
        self.fleet.state_folder(self.root)
        self.assertFalse((new / "chromes" / "sam.json").exists())  # copied once, not kept in sync

    def test_settings_read_the_new_environment_name_then_the_old_one(self):
        saved = {k: os.environ.pop(k, None) for k in ("HERMES_HQ_BROWSER_CHROME", "DISPATCH_BROWSER_CHROME")}
        try:
            self.assertEqual(self.fleet.setting("CHROME", "none"), "none")
            os.environ["DISPATCH_BROWSER_CHROME"] = "/old"
            self.assertEqual(self.fleet.setting("CHROME"), "/old")
            os.environ["HERMES_HQ_BROWSER_CHROME"] = "/new"
            self.assertEqual(self.fleet.setting("CHROME"), "/new")
        finally:
            for key, value in saved.items():
                os.environ.pop(key, None)
                if value is not None:
                    os.environ[key] = value


class PlanTests(Base):
    def plan(self, bots, states=None, owners=None, in_use=lambda port: False):
        listed = [{"profile": name, "home": str(self.root / name), "config": value} for name, value in bots]
        return self.fleet.plan(listed, states or {}, owners or {}, in_use)

    def test_each_bot_gets_its_own_port_and_a_bot_already_connected_keeps_its_chrome(self):
        decisions = self.plan([
            ("default", {}),
            ("jim", {"browser": {"cdp_url": f"http://127.0.0.1:{PORTS}"}}),
            ("sam", {"browser": {"cloud_provider": "browser-use"}}),
        ], in_use=lambda port: port == PORTS + 1)
        self.assertEqual(decisions["jim"], {"manage": PORTS, "url": f"http://127.0.0.1:{PORTS}", "write": False})
        self.assertEqual(decisions["default"], {"manage": PORTS + 2, "url": f"http://127.0.0.1:{PORTS + 2}", "write": True})
        self.assertEqual(decisions["sam"]["manage"], PORTS + 3)

    def test_a_cloned_bot_gets_its_own_chrome_and_the_bot_whose_chrome_it_is_keeps_it(self):
        url = {"browser": {"cdp_url": f"http://127.0.0.1:{PORTS + 5}"}}
        owners = {PORTS + 5: str(self.root / "jim" / "chrome-debug")}
        decisions = self.plan([("aaa-copy", url), ("jim", url)], owners=owners)
        self.assertEqual(decisions["jim"]["manage"], PORTS + 5)
        self.assertEqual(decisions["aaa-copy"], {"manage": PORTS, "url": f"http://127.0.0.1:{PORTS}", "write": True})
        # Without a running Chrome, the state file says whose port it is.
        decisions = self.plan([("aaa-copy", url), ("jim", url)], states={"jim": {"port": PORTS + 5}})
        self.assertEqual(decisions["jim"]["manage"], PORTS + 5)

    def test_bots_set_up_another_way_are_left_alone_and_their_ports_never_reused(self):
        decisions = self.plan([
            ("real", {"browser": {"use_real_profile": True, "cdp_url": f"http://127.0.0.1:{PORTS}"}}),
            ("remote", {"browser": {"cdp_url": "https://cloud.example/cdp"}}),
            ("fixed", {"browser": {"cdp_url": f"ws://127.0.0.1:{PORTS + 1}/devtools/browser/abc"}}),
            ("fox", {"browser": {"cloud_provider": "camofox"}}),
            ("off", {"plugins": {"disabled": ["hermes-hq-browser"]}}),
            ("unlisted", {"plugins": {"enabled": ["other"]}}),
            ("removed", {}),
            ("new", {}),
        ], states={"removed": {"url": f"http://127.0.0.1:{PORTS + 2}", "port": PORTS + 2}})
        self.assertEqual({k: v.get("skip") for k, v in decisions.items() if "skip" in v}, {
            "real": "real_profile", "remote": "remote", "fixed": "fixed", "fox": "camofox", "off": "off",
            "unlisted": "off", "removed": "released"})
        self.assertEqual(decisions["new"]["manage"], PORTS + 3)  # not the real-profile bot's port, nor the released one's


@unittest.skipUnless(tb.CHROME, "needs Chrome or Chromium")
class RefreshTests(Base):
    """mac.md F6 (Plaid round): ten bot Chromes restarted in one 35 s burst every hour inside the gateway's process,
    and the main bot's Chrome was restarted ten minutes after it last browsed, mid-session."""

    def setUp(self):
        super().setUp()
        self.fleet.installed_version = lambda: None  # no newer Chrome on disk
        self.supervisor = self.fleet.Supervisor(self.root, self.source, activate=lambda profile: None)
        self.idle = {}
        self.supervisor.idle_for = lambda profile: self.idle[profile]
        self.now = time.time()

    def stale(self, profile, age, later=0.0):
        self.fleet.REFRESH_AGE = 3600.0
        import psutil
        born = psutil.Process(os.getpid()).create_time()
        return self.supervisor.stale(profile, os.getpid(), 1, born + age + later)

    def test_refreshes_are_spread_out_and_wait_for_a_long_quiet(self):
        self.idle.update(a=1300.0, b=1300.0, c=700.0)
        self.assertFalse(self.stale("c", 7200))  # used 12 minutes ago: a bot in a long turn may come back to it
        self.assertTrue(self.stale("a", 7200))
        self.assertFalse(self.stale("b", 7200, later=60))  # not in the same burst
        self.assertTrue(self.stale("b", 7200, later=self.fleet.REFRESH_SPACING + 1))

    def test_a_chrome_nobody_used_waits_hours_not_one(self):
        self.idle.update(a=float("inf"))
        self.assertFalse(self.stale("a", 3 * 3600))
        self.assertTrue(self.stale("a", self.fleet.REFRESH_UNUSED + 60))

    def test_a_young_chrome_is_left_alone(self):
        self.idle.update(a=5000.0)
        self.assertFalse(self.stale("a", 600))


class SupervisorTests(Base):
    def setUp(self):
        super().setUp()
        os.environ["HERMES_HQ_BROWSER_CHROME"] = tb.CHROME
        self.fleet.START_GRACE, self.fleet.MISSES, self.fleet.PROBE_TIMEOUT = 0.0, 2, 1.0
        self.supervisor = self.fleet.Supervisor(self.root, self.source, activate=lambda profile: None)

    def tearDown(self):
        for pid, folder, _ in self.fleet.chrome_processes():
            if not folder.startswith(str(self.root)):
                continue
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)  # Chrome's children keep the profile directory open
            except OSError:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        deadline = time.time() + 2.0
        while time.time() < deadline and any(folder.startswith(str(self.root))
                                              for _, folder, _ in self.fleet.chrome_processes()):
            time.sleep(0.05)
        os.environ.pop("HERMES_HQ_BROWSER_CHROME", None)
        for _ in range(20):  # the directory stays busy for a moment after the process is gone
            try:
                super().tearDown()
                return
            except OSError:
                time.sleep(0.1)
        super().tearDown()

    def until(self, check, seconds=20.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if check():
                return True
            time.sleep(0.2)
        return False

    def mine(self, home):
        return [pid for pid, folder, _ in self.fleet.chrome_processes() if folder == str(home / "chrome-debug")]

    def test_every_bot_browses_in_a_chrome_of_its_own_which_comes_back_when_it_dies_or_freezes(self):
        jim = self.bot("jim", {"plugins": {"enabled": ["hermes-hq-browser"]}, "browser": {"cloud_provider": "browser-use"}})
        clone = self.bot("jim-copy", {"plugins": {"enabled": ["hermes-hq-browser"]}, "browser": {"cdp_url": ""}})
        self.supervisor.pass_once()
        urls = {name: config(home)["browser"]["cdp_url"] for name, home in (("default", self.root), ("jim", jim), ("jim-copy", clone))}
        self.assertEqual(len(set(urls.values())), 3, urls)
        ports = {name: self.fleet.port_of(url) for name, url in urls.items()}
        self.assertTrue(self.until(lambda: all(self.fleet.answers(p, 1.0) for p in ports.values())), ports)
        statuses = self.supervisor.pass_once()
        self.assertEqual({s["status"] for s in statuses.values()}, {"ready"})
        self.assertFalse((jim / "chrome-debug" / "Default" / "Preferences").read_text().count('"credentials_enable_service": true'))

        # A copy made later (config and all) gets its own Chrome; jim keeps its port.
        twin = self.bot("aaa-twin", {"plugins": {"enabled": ["hermes-hq-browser"]}, "browser": {"cdp_url": urls["jim"]}})
        self.supervisor.pass_once()
        self.assertEqual(config(jim)["browser"]["cdp_url"], urls["jim"])
        self.assertNotIn(config(twin)["browser"]["cdp_url"], urls.values())

        # Exits: started again on the next pass, same port, same profile folder.
        [pid] = self.mine(jim)
        os.kill(pid, signal.SIGKILL)
        self.until(lambda: not self.fleet.answers(ports["jim"], 0.5), 5)
        self.supervisor.pass_once()
        self.assertTrue(self.until(lambda: self.fleet.answers(ports["jim"], 1.0)))
        self.assertNotEqual(self.mine(jim), [pid])

        # Freezes: after MISSES unanswered probes it is ended and started again.
        [frozen] = self.mine(jim)
        os.kill(frozen, signal.SIGSTOP)
        self.assertEqual(self.supervisor.pass_once()["jim"]["status"], "not_responding")
        self.supervisor.pass_once()
        self.assertTrue(self.until(lambda: self.fleet.answers(ports["jim"], 1.0)))
        self.assertNotIn(frozen, self.mine(jim))

        # A bot that is deleted: its Chrome ends.
        [gone] = self.mine(clone)
        (clone / "config.yaml").unlink()  # no longer a bot (its Chrome may still be writing to its folder)
        self.supervisor.pass_once()
        self.assertTrue(self.until(lambda: gone not in [p for p, _, _ in self.fleet.chrome_processes()], 10))
        shutil.rmtree(clone, ignore_errors=True)

        # The person removes a bot's cdp_url: it is theirs again, and its Chrome stops.
        [before] = self.mine(jim)
        value = config(jim)
        value["browser"].pop("cdp_url")
        write(jim, value)
        self.assertEqual(self.supervisor.pass_once()["jim"]["status"], "released")
        self.assertTrue(self.until(lambda: before not in [p for p, _, _ in self.fleet.chrome_processes()], 10))
        self.supervisor.pass_once()
        self.assertNotIn("cdp_url", config(jim)["browser"])

    def test_a_bot_that_browses_first_starts_its_own_chrome(self):
        jim = self.bot("jim", {"plugins": {"enabled": ["hermes-hq-browser"]}})
        self.supervisor.pass_once()
        url = config(jim)["browser"]["cdp_url"]
        port = self.fleet.port_of(url)
        self.assertTrue(self.until(lambda: self.fleet.answers(port, 1.0)))
        for pid in self.mine(jim):
            os.kill(pid, signal.SIGKILL)
        self.until(lambda: not self.fleet.answers(port, 0.5), 5)
        self.assertTrue(self.fleet.ensure("jim", jim, url))
        self.assertFalse(self.fleet.ensure("jim", jim, "http://127.0.0.1:1"))  # not a Chrome the plugin runs

    def test_an_old_chrome_is_started_afresh_only_while_nobody_uses_it(self):
        jim = self.bot("jim", {"plugins": {"enabled": ["dispatch-browser"]}})
        self.supervisor.pass_once()
        port = self.fleet.port_of(config(jim)["browser"]["cdp_url"])
        self.assertTrue(self.until(lambda: self.fleet.answers(port, 1.0)))
        self.supervisor.pass_once()
        [first] = self.mine(jim)
        self.fleet.REFRESH_AGE = 0.0  # every Chrome counts as old
        self.fleet.REFRESH_SPACING = 0.0  # the default bot's Chrome goes first; RefreshTests covers the spacing
        self.supervisor.idle_for = lambda profile: 0.0  # in use
        self.supervisor.pass_once()
        self.assertEqual(self.mine(jim), [first])
        self.supervisor.idle_for = lambda profile: 3600.0  # unused for an hour
        self.supervisor.pass_once()
        self.assertTrue(self.until(lambda: self.fleet.answers(port, 1.0) and self.mine(jim) not in ([], [first])))

    def test_only_one_supervisor_per_machine(self):
        self.assertTrue(self.supervisor.begin.__self__ is self.supervisor)
        first = self.fleet.Locked(self.fleet.state_folder(self.root) / "supervisor.lock", wait=False).__enter__()
        try:
            self.assertFalse(self.fleet.Supervisor(self.root, self.source).begin())
        finally:
            first.__exit__()


class OnDemandTests(Base):
    """``HERMES_HQ_BROWSER_ON_DEMAND=1``: a bot's Chrome runs only while it is used. Chrome is faked here (launch, the
    port probe, the process list and ``end``), so these run anywhere; the default mode is checked the same way."""

    def setUp(self):
        super().setUp()
        self.saved = {k: os.environ.pop(k, None) for k in ("HERMES_HQ_BROWSER_ON_DEMAND", "HERMES_HQ_BROWSER_IDLE_MINUTES",
                                                           "DISPATCH_BROWSER_ON_DEMAND", "DISPATCH_BROWSER_IDLE_MINUTES")}
        self.running: dict[str, tuple[int, int]] = {}  # profile folder -> (pid, port)
        self.frozen: set[int] = set()                   # ports whose Chrome is up but answers nothing
        self.launches: list[int] = []                   # ports Chrome was started on
        self.pids = iter(range(2_000_000, 3_000_000))   # no real process has these
        fleet = self.fleet
        fleet.chrome_binary = lambda: "/fake/chrome"
        fleet.installed_version = lambda: None
        fleet.sample = lambda *args: None
        fleet.listening = lambda port: any(p == port for _, p in self.running.values())
        fleet.answers = lambda port, timeout=0: fleet.listening(port) and port not in self.frozen
        fleet.chrome_processes = lambda: [(pid, folder, port) for folder, (pid, port) in self.running.items()]

        def launch(home, port, binary, window="hidden", copied=False):
            fleet.seed(home / "chrome-debug")
            pid = next(self.pids)
            self.running[str(home / "chrome-debug")] = (pid, port)
            self.launches.append(port)
            return pid

        def end(pid, folder):
            self.running.pop(folder, None)
        fleet.launch, fleet.end = launch, end
        self.supervisor = fleet.Supervisor(self.root, self.source, activate=lambda profile: None)
        self.jim = self.bot("jim", {"plugins": {"enabled": ["hermes-hq-browser"]}})

    def tearDown(self):
        for key in ("HERMES_HQ_BROWSER_ON_DEMAND", "HERMES_HQ_BROWSER_IDLE_MINUTES"):
            os.environ.pop(key, None)
        for key, value in self.saved.items():
            if value is not None:
                os.environ[key] = value
        super().tearDown()

    def on(self, minutes=None):
        os.environ["HERMES_HQ_BROWSER_ON_DEMAND"] = "1"
        if minutes is not None:
            os.environ["HERMES_HQ_BROWSER_IDLE_MINUTES"] = str(minutes)

    def status(self, name="jim", **kw):
        return self.supervisor.pass_once(**kw)[name]["status"]

    def url(self, home=None):
        return config(home or self.jim)["browser"]["cdp_url"]

    def wake(self):
        """What a bot's browser call does (agent._own_chrome_up), then the supervisor's next look."""
        self.assertTrue(self.fleet.ensure("jim", self.jim, self.url()))
        self.assertEqual(self.status(), "ready")

    def folder(self):
        return str(self.jim / "chrome-debug")

    def test_by_default_every_bots_chrome_is_started_and_comes_back_as_today(self):
        self.supervisor.pass_once()
        self.assertEqual(len(self.running), 2)  # the main bot's and jim's
        self.assertEqual(self.status(), "ready")
        self.running.pop(self.folder())  # it exits
        self.supervisor.pass_once()
        self.assertIn(self.folder(), self.running)  # started again on the next pass
        self.assertEqual(len(self.launches), 3)
        self.supervisor.idle_for = lambda profile: 10 ** 9  # never stopped for being idle
        self.supervisor.pass_once()
        self.assertEqual(len(self.running), 2)

    def test_on_demand_gives_every_bot_its_port_and_profile_but_starts_no_chrome(self):
        self.on()
        statuses = self.supervisor.pass_once()
        self.assertEqual({s["status"] for s in statuses.values()}, {"sleeping"})
        self.assertEqual((self.running, self.launches), ({}, []))
        urls = {self.url(self.root), self.url()}
        self.assertEqual(len(urls), 2)  # each bot is pointed at a Chrome of its own
        state = self.fleet.read_state(self.root, "jim")
        self.assertEqual((state["status"], state["url"], state["folder"], state.get("pid")),
                         ("sleeping", self.url(), self.folder(), None))
        self.supervisor.pass_once()
        self.assertEqual(self.launches, [])  # later passes start nothing either

    def test_a_bots_browser_call_starts_its_chrome_and_only_its_own(self):
        self.on()
        self.supervisor.pass_once()
        self.wake()
        self.assertEqual(self.launches, [self.fleet.port_of(self.url())])
        statuses = self.supervisor.pass_once()
        self.assertEqual((statuses["jim"]["status"], statuses["default"]["status"]), ("ready", "sleeping"))

    def test_an_unused_chrome_is_stopped_and_its_profile_and_next_start_are_kept(self):
        self.on(minutes=15)
        self.supervisor.pass_once()
        self.wake()
        (self.jim / "chrome-debug" / "Default" / "Cookies").write_text("signed in")  # what the profile keeps
        self.supervisor.idle_for = lambda profile: 15 * 60 - 1
        self.assertEqual(self.status(), "ready")  # not yet
        self.supervisor.idle_for = lambda profile: 15 * 60
        self.assertEqual(self.status(), "sleeping")
        self.assertEqual(self.running, {})
        state = self.fleet.read_state(self.root, "jim")
        self.assertEqual((state["status"], state.get("pid"), state["url"]), ("sleeping", None, self.url()))
        self.assertEqual((self.jim / "chrome-debug" / "Default" / "Cookies").read_text(), "signed in")
        self.assertEqual(self.status(), "sleeping")  # and it stays asleep
        self.supervisor.idle_for = lambda profile: 0.0  # (the dashboard's clock starts over with the new Chrome)
        self.wake()  # the bot's next call
        self.assertEqual(len(self.launches), 2)
        self.assertEqual((self.jim / "chrome-debug" / "Default" / "Cookies").read_text(), "signed in")

    def test_a_chrome_in_use_is_never_stopped(self):
        self.on(minutes=1)
        self.supervisor.pass_once()
        self.wake()
        self.supervisor.idle_for = lambda profile: 0.0  # a call runs, a phone watches, a person holds it, or an ask waits
        for _ in range(3):
            self.assertEqual(self.status(), "ready")
        self.supervisor.idle_for = None  # no idle clock (a dashboard that doesn't run it): never stopped
        self.assertEqual(self.status(), "ready")

        def broken(profile):
            raise RuntimeError("torn read")
        self.supervisor.idle_for = broken
        self.assertEqual(self.status(), "ready")
        self.assertEqual(len(self.running), 1)

    def test_idle_minutes_is_read_from_the_environment_and_zero_never_stops_one(self):
        self.assertEqual(self.fleet.idle_limit(), 900.0)
        for value, seconds in (("2", 120.0), ("0.5", 30.0), ("0", float("inf")), ("0.0", float("inf")),
                                ("soon", 900.0), ("-3", 900.0), ("nan", 900.0), ("", 900.0)):
            os.environ["HERMES_HQ_BROWSER_IDLE_MINUTES"] = value
            self.assertEqual(self.fleet.idle_limit(), seconds, value)
        os.environ.pop("HERMES_HQ_BROWSER_IDLE_MINUTES")
        os.environ["DISPATCH_BROWSER_IDLE_MINUTES"] = "4"  # the older name still works
        self.assertEqual(self.fleet.idle_limit(), 240.0)
        os.environ.pop("DISPATCH_BROWSER_IDLE_MINUTES")
        self.assertFalse(self.fleet.on_demand())
        for value, wanted in (("1", True), ("true", True), ("on", True), ("yes", True),
                               ("0", False), ("", False), ("no", False)):
            os.environ["HERMES_HQ_BROWSER_ON_DEMAND"] = value
            self.assertIs(self.fleet.on_demand(), wanted, value)
        os.environ.pop("HERMES_HQ_BROWSER_ON_DEMAND")
        os.environ["DISPATCH_BROWSER_ON_DEMAND"] = "on"  # the older name still works
        self.assertTrue(self.fleet.on_demand())
        os.environ.pop("DISPATCH_BROWSER_ON_DEMAND")

    def test_a_chrome_that_exits_or_freezes_is_not_started_again_until_the_bot_browses(self):
        self.on()
        self.supervisor.pass_once()
        self.wake()
        self.running.pop(self.folder())  # exits
        self.assertEqual(self.status(), "sleeping")
        self.assertEqual(len(self.launches), 1)
        self.wake()
        self.assertEqual(len(self.launches), 2)
        self.fleet.START_GRACE, self.fleet.MISSES = 0.0, 2
        self.frozen.add(self.fleet.port_of(self.url()))  # freezes: ended after MISSES missed probes
        self.assertEqual(self.status(), "not_responding")
        self.assertEqual(self.status(), "sleeping")
        self.assertEqual(self.running, {})
        self.frozen.clear()
        self.wake()  # a fresh one for the bot's next call
        self.assertEqual(len(self.launches), 3)

    def test_a_chrome_a_bot_call_is_just_starting_is_not_marked_asleep(self):
        self.on()
        self.supervisor.pass_once()
        self.fleet.update_state(self.root, "jim", {"status": "starting", "launched_at": time.time()})
        self.assertEqual(self.status(), "starting")  # launched, but not in this pass's process list yet
        self.fleet.update_state(self.root, "jim", {"status": "starting", "launched_at": time.time() - 3600})
        self.assertEqual(self.status(), "sleeping")  # a start that never happened

    def test_an_old_chrome_nobody_uses_is_stopped_not_refreshed(self):
        self.on(minutes=0)  # never stopped for being idle: the hourly refresh would have restarted it
        self.supervisor.pass_once()
        self.wake()
        self.supervisor.stale = lambda *args: True
        self.assertEqual(self.status(), "sleeping")
        self.assertEqual((len(self.launches), self.running), (1, {}))

    def test_the_dashboard_counts_idle_time_from_the_start_in_on_demand_mode(self):
        api = tb.load("dashboard/plugin_api")
        self.supervisor.pass_once()
        self.fleet.update_state(self.root, "jim", {"launched_at": time.time() - 100})
        self.assertEqual(api._idle_for("jim"), float("inf"))  # as it always was
        self.on()
        self.assertTrue(99 < api._idle_for("jim") < 110)  # a Chrome nobody uses is still stopped; a new one has its time
        self.fleet.update_state(self.root, "jim", {"activity_at": time.time() - 5})
        self.assertTrue(4 < api._idle_for("jim") < 15)  # a browser call's stamp, before its busy flag is published
        api._watching["jim"] = 1
        try:
            self.assertEqual(api._idle_for("jim"), 0.0)  # a phone watches it
        finally:
            api._watching.pop("jim")

    def test_bots_lists_sleeping_browsers_and_says_which_mode_it_is(self):
        api = tb.load("dashboard/plugin_api")
        self.on()
        self.supervisor.pass_once()
        listing = api.bots()
        self.assertTrue(listing["on_demand"])
        self.assertEqual({row["profile"]: row["status"] for row in listing["bots"]}, {"default": "sleeping", "jim": "sleeping"})
        os.environ.pop("HERMES_HQ_BROWSER_ON_DEMAND")
        self.assertFalse(api.bots()["on_demand"])

    def test_an_idle_stop_looks_again_before_it_kills_the_chrome(self):
        self.on(minutes=1)
        self.supervisor.pass_once()
        self.wake()
        calls = {"n": 0}

        def idle_for(profile):
            calls["n"] += 1
            return 60.0 if calls["n"] == 1 else 0.0  # idle on the first look; a call, watch, hold or ask on the second

        self.supervisor.idle_for = idle_for
        self.assertEqual(self.status(), "ready")
        self.assertGreaterEqual(calls["n"], 2)
        self.assertIn(self.folder(), self.running)

    def test_a_short_idle_limit_does_not_end_a_chrome_a_call_just_claimed(self):
        # The dashboard has not published the call yet, and the idle limit is shorter than that gap.
        self.on(minutes=0.05)
        self.supervisor.pass_once()
        self.wake()
        self.fleet.update_state(self.root, "jim", {"launched_at": time.time() - 3600, "activity_at": time.time() - 10})
        self.supervisor.idle_for = lambda profile: 1000.0
        self.assertEqual(self.status(), "ready")
        self.assertIn(self.folder(), self.running)
        self.fleet.update_state(self.root, "jim", {"activity_at": time.time() - 3600})
        self.assertEqual(self.status(), "sleeping")  # once the claim window has passed, the short limit still stops it

    def test_an_idle_stop_does_not_kill_a_chrome_a_call_has_claimed(self):
        self.on(minutes=15)
        self.supervisor.pass_once()
        self.wake()
        self.fleet.update_state(self.root, "jim", {"launched_at": time.time() - 3600})
        self.supervisor.idle_for = lambda profile: 15 * 60  # the dashboard clock still says idle
        claimed = {"done": False}
        real_enter = self.fleet.Locked.__enter__

        def enter(lock):
            # The call's stamp lands before this stop takes the profile lock (note_call writes it under that lock).
            if not claimed["done"] and lock.path.name == "jim.lock":
                claimed["done"] = True
                state = self.fleet.read_state(self.root, "jim")
                self.fleet.write_state(self.root, "jim", {**state, "activity_at": time.time()})
            return real_enter(lock)

        self.fleet.Locked.__enter__ = enter
        try:
            self.assertEqual(self.status(), "ready")
        finally:
            self.fleet.Locked.__enter__ = real_enter
        self.assertTrue(claimed["done"])
        self.assertIn(self.folder(), self.running)
        self.assertEqual(self.fleet.read_state(self.root, "jim")["status"], "ready")

    def test_marking_a_bot_asleep_loses_to_a_start_that_landed_first(self):
        self.on()
        self.supervisor.pass_once()
        started = {"done": False}
        real_enter = self.fleet.Locked.__enter__

        def enter(lock):
            if not started["done"] and lock.path.name == "jim.lock":
                started["done"] = True
                state = self.fleet.read_state(self.root, "jim")
                self.fleet.write_state(self.root, "jim", {**state, "status": "starting", "launched_at": time.time(), "pid": 222})
            return real_enter(lock)

        self.fleet.Locked.__enter__ = enter
        try:
            self.assertEqual(self.status(), "starting")
        finally:
            self.fleet.Locked.__enter__ = real_enter
        self.assertEqual(self.fleet.read_state(self.root, "jim")["status"], "starting")
        self.assertEqual(self.launches, [])

    def test_a_chrome_that_is_starting_is_not_called_port_taken(self):
        self.on()
        self.supervisor.pass_once()
        port = self.fleet.port_of(self.url())
        self.fleet.update_state(self.root, "jim", {"status": "starting", "launched_at": time.time()})
        self.fleet.listening = lambda candidate: candidate == port  # up, but not in this pass's process list
        self.fleet.answers = lambda candidate, timeout=0: False
        self.assertEqual(self.status(), "starting")
        self.assertEqual(self.fleet.read_state(self.root, "jim")["status"], "starting")

    def test_a_failed_launch_stays_failed_and_is_not_started_again(self):
        self.on()
        self.supervisor.pass_once()
        self.fleet.chrome_binary = lambda: None
        self.assertFalse(self.fleet.ensure("jim", self.jim, self.url()))
        self.assertEqual(self.fleet.read_state(self.root, "jim")["status"], "no_chrome")
        self.assertEqual(self.status(), "no_chrome")
        self.assertEqual((self.launches, self.running), ([], {}))
        self.assertEqual(self.status(), "no_chrome")  # later passes don't bring it back on their own

    def test_a_chrome_that_never_answers_is_not_started_again_by_the_supervisor(self):
        self.on()
        self.supervisor.pass_once()
        self.fleet.answers = lambda port, timeout=0: False
        self.fleet.READY_WAIT = 0
        self.fleet.START_GRACE = 0
        self.fleet.MISSES = 1
        self.assertFalse(self.fleet.ensure("jim", self.jim, self.url()))
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(self.status(), "sleeping")
        self.assertEqual(self.running, {})
        self.assertEqual(len(self.launches), 1)  # on-demand does not keep restarting an unused Chrome
        self.assertEqual(self.status(), "sleeping")

    def test_zero_idle_minutes_does_not_stop_a_chrome_even_when_the_clock_says_never_used(self):
        self.on(minutes=0)
        self.supervisor.pass_once()
        self.wake()
        self.supervisor.idle_for = lambda profile: float("inf")
        self.assertEqual(self.status(), "ready")
        self.assertIn(self.folder(), self.running)

    def test_two_browser_calls_at_once_start_one_chrome(self):
        self.on()
        self.supervisor.pass_once()
        overlap = {"now": 0, "max": 0}
        gate = threading.Lock()
        real_launch = self.fleet.launch

        def launch(home, port, binary, window="hidden", copied=False):
            with gate:
                overlap["now"] += 1
                overlap["max"] = max(overlap["max"], overlap["now"])
            try:
                time.sleep(0.05)  # both calls are in start() together if the profile lock doesn't cover launch
                return real_launch(home, port, binary, window, copied)
            finally:
                with gate:
                    overlap["now"] -= 1

        self.fleet.launch = launch
        barrier = threading.Barrier(2)
        errors = []

        def call():
            try:
                barrier.wait(2)
                if not self.fleet.ensure("jim", self.jim, self.url()):
                    errors.append("ensure returned False")
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=call) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.launches, [self.fleet.port_of(self.url())])
        self.assertEqual(overlap["max"], 1)

    def test_a_call_a_hold_or_an_ask_keeps_the_idle_clock_at_zero(self):
        api = tb.load("dashboard/plugin_api")
        self.on()
        self.supervisor.pass_once()
        self.wake()
        self.fleet.update_state(self.root, "jim", {"launched_at": time.time() - 3600})
        ident = "ab" * 12
        now = time.time()
        record = {"profile": "jim", "status": "live", "pid": os.getpid(), "seen_at": now,
                  "busy": 0, "agent_at": now - 3600}
        browsers = api.spool.folder("browsers") / f"{ident}.json"
        api.spool.write(browsers, {**record, "busy": 1})
        self.assertEqual(api._idle_for("jim"), 0.0)  # a browser call is running
        api.spool.write(browsers, record)
        api.spool.write(api.spool.folder("control") / f"{ident}.json",
                        {"state": "requested", "epoch": 1, "expires_at": now + 60})
        self.assertEqual(api._idle_for("jim"), 0.0)  # a handoff is pending
        api.spool.write(api.spool.folder("control") / f"{ident}.json",
                        {"state": "controlled", "epoch": 1, "expires_at": now + 60})
        self.assertEqual(api._idle_for("jim"), 0.0)  # a person holds it
        api.spool.remove(api.spool.folder("control") / f"{ident}.json")
        api.spool.write(api.spool.folder("asks") / f"{ident}.json", {"expires_at": now + 60})
        self.assertEqual(api._idle_for("jim"), 0.0)  # browser_ask_user is waiting
        self.supervisor.idle_for = api._idle_for
        self.assertEqual(self.status(), "ready")
        self.assertIn(self.folder(), self.running)

    def test_handing_a_bot_back_to_the_plugin_does_not_wait_for_a_chrome_that_sleeps(self):
        choice = tb.load("choice")
        self.on()
        url = f"http://127.0.0.1:{PORTS + 7}"
        home = self.bot("sam", {"plugins": {"enabled": ["hermes-hq-browser"]}, "browser": {"use_real_profile": True, "cdp_url": url}})
        self.fleet.write_state(self.root, "sam", {"profile": "sam", "port": PORTS + 7, "url": url,
                                                  "folder": str(home / "chrome-debug"), "status": "ready"})
        self.assertEqual(choice.read_choice(self.fleet, self.root, "sam", home)["blocked"], "real_profile")
        threading.Timer(0.3, self.supervisor.pass_once).start()  # the supervisor's next pass
        started = time.monotonic()
        result = choice.take_over(self.fleet, self.root, "sam", home)
        self.assertLess(time.monotonic() - started, 8.0)  # not choice.TAKE_OVER_WAIT
        self.assertEqual((result["blocked"], result["status"]), (None, "sleeping"))
        self.assertEqual(self.launches, [])


def chrome_for_test() -> str | None:
    """Chrome itself. A shell wrapper that injects its own ``--user-data-dir`` (ahead of the one we pass) makes every
    bot share one profile, so process checks and sign-ins can't be tested through it."""
    path = tb.CHROME or ""
    try:
        head = Path(path).read_text(errors="ignore")[:800]
    except OSError:
        head = ""
    if head.startswith("#!") and "--user-data-dir=" in head:
        for candidate in ("/usr/bin/google-chrome-stable", "/usr/bin/chromium", "/usr/bin/chromium-browser"):
            if os.access(candidate, os.X_OK):
                return candidate
    return path or None


@unittest.skipUnless(chrome_for_test(), "needs Chrome or Chromium")
class OnDemandRealChromeTests(Base):
    """One real Chrome, in a temporary profile: launch on a call, stay up while watched, sleep, wake, keep the profile.

    The idle decision uses the dashboard clock with a short limit. Page restore when a restart is noticed is the
    tracker's job (TabTrackerTests); what survives here is the profile, including a cookie set over CDP."""

    def setUp(self):
        super().setUp()
        os.environ["HERMES_HQ_BROWSER_CHROME"] = chrome_for_test()
        os.environ["HERMES_HQ_BROWSER_ON_DEMAND"] = "1"
        os.environ["HERMES_HQ_BROWSER_IDLE_MINUTES"] = "0.05"  # 3 seconds, this test only
        self.fleet.START_GRACE = 5.0
        self.supervisor = self.fleet.Supervisor(self.root, self.source, activate=lambda profile: None)
        self.home = self.bot("jim", {"plugins": {"enabled": ["hermes-hq-browser"]}})

    def tearDown(self):
        for pid, folder, _ in self.fleet.chrome_processes():
            if folder.startswith(str(self.root)):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        time.sleep(0.3)  # Chrome 157 still has the profile directory open for a moment after SIGKILL
        for key in ("HERMES_HQ_BROWSER_CHROME", "HERMES_HQ_BROWSER_ON_DEMAND", "HERMES_HQ_BROWSER_IDLE_MINUTES"):
            os.environ.pop(key, None)
        super().tearDown()

    def mine(self, home: Path) -> list[int]:
        return [pid for pid, folder, _ in self.fleet.chrome_processes() if folder == str(home / "chrome-debug")]

    def until(self, check, seconds=12.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if check():
                return True
            time.sleep(0.2)
        return False

    def test_a_real_chrome_starts_from_sleep_navigates_idles_and_wakes_with_its_profile(self):
        import asyncio
        import json
        import urllib.request
        self.supervisor.pass_once()
        self.assertEqual(self.supervisor.statuses["jim"]["status"], "sleeping")
        self.assertEqual(self.supervisor.statuses["default"]["status"], "sleeping")
        self.assertEqual((self.mine(self.home), self.mine(self.root)), ([], []))
        url = config(self.home)["browser"]["cdp_url"]
        self.assertTrue(self.fleet.ensure("jim", self.home, url))
        self.assertTrue(self.mine(self.home))
        self.assertEqual(self.mine(self.root), [])  # only the bot that browsed
        self.assertEqual(self.supervisor.pass_once()["default"]["status"], "sleeping")

        cdp = tb.load("cdp")

        async def browse(endpoint: str, write: bool):
            browser = await cdp.Cdp().connect(endpoint)
            try:
                if write:
                    await browser.call("Target.createTarget", {"url": "data:text/html,hq-on-demand"})
                    # Expires, so it is stored in the profile. A session cookie would not survive the restart.
                    # Storage.* is on the browser target; Network.setCookie is not, on current Chrome.
                    await browser.call("Storage.setCookies", {"cookies": [{
                        "name": "hq", "value": "kept", "url": "https://example.test/",
                        "expires": time.time() + 86400}]})
                got = await browser.call("Storage.getCookies")
                return [cookie.get("value") for cookie in got.get("cookies") or [] if cookie.get("name") == "hq"]
            finally:
                await browser.close()

        endpoint = json.load(urllib.request.urlopen(f"{url}/json/version", timeout=5))["webSocketDebuggerUrl"]
        self.assertIn("kept", asyncio.run(browse(endpoint, True)))
        listing = json.load(urllib.request.urlopen(f"{url}/json/list", timeout=5))
        self.assertTrue(any("hq-on-demand" in str(target.get("url") or "") for target in listing))

        api = tb.load("dashboard/plugin_api")
        self.supervisor.idle_for = api._idle_for
        api._watching["jim"] = 1
        try:
            time.sleep(3.5)  # longer than the idle limit, and a phone is watching
            self.assertEqual(self.supervisor.pass_once()["jim"]["status"], "ready")
            self.assertTrue(self.mine(self.home))
        finally:
            api._watching.pop("jim", None)

        def asleep():
            status = self.supervisor.pass_once()["jim"]["status"]
            return status == "sleeping" and not self.mine(self.home)

        self.assertTrue(self.until(asleep), self.fleet.read_state(self.root, "jim"))
        self.assertTrue((self.home / "chrome-debug" / "Default").is_dir())
        cookie_files = list((self.home / "chrome-debug").rglob("Cookies*"))
        # Kept so a failure can show whether Chrome flushed the store before the process ended.
        self._cookie_files = [(str(path.relative_to(self.home)), path.stat().st_size) for path in cookie_files]
        # The profile directory itself, not only what Chrome may still be flushing.
        marker = self.home / "chrome-debug" / "Default" / "hq-marker.txt"
        marker.write_text("kept-profile")
        self.assertTrue(self.fleet.ensure("jim", self.home, url))  # the next browse starts it again
        self.assertEqual(marker.read_text(), "kept-profile")
        found = []
        deadline = time.time() + 8
        while time.time() < deadline:
            endpoint = json.load(urllib.request.urlopen(f"{url}/json/version", timeout=5))["webSocketDebuggerUrl"]
            found = asyncio.run(browse(endpoint, False))
            if "kept" in found:
                break
            time.sleep(0.4)
        self.assertIn("kept", found, self._cookie_files)
        self.assertEqual(self.mine(self.root), [])


@unittest.skipUnless(tb.CHROME, "needs Chrome or Chromium")
class TabTrackerTests(unittest.TestCase):
    """Watch Browser opens on the tab the bot used last, not on the newest tab."""

    def setUp(self):
        import subprocess
        self.dir = tempfile.TemporaryDirectory()
        self.port = PORTS + 50
        self.chrome = subprocess.Popen([tb.CHROME, "--headless=new", f"--remote-debugging-port={self.port}",
                                        f"--user-data-dir={self.dir.name}", "--no-first-run", "about:blank"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.url = f"http://127.0.0.1:{self.port}"
        tb.FakeHermes(Path(self.dir.name))
        self.cdp, self.tabs, self.stream = tb.load("cdp"), tb.load("tabs"), tb.load("stream")
        fleet = tb.load("fleet")
        deadline = time.time() + 20
        while not fleet.answers(self.port, 1.0) and time.time() < deadline:
            time.sleep(0.2)

    def tearDown(self):
        for process in (self.chrome,):
            if process.poll() is not None:
                continue
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except OSError:
                process.kill()
            try:
                process.wait(timeout=2)
            except Exception:  # noqa: BLE001
                pass
        for _ in range(20):  # Chrome's children keep the profile directory open for a moment
            try:
                self.dir.cleanup()
                return
            except OSError:
                time.sleep(0.1)
        self.dir.cleanup()

    def test_the_view_opens_on_the_tab_the_bot_moved_in_last(self):
        import asyncio
        import json
        import urllib.request
        tracker = self.tabs.Tracker(self.cdp)

        async def bot():
            endpoint = json.load(urllib.request.urlopen(f"{self.url}/json/version"))["webSocketDebuggerUrl"]
            browser = await self.cdp.Cdp().connect(endpoint)
            made = [(await browser.call("Target.createTarget", {"url": f"data:text/html,{name}"}))["targetId"] for name in "ABC"]
            await asyncio.sleep(1.0)
            tracker.watch(self.url)
            await asyncio.sleep(1.5)
            session = (await browser.call("Target.attachToTarget", {"targetId": made[1], "flatten": True}))["sessionId"]
            await browser.call("Page.navigate", {"url": "data:text/html,B-moved-on"}, session)  # no activation
            await asyncio.sleep(1.0)
            await browser.close()
            return made

        made = asyncio.run(bot())
        self.assertEqual(tracker.recent(self.url)[0], made[1])
        self.assertLess(time.time() - tracker.last_used(self.url), 5)
        # Tabs already open when tracking starts are not "used": a fresh tracker sees no use yet.
        fresh = self.tabs.Tracker(self.cdp)
        fresh.watch(self.url)
        time.sleep(1.5)
        self.assertEqual(fresh.last_used(self.url), 0.0)
        listing = json.load(urllib.request.urlopen(f"{self.url}/json/list"))
        viewer = self.stream.Viewer(None, self.cdp, {"id": "0" * 24, "url": None}, None, recent=lambda: tracker.recent(self.url))
        viewer.tabs = self.cdp.page_tabs([{**t, "targetId": t["id"]} for t in listing])
        self.assertEqual(viewer._first_tab(), made[1])
        viewer.recent = lambda: []
        self.assertNotEqual(viewer._first_tab(), made[1])  # without it, a guess: here the wrong tab

    def test_a_restarted_chrome_gets_the_bots_pages_back(self):
        import asyncio
        import json
        import subprocess
        import urllib.request
        self.tabs.RETRY = 0.5
        tracker = self.tabs.Tracker(self.cdp)

        async def open_pages():
            endpoint = json.load(urllib.request.urlopen(f"{self.url}/json/version"))["webSocketDebuggerUrl"]
            browser = await self.cdp.Cdp().connect(endpoint)
            for name in ("first", "second"):
                await browser.call("Target.createTarget", {"url": f"data:text/html,{name}"})
                await asyncio.sleep(0.5)
            await browser.close()

        tracker.watch(self.url)
        time.sleep(1.0)
        asyncio.run(open_pages())
        time.sleep(1.0)
        self.chrome.kill()  # frozen and ended by the supervisor, or quit
        self.chrome.wait()
        self.chrome = subprocess.Popen([tb.CHROME, "--headless=new", f"--remote-debugging-port={self.port}",
                                        f"--user-data-dir={self.dir.name}", "--no-first-run", "about:blank"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

        def pages():
            try:
                return [t["url"] for t in json.load(urllib.request.urlopen(f"{self.url}/json/list", timeout=1)) if t["type"] == "page"]
            except Exception:  # noqa: BLE001
                return []
        deadline = time.time() + 20
        while time.time() < deadline and not {"data:text/html,first", "data:text/html,second"} <= set(pages()):
            time.sleep(0.3)
        self.assertLessEqual({"data:text/html,first", "data:text/html,second"}, set(pages()))
        listing = json.load(urllib.request.urlopen(f"{self.url}/json/list"))
        viewer = self.stream.Viewer(None, self.cdp, {"id": "0" * 24, "url": None}, None, recent=lambda: tracker.recent(self.url))
        viewer.tabs = self.cdp.page_tabs([{**t, "targetId": t["id"]} for t in listing])
        self.assertEqual(next(t["url"] for t in viewer.tabs if t["id"] == viewer._first_tab()), "data:text/html,second")
