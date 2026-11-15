"""[0.9.52] plugin_updates: the owner's approved update runs Hermes's own command and answers only what was approved.
[0.9.58] Yes only when Hermes asks about exactly what the app showed (hermes-hq-plugins#3). The terminal tests at the
end run Hermes's own questions (``~/.hermes/hermes-agent/venv/bin/python -m pytest -q test_plugin_updates.py``;
HERMES_AGENT names another checkout); without a Hermes checkout they skip."""
import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("hq_plugin_updates_test", Path(__file__).parent / "plugin_updates.py")
updates = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(updates)


def wait(key):
    for _ in range(200):
        found = updates.job(key)
        if found["state"] != "running":
            return found
        time.sleep(0.02)
    raise AssertionError("the update never finished")


PACKAGES = ("\nhomeassistant declares Python dependencies:\n  - tzdata\n  - requests>=2.31; python_version < \"3.12\"\n"
            "  Prepare these with Hermes through PM now? [y/N]: ")
CAUTION = ("\n⚠ Security scan flagged this plugin:\nScan: clip (git/community)  Verdict: CAUTION\n"
           "  HIGH     network        clip/fetch.py:12               \"requests.post(url\"\n"
           "  MEDIUM   obfuscation    clip/my notes.py:3             \"base64.b64decode(\"\n\n"
           "Decision: NEEDS CONFIRMATION — caution\n  Update anyway? Only continue if you trust the source. [y/N]: ")
WIDER = "\n  The new pin of clip adds:\n    Tools: clip_page, clip_pdf\n    Hooks: on_turn\n  Apply this update? [y/N]: "
SHOWN_FINDINGS = [{"severity": "medium", "category": "obfuscation", "file": "clip/my notes.py", "line": 3},
                  {"severity": "high", "category": "network", "file": "clip/fetch.py", "line": 12}]


def test_the_command_is_hermes_own_update_for_the_bot_and_never_forced():
    assert updates.command("default", "homeassistant")[-3:] == ["plugins", "update", "homeassistant"]
    assert updates.command("finance", "homeassistant")[-5:] == ["-p", "finance", "plugins", "update", "homeassistant"]
    assert updates.command("default", "x")[0] == sys.executable
    assert "--force" not in updates.command("finance", "x")


def test_the_packages_question_is_yes_only_for_the_packages_shown():
    shown = 'tzdata, requests>=2.31; python_version < "3.12"'
    assert updates.answer_for(PACKAGES, updates.Approved(packages=shown)) == "y"
    held = []
    assert updates.answer_for(PACKAGES, updates.Approved(), held) == "n", "not shown in the app: no"
    assert held == [{"kind": "packages", "now": shown, "changed": False}], "the app can show them and ask"
    held = []
    assert updates.answer_for(PACKAGES, updates.Approved(packages="tzdata"), held) == "n", "one more than was shown"
    assert held[0]["changed"] is True
    assert updates.answer_for(PACKAGES.replace(">=2.31", ">=2.0"), updates.Approved(packages=shown)) == "n", "another version"
    assert updates.answer_for(PACKAGES, updates.Approved(packages=shown, findings=SHOWN_FINDINGS)) == "y"
    pyproject = "\nx declares Python dependencies:\n  - (declared in its pyproject.toml)\n  Prepare these with Hermes through PM now? [y/N]: "
    assert updates.answer_for(pyproject, updates.Approved(packages="in its pyproject.toml")) == "y", "as the refusal words it"


def test_a_package_with_extras_matches_the_way_hermes_prints_it():
    printed = "\nx declares Python dependencies:\n  - requests>=2.31\n  Prepare these with Hermes through PM now? [y/N]: "
    assert updates.answer_for(printed, updates.Approved(packages="requests[socks]>=2.31")) == "y", "rich drops [socks]"
    assert updates.answer_for(printed, updates.Approved(packages="requests[socks]>=2.32")) == "n"


def test_a_caution_scan_is_yes_only_for_the_findings_shown():
    assert updates.answer_for(CAUTION, updates.Approved(findings=SHOWN_FINDINGS)) == "y", "same findings, any order"
    held = []
    assert updates.answer_for(CAUTION, updates.Approved(packages="tzdata"), held) == "n", "no findings shown: no"
    assert held[0]["kind"] == "findings" and held[0]["changed"] is False
    assert len(held[0]["now"]) == 2
    one = updates.Approved(findings=SHOWN_FINDINGS[1:])
    assert updates.answer_for(CAUTION, one) == "n", "a finding the owner wasn't shown"
    moved = [dict(SHOWN_FINDINGS[0]), SHOWN_FINDINGS[1]]
    moved[0]["line"] = 4
    assert updates.answer_for(CAUTION, updates.Approved(findings=moved)) == "n", "the same finding somewhere else"
    assert updates.answer_for(CAUTION, updates.Approved(findings=[])) == "n", "an empty list approves nothing"


def test_a_widened_pin_is_yes_only_for_what_was_shown_to_be_added():
    lines = ["Tools: clip_page, clip_pdf", "Hooks: on_turn"]
    assert updates.answer_for(WIDER, updates.Approved(capabilities=lines)) == "y"
    assert updates.answer_for(WIDER, updates.Approved(capabilities=lines[:1])) == "n"
    assert updates.answer_for(WIDER, updates.Approved()) == "n", "accept_capabilities alone (0.9.52) is no more"


def test_any_other_question_is_no():
    everything = updates.Approved(packages="tzdata", findings=SHOWN_FINDINGS, capabilities=["Tools: x"])
    held = []
    assert updates.answer_for("installing…\n", everything) is None, "not asking: nothing typed"
    assert updates.answer_for("  Enable 'x' now? [y/N]: ", everything, held) == "n"
    assert updates.answer_for("  Grant these capabilities? [y/N]: ", everything, held) == "n"
    assert updates.answer_for("  Grant these capabilities?  ", everything, held) == "n", "rich drops the [y/N] there"
    assert held == [], "only the three approvals are reported back"
    wrapped = "x declares Python dependencies:\n  - tzdata\nPrepare these with Hermes\nthrough PM now? \x1b[1m[y/N]\x1b[0m: "
    assert updates.answer_for(wrapped, updates.Approved(packages="tzdata")) == "y", "wrapped or coloured, still the same question"


def test_the_route_reads_what_the_app_showed():
    status = updates.status()
    assert status["pluginUpdatesChecked"] is updates.supported() and status["pluginUpdates"] is updates.supported()


def test_bad_names_are_refused():
    for profile, name in (("default", "../etc"), ("a b", "x"), ("default", ""), ("default", "x; rm -rf /"), ("-p", "x")):
        with pytest.raises(updates.UpdateError) as caught:
            updates.start(profile, name, runner=lambda *a, **k: (0, "", []))
        assert caught.value.status_code == 400


def test_a_job_says_how_the_update_went():
    calls = []

    def ok(cmd, approved, held):
        calls.append((cmd, approved))
        return 0, "homeassistant declares Python dependencies:\n - tzdata\nPrepare these with Hermes through PM now? [y/N]: y\nUpdated homeassistant.\n", ["y"]

    approved = updates.Approved(packages="tzdata")
    key = updates.start("finance", "homeassistant", approved=approved, runner=ok)["job"]
    done = wait(key)
    assert done["state"] == "done" and done["code"] == 0 and done["packages"] is True and done["unchanged"] is False
    assert "Updated homeassistant." in done["output"] and "held" not in done
    assert calls[0][0][-5:] == ["-p", "finance", "plugins", "update", "homeassistant"] and calls[0][1] is approved

    def refused(cmd, approved, held):
        held.append({"kind": "packages", "now": "tzdata, numpy", "changed": True})
        return 1, "Update declined: dependency install declined.\n", ["n"]

    key = updates.start("default", "homeassistant", approved=approved, runner=refused)["job"]
    changed = wait(key)
    assert changed["state"] == "failed" and changed["held"] == {"kind": "packages", "now": "tzdata, numpy", "changed": True}

    key = updates.start("default", "honcho", runner=lambda *a, **k: (1, "Error: the security scan blocked it\n", []))["job"]
    failed = wait(key)
    assert failed["state"] == "failed" and "security scan" in failed["output"]

    key = updates.start("default", "slow", runner=lambda *a, **k: (-1, "…", []))["job"]
    assert wait(key)["output"] == "The update took too long and was stopped."

    key = updates.start("default", "same", runner=lambda *a, **k: (0, "Already up to date.\n", []))["job"]
    assert wait(key)["unchanged"] is True

    with pytest.raises(updates.UpdateError) as caught:
        updates.job("nope")
    assert caught.value.status_code == 404


def test_one_update_of_a_plugin_at_a_time():
    import threading
    gate = threading.Event()

    def slow(cmd, approved, held):
        gate.wait(2)
        return 0, "done\n", []

    first = updates.start("default", "busy", runner=slow)["job"]
    assert updates.start("default", "busy", runner=slow)["job"] == first, "the running one, not a second"
    assert updates.start("other", "busy", runner=slow)["job"] != first, "another bot's copy is its own update"
    gate.set()
    assert wait(first)["state"] == "done"


@pytest.mark.skipif(not updates.supported(), reason="needs a pseudo-terminal")
def test_a_real_terminal_gets_the_answer():
    script = ("import sys\n"
              "print('x declares Python dependencies:')\n"
              "print('  - tzdata')\n"
              "a = input('  Prepare these with Hermes through PM now? [y/N]: ')\n"
              "b = input('  Enable it now? [y/N]: ')\n"
              "print('answers', a, b, sys.stdin.isatty())\n"
              "sys.exit(0 if (a, b) == ('y', 'n') else 3)\n")
    code, output, answers = updates.run([sys.executable, "-c", script], updates.Approved(packages="tzdata"), timeout=20)
    assert code == 0 and answers == ["y", "n"]
    assert "answers y n True" in output
    code, _, _ = updates.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.5)
    assert code == -1, "stopped when it runs out of time"


AGENT = os.environ.get("HERMES_AGENT", os.path.expanduser("~/.hermes/hermes-agent"))
needs_hermes = pytest.mark.skipif(not updates.supported() or not os.path.isdir(os.path.join(AGENT, "hermes_cli")),
                                  reason="needs a pseudo-terminal and a Hermes checkout")


def hermes_asks(body: str, approved, held=None):
    """Runs *body* with Hermes's own console and question code on a terminal, as `hermes plugins update` would."""
    script = ("import sys\n"
              f"sys.path.insert(0, {AGENT!r})\n"
              "from hermes_cli import plugins_cmd as pc\n"
              "console = pc._console()\n" + body)
    return updates.run([sys.executable, "-c", script], approved, timeout=60, held=held)


@needs_hermes
def test_hermes_own_packages_question():
    body = ("from hermes_cli.plugins_cmd_install import _consent_python_deps\n"
            "ok, _ = _consent_python_deps('clip', ('requests[socks]>=2.31', 'tzdata; platform_system == \"Windows\"'), console)\n"
            "sys.exit(0 if ok else 4)\n")
    shown = 'requests[socks]>=2.31, tzdata; platform_system == "Windows"'   # the refusal's ", ".join(added)
    code, output, answers = hermes_asks(body, updates.Approved(packages=shown))
    assert (code, answers) == (0, ["y"]), output
    held = []
    code, output, answers = hermes_asks(body, updates.Approved(packages="requests[socks]>=2.31"), held)
    assert (code, answers) == (4, ["n"]), output
    assert held == [{"kind": "packages", "now": 'requests>=2.31, tzdata; platform_system == "Windows"', "changed": True}]


@needs_hermes
def test_hermes_own_caution_question():
    body = ("from tools.skills_guard import Finding, ScanResult\n"
            "from hermes_cli.plugins_cmd_install import interactive_scan_decision\n"
            "result = ScanResult('clip', 'git', 'community', 'caution', [\n"
            "    Finding('net_post', 'high', 'network', 'clip/fetch.py', 12, 'requests.post(url)', 'posts data'),\n"
            "    Finding('b64', 'medium', 'obfuscation', 'clip/my [old] notes.py', 3, 'base64.b64decode(', 'decodes')])\n"
            "sys.exit(0 if interactive_scan_decision(console, 'Update')(result) else 4)\n")
    findings = [{"severity": "high", "category": "network", "file": "clip/fetch.py", "line": 12},
                {"severity": "medium", "category": "obfuscation", "file": "clip/my [old] notes.py", "line": 3}]
    code, output, answers = hermes_asks(body, updates.Approved(findings=findings))
    assert (code, answers) == (0, ["y"]), output
    held = []
    code, output, answers = hermes_asks(body, updates.Approved(findings=findings[:1]), held)
    assert (code, answers) == (4, ["n"]), output
    assert held[0]["kind"] == "findings" and held[0]["changed"] is True and len(held[0]["now"]) == 2


@needs_hermes
def test_hermes_own_widened_pin_lines():
    body = ("from hermes_cli.plugins_cmd_catalog import surface_delta_lines\n"
            "delta = {'tools': ['clip_page', 'clip_pdf']}\n"
            "console.print('\\n  [yellow]The new pin of [bold]clip[/bold] adds:[/yellow]')\n"
            "for line in surface_delta_lines(delta):\n"
            "    console.print(f'    {line}')\n"
            "sys.exit(0 if pc._ask_yes('  Apply this update? [y/N]: ') else 4)\n"
            "")
    lines_body = ("import sys, json\n"
                  f"sys.path.insert(0, {AGENT!r})\n"
                  "from hermes_cli.plugins_cmd_catalog import surface_delta_lines\n"
                  "print(json.dumps(surface_delta_lines({'tools': ['clip_page', 'clip_pdf']})))\n")
    import json
    import subprocess
    lines = json.loads(subprocess.run([sys.executable, "-c", lines_body], capture_output=True, text=True, check=True).stdout)
    code, output, answers = hermes_asks(body, updates.Approved(capabilities=lines))
    assert (code, answers) == (0, ["y"]), output
    code, output, answers = hermes_asks(body, updates.Approved(capabilities=lines + ["Hooks: on_turn"]))
    assert (code, answers) == (4, ["n"]), output


@needs_hermes
def test_hermes_own_grant_question_is_answered_not_waited_out():
    body = "sys.exit(0 if pc._ask_yes('  Grant these capabilities? [y/N] ', console.input) else 4)\n"
    code, output, answers = hermes_asks(body, updates.Approved(packages="x"))
    assert (code, answers) == (4, ["n"]), output
