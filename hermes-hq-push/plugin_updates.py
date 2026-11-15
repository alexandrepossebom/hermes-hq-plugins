"""[0.9.52] A plugin update the owner approved in the app, applied on this computer.

Hermes's app-facing update (``plugins.manage`` update) refuses an update that adds Python packages: "run `hermes plugins
update <name>` in a terminal to review them". The app shows the owner those packages and asks; on yes it starts the
update here. This runs that same command, ``hermes [-p <profile>] plugins update <name>``, on a terminal of its own and
answers the questions the owner already answered in the app. Nothing but Hermes's own command changes the plugin.

[0.9.58] The answer is tied to what the owner was shown (hermes-hq-plugins#3, finding 1). Hermes pulls the new version
into a staging copy before it asks anything, and prints what that copy needs above each question. Each one is answered
yes only when what it prints now is exactly what the app showed:
- "Prepare these with Hermes through PM now?": the Python packages, as Hermes listed them in its refusal (``packages``);
- "Update anyway?" after a "caution" scan: the scan's findings, severity, kind, file and line (``findings``). This
  replaces ``--force``, which accepted whatever the scan found on the second run, and is never passed any more;
- "Apply this update?" for a widened catalog pin: the lines saying what it adds (``capabilities``).
Anything else, something the app didn't send, or a list that differs by one entry is answered no, so Hermes changes
nothing, and the job says what Hermes asks about now (``held``) so the app can show the owner the new version. A true
pin to one revision needs ``hermes plugins update`` to take an expected revision; that is Hermes's to add.

The update runs on a thread; ``start`` answers a job id at once and ``job`` says how it went, so a slow update never
holds a request open and the app can look again after a trip to the background. Not on Windows (no terminal to give
the command): ``status`` says so and the app falls back to asking the main bot.
"""
import os
import re
import select
import subprocess
import sys
import threading
import time
import uuid

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PACKAGES_QUESTION = "Prepare these with Hermes through PM now?"
WIDER_QUESTION = "Apply this update?"
CAUTION_QUESTION = "anyway? Only continue if you trust the source."
# Hermes asks "Grant these capabilities? [y/N]" through rich's console.input, which reads "[y/N]" as a style and
# prints the question without it: without the second form the update sat there until TIMEOUT (it is answered no).
ANY_QUESTION = re.compile(r"(\[y/N\]:?|Grant these capabilities\?)\s*$")
PACKAGES_HEAD = "declares Python dependencies:"
CAUTION_HEAD = "Security scan flagged this plugin:"
WIDER_HEAD = re.compile(r"The new pin of .+ adds:\s*$")
FINDING = re.compile(r"^\s{2}([A-Z]+)\s+(\S+)\s+(.+?):(\d+)\s+\"")
PYPROJECT_SHOWN, PYPROJECT_REFUSAL = "(declared in its pyproject.toml)", "in its pyproject.toml"
TAG = re.compile(r"(?<!\\)\[[a-z#/@][^\[]*?\]")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
TIMEOUT = 300.0
KEEP = 30 * 60.0

_jobs: dict[str, dict] = {}
_lock = threading.Lock()


class UpdateError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


def supported() -> bool:
    return os.name == "posix"


def status() -> dict:
    """`pluginUpdates` (0.9.52): POST /plugins/update applies an update whose Python packages the owner approved.
    `pluginUpdatesChecked` (0.9.58): it answers only when Hermes asks about exactly what the app showed (``packages``,
    ``findings``, ``capabilities``), and a "caution" scan is accepted here, on its findings, not with ``force``."""
    return {"pluginUpdates": supported(), "pluginUpdatesChecked": supported()}


def command(profile: str, name: str) -> list[str]:
    """Hermes's own CLI in this server's Python (the same code the server runs), never through a shell. Never
    ``--force``: a caution scan is answered at its own question, on its findings."""
    args = ["plugins", "update", name]
    if profile and profile != "default":
        args = ["-p", profile] + args
    return [sys.executable, "-c", "import sys; from hermes_cli.main import main; sys.exit(main())"] + args


def shown(text: str) -> str:
    """*text* as Hermes's console prints it: Hermes prints these lines through rich without escaping them, so a
    package's extras (``requests[socks]``) or a ``[x]`` in a file name drop out of what the terminal shows."""
    try:
        from rich.markup import render
        return render(text).plain
    except Exception:
        return TAG.sub("", text)


class Approved:
    """What the owner saw in the app, each part None when the app didn't show it (then that question is answered no)."""

    def __init__(self, packages: str | None = None, findings: list[dict] | None = None,
                 capabilities: list[str] | None = None):
        self.packages = None if packages is None else shown(packages).strip()
        self.findings = None if findings is None else sorted(_finding_key(f) for f in findings)
        self.capabilities = None if capabilities is None else [" ".join(shown(line).split()) for line in capabilities]


def _finding_key(finding: dict) -> tuple:
    line = finding.get("line")
    return (str(finding.get("severity") or "").lower(), str(finding.get("category") or ""),
            shown(str(finding.get("file") or "")).strip(), int(line) if isinstance(line, int) or str(line).isdigit() else -1)


def _block(lines: list[str], head) -> list[str] | None:
    """The lines after the last line that matches *head* (a string it contains, or a pattern), or None."""
    for at in range(len(lines) - 1, -1, -1):
        line = lines[at]
        if (head in line) if isinstance(head, str) else head.search(line):
            return lines[at + 1:]
    return None


def asked(text: str) -> tuple[str, object] | None:
    """The question the output ends on and what Hermes printed above it: ("packages", "a, b"), ("findings", [keys]),
    ("capabilities", [lines]), ("other", None); None while it isn't asking."""
    clean = ANSI.sub("", text).replace("\r", "")
    flat = " ".join(clean.split())
    if not ANY_QUESTION.search(flat):
        return None
    tail = flat[-240:]
    lines = clean.split("\n")
    if PACKAGES_QUESTION in tail:
        block = _block(lines, PACKAGES_HEAD) or []
        deps = [line.strip()[2:].strip() for line in block if line.strip().startswith("- ")]
        deps = [PYPROJECT_REFUSAL if dep == PYPROJECT_SHOWN else dep for dep in deps]
        return "packages", ", ".join(deps)
    if CAUTION_QUESTION in tail:
        block = _block(lines, CAUTION_HEAD) or []
        found = [FINDING.match(line) for line in block]
        return "findings", sorted((m[1].lower(), m[2], m[3].strip(), int(m[4])) for m in found if m)
    if WIDER_QUESTION in tail:
        block = _block(lines, WIDER_HEAD) or []
        return "capabilities", [" ".join(line.split()) for line in block if line.strip() and WIDER_QUESTION not in line]
    return "other", None


def answer_for(text: str, approved: Approved, held: list | None = None) -> str | None:
    """What to type at the question the output ends on, or None while it isn't asking. Yes only when what Hermes
    shows now is exactly what the owner approved; a question answered no is added to *held* with what it showed."""
    question = asked(text)
    if question is None:
        return None
    kind, now = question
    if kind != "other":
        expected = getattr(approved, kind)
        if expected is not None and now and expected == now:
            return "y"
        if held is not None:
            held.append({"kind": kind, "now": now, "changed": expected is not None})
    return "n"


def run(cmd: list[str], approved: Approved | None = None, timeout: float = TIMEOUT,
        held: list | None = None) -> tuple[int, str, list[str]]:
    """Runs `cmd` on a pseudo-terminal -> (exit code, its output, the answers typed). -1: it ran out of time."""
    import pty
    approved = approved or Approved()
    master, slave = pty.openpty()
    env = {**os.environ, "TERM": "dumb", "NO_COLOR": "1", "COLUMNS": "1000", "LINES": "50"}
    proc = subprocess.Popen(cmd, stdin=slave, stdout=slave, stderr=slave, close_fds=True, start_new_session=True, env=env)
    os.close(slave)
    out, asked_from, answers = b"", 0, []
    deadline = time.monotonic() + timeout
    try:
        while True:
            if time.monotonic() > deadline:
                proc.kill()
                proc.wait()
                return -1, _text(out), answers
            ready, _, _ = select.select([master], [], [], 0.25)
            if not ready:
                if proc.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(master, 4096)
            except OSError:
                chunk = b""
            if not chunk:
                break
            out += chunk
            answer = answer_for(out[asked_from:].decode(errors="replace"), approved, held)
            if answer:
                os.write(master, (answer + "\n").encode())
                answers.append(answer)
                asked_from = len(out)
        return proc.wait(), _text(out), answers
    finally:
        os.close(master)


def _text(out: bytes) -> str:
    return ANSI.sub("", out.decode(errors="replace")).replace("\r", "")


def last_lines(text: str, count: int = 6) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-count:])[-1200:]


def start(profile: str, name: str, approved: Approved | None = None, runner=run) -> dict:
    """Starts the update on a thread -> {"job": id}. One update of a plugin for a bot at a time."""
    if not supported():
        raise UpdateError(501, "updating from here needs a Mac or Linux computer")
    if not NAME.match(name or "") or not NAME.match(profile or "default"):
        raise UpdateError(400, "bad plugin or profile name")
    profile = profile or "default"
    now = time.time()
    with _lock:
        for key in [k for k, j in _jobs.items() if j["state"] != "running" and now - j["at"] > KEEP]:
            del _jobs[key]
        for key, job in _jobs.items():
            if job["state"] == "running" and (job["profile"], job["name"]) == (profile, name):
                return {"job": key}
        key = uuid.uuid4().hex[:16]
        _jobs[key] = {"state": "running", "profile": profile, "name": name, "at": now}

    def work():
        held: list[dict] = []
        try:
            code, output, answers = runner(command(profile, name), approved or Approved(), held=held)
        except Exception as error:  # the command couldn't be started
            code, output, answers = -2, str(error), []
        done = {"at": time.time(), "state": "done" if code == 0 else "failed", "code": code,
                "unchanged": "Already up to date" in output, "output": last_lines(output),
                "packages": "y" in answers and PACKAGES_QUESTION in output}
        if held:
            # Hermes asked about something the owner wasn't shown: nothing changed, and the app can show it now.
            done["held"] = held[0]
        if code == -1:
            done["output"] = "The update took too long and was stopped."
        with _lock:
            _jobs[key].update(done)

    threading.Thread(target=work, name="hq-plugin-update", daemon=True).start()
    return {"job": key}


def job(key: str) -> dict:
    with _lock:
        found = _jobs.get(key)
        if found is None:
            raise UpdateError(404, "no such update")
        return {k: v for k, v in found.items() if k != "at"}
