"""Read-only GitHub PR status for session evidence. No transcript is executed.

Who may learn what:

* The computer's own GitHub sign-in (``gh``, ``GH_TOKEN``, a keychain, ``~/.netrc``) is never used.
  A caller of the route is not necessarily the person ``gh`` is signed in as.
* A repository or branch that was only *inferred* from history may be private. It is sent to GitHub
  only when either
    - the owner listed the repository as public in ``<plugin-data>/session-pr.json``
      (``"public_repos": ["owner/repo"]``); then GitHub is asked anonymously, and only about a PR
      number the session recorded (never a branch name, which may exist only locally), or
    - the signed-in owner put a GitHub token of their own at
      ``<plugin-data>/session-pr-github/<sha256 of their subject>.token`` (mode 0600, owned by this
      user); then GitHub is asked with that token only, for that owner's sessions only.
  Otherwise nothing leaves the computer and the session's state is ``unknown``.
"""
import hashlib
import importlib
import importlib.util
import json
import os
import re
import stat
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote, urlencode

MAX_BATCH = 32
TTL = 60
MAX_CACHE = 512
_TIMEOUT = 8
_MAX_PAGES = 3
_MAX_RESPONSE = 4_000_000
API_ROOT = 'https://api.github.com'
CONFIG_NAME = 'session-pr.json'
TOKEN_DIR = 'session-pr-github'
_cache = OrderedDict()
_lock = threading.Lock()
_gh_slots = threading.BoundedSemaphore(4)
_PROFILE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}\Z")
_ID = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.:-]{0,255}\Z")
_REPO = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_.-]{1,100}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_]{20,255}\Z")

# Everything the route calls on the runtime. The app is told the endpoint exists only when all of it does.
_RUNTIME = (
    ('hermes_cli.web_server_sessions', '_session_mutation_context'),
    ('hermes_cli.profiles', 'get_profile_dir'),
    ('gateway.config', 'Platform'),
    ('gateway.owner_identity', 'same_owner'),
    ('gateway.session_authorities', 'require_serving'),
    ('gateway.session_history_mount', 'is_history_mount'),
    ('hermes_state_local', 'POLICY_PREFIX'),
    ('hermes_state_local_lineage', 'validate_local_lineage'),
    ('hermes_state_local_migration', 'legacy_history_row'),
    ('hermes_state_mutation_binding', 'BINDING_PREFIX'),
    ('hermes_state_mutation_binding', 'binding_names'),
)
_supported = None


def supported():
    """Whether this Hermes has every session-ownership API the route relies on (checked once)."""
    global _supported
    if _supported is None:
        try:
            ok = all(hasattr(importlib.import_module(module), name) for module, name in _RUNTIME)
            ok = ok and hasattr(importlib.import_module('gateway.config').Platform, 'LOCAL')
        except Exception:
            ok = False
        _supported = bool(ok)
    return _supported


def valid_identity(profile, session_id):
    return (isinstance(profile, str) and bool(_PROFILE.fullmatch(profile)) and profile not in ('.', '..')
            and isinstance(session_id, str) and bool(_ID.fullmatch(session_id)) and '..' not in session_id)


def _valid_repo(repo):
    return (isinstance(repo, str) and bool(_REPO.fullmatch(repo))
            and repo.split('/')[1] not in ('.', '..') and '..' not in repo)


def _read_private(path, limit):
    """A regular file owned by this user, not a symlink, readable by nobody else; else None."""
    if not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'getuid'):
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0))
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                or info.st_size > limit):
            return None
        with os.fdopen(fd, 'rb', closefd=False) as handle:
            return handle.read(limit + 1).decode('utf-8')
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        os.close(fd)


def load_config(data_dir):
    """The owner's choices in ``<plugin-data>/session-pr.json``. Anything malformed counts as unset."""
    empty = {'public_repos': frozenset(), 'receipt_roots': ()}
    if not data_dir:
        return empty
    path = Path(data_dir) / CONFIG_NAME
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
            return empty
        raw = json.loads(path.read_text())
    except (OSError, ValueError, UnicodeDecodeError):
        return empty
    if not isinstance(raw, dict):
        return empty
    repos = raw.get('public_repos', [])
    roots = raw.get('receipt_roots', [])
    if (not isinstance(repos, list) or not all(_valid_repo(r) for r in repos)
            or not isinstance(roots, list) or len(roots) > 16
            or not all(isinstance(r, str) and r.startswith('/') and '\x00' not in r for r in roots)):
        return empty
    return {'public_repos': frozenset(r.lower() for r in repos),
            'receipt_roots': tuple(os.path.normpath(r) for r in roots)}


def owner_token_path(data_dir, subject):
    """Where the owner *subject* may keep a GitHub token of their own for this endpoint."""
    digest = hashlib.sha256(str(subject).encode('utf-8')).hexdigest()
    return Path(data_dir) / TOKEN_DIR / f'{digest}.token'


class Access:
    """What GitHub may be asked for one owner's sessions. Never holds the host's credentials."""
    __slots__ = ('public_repos', 'token', 'key')

    def __init__(self, public_repos=frozenset(), token=None):
        self.public_repos = frozenset(public_repos)
        self.token = token
        # Cached answers are shared only between callers with the same access: anonymous, or one token.
        self.key = ('token:' + hashlib.sha256(token.encode()).hexdigest()[:32]) if token else 'anonymous'

    def __repr__(self):
        return f'Access(public_repos={sorted(self.public_repos)!r}, token={"set" if self.token else None})'


def access_for(data_dir, subject, config=None):
    config = config if config is not None else load_config(data_dir)
    token = None
    if data_dir and isinstance(subject, str) and subject:
        text = _read_private(owner_token_path(data_dir, subject), 512)
        if text is not None and _TOKEN.fullmatch(text.strip()):
            token = text.strip()
    return Access(config['public_repos'], token)


def evidence_in(conn, session_id, receipt_roots=()):
    """Read one session's evidence on the caller's connection, inside the caller's read snapshot."""
    # The evidence reader is maintained separately; load it lazily so a missing reader fails closed.
    path = Path(__file__).with_name('session_pr_evidence.py')
    spec = importlib.util.spec_from_file_location('hermes_hq_session_pr_evidence', path)
    if spec is None or spec.loader is None:
        raise ImportError('session evidence unavailable')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.evidence_from_conn(conn, session_id, receipt_roots)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # A redirect could carry an owner's token to another host; a moved repository is simply unknown.
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect())


def _api(endpoint, access):
    """GET api.github.com*endpoint*, anonymously or with the owner's own token; nothing else."""
    if not isinstance(endpoint, str) or not endpoint.startswith('/repos/') or '\\' in endpoint or '@' in endpoint:
        raise ValueError('invalid endpoint')
    headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28',
               'User-Agent': 'hermes-hq-push'}
    if access.token:
        headers['Authorization'] = f'Bearer {access.token}'
    request = urllib.request.Request(API_ROOT + endpoint, headers=headers, method='GET')
    with _gh_slots:
        with _opener.open(request, timeout=_TIMEOUT) as response:
            if response.status != 200:
                raise ValueError('unexpected status')
            body = response.read(_MAX_RESPONSE + 1)
    if len(body) > _MAX_RESPONSE:
        raise ValueError('response too large')
    return json.loads(body)


def _pr_result(pr, repo, branch=None, number=None, head_repo=None):
    if not isinstance(pr, dict):
        raise ValueError('invalid PR')
    num = pr.get('number')
    base = pr.get('base')
    head = pr.get('head')
    if (type(num) is not int or num <= 0 or (number is not None and num != number)
            or not isinstance(base, dict) or not isinstance(base.get('repo'), dict)
            or str(base['repo'].get('full_name', '')).lower() != repo.lower()
            or not isinstance(head, dict) or not isinstance(head.get('ref'), str)
            or (branch is not None and head['ref'] != branch)
            or (head_repo is not None and (not isinstance(head.get('repo'), dict)
                or str(head['repo'].get('full_name', '')).lower() != head_repo.lower()))):
        raise ValueError('mismatched PR')
    state = pr.get('state')
    if state not in ('open', 'closed') or type(pr.get('draft')) is not bool:
        raise ValueError('invalid state')
    if state == 'open':
        state = 'draft' if pr['draft'] else 'open'
    elif pr.get('merged_at'):
        state = 'merged'
    else:
        state = 'closed'
    return {'state': state, 'repo': repo, 'branch': head['ref'], 'number': num,
            'url': f'https://github.com/{repo}/pull/{num}'}


def _resolve_evidence(info, access=None):
    access = access if access is not None else Access()
    if info is None:
        return None  # no proven coding: omit this session altogether
    if not isinstance(info, dict):
        return None
    repo, branch, number = info.get('repo'), info.get('branch'), info.get('number')
    valid_repo = _valid_repo(repo)
    valid_branch = (isinstance(branch, str) and bool(branch) and len(branch) <= 255
                    and not any(ord(c) < 32 or ord(c) == 127 for c in branch))
    valid_number = type(number) is int and 0 < number <= 2147483647
    known = {}
    if valid_repo:
        known['repo'] = repo
    if valid_branch:
        known['branch'] = branch
    if valid_repo and valid_number:
        known.update(number=number, url=f'https://github.com/{repo}/pull/{number}')
    unknown = {'state': 'unknown', **known}
    if (not valid_repo or (branch is not None and not valid_branch)
            or (number is not None and not valid_number) or (number is None and not valid_branch)):
        return unknown
    # Nothing unproven leaves the computer: with no owner token, only a repository the owner
    # declared public is asked about, and only by number (a branch name may be local-only).
    if not access.token and not (valid_number and repo.lower() in access.public_repos):
        return unknown
    key = (access.key, repo.lower(), branch if valid_branch else None, number if valid_number else None)
    now = time.monotonic()
    with _lock:
        entry = _cache.get(key)
        if entry and entry[0] > now:
            _cache.move_to_end(key)
            return dict(entry[1])
    result = _ask_github(repo, branch, number, valid_number, known, unknown, access)
    with _lock:
        _cache[key] = (time.monotonic() + TTL, dict(result))
        _cache.move_to_end(key)
        while len(_cache) > MAX_CACHE:
            _cache.popitem(last=False)
    return result


def _ask_github(repo, branch, number, valid_number, known, unknown, access):
    owner, name = repo.split('/')
    base = f'/repos/{quote(owner, safe="")}/{quote(name, safe="")}'
    try:
        if valid_number:
            return _pr_result(_api(f'{base}/pulls/{number}', access), repo, branch, number)
        # Branch searches happen only with the owner's own token (checked by the caller).
        # Query the exact head owner and branch, then validate the full head repo as
        # the owner can have multiple repositories with identically named branches.
        matches = []
        for page in range(1, _MAX_PAGES + 1):
            query = urlencode({'state': 'all', 'head': f'{owner}:{branch}', 'per_page': 100, 'page': page})
            prs = _api(f'{base}/pulls?{query}', access)
            if not isinstance(prs, list) or len(prs) > 100:
                return unknown
            for pr in prs:
                # A different head repository or malformed response cannot prove
                # which PR belongs to the session.
                matches.append(_pr_result(pr, repo, branch, head_repo=repo))
                if len(matches) > 1:
                    return unknown
            if len(prs) < 100:
                if matches:
                    return matches[0]
                # An empty head query in a fork tells us nothing about upstream.
                metadata = _api(base, access)
                if (isinstance(metadata, dict) and metadata.get('fork') is False
                        and str(metadata.get('full_name', '')).lower() == repo.lower()
                        and metadata.get('default_branch') != branch):
                    return {'state': 'none', **known}
                return unknown
        return unknown
    except Exception:
        return unknown


def resolve_batch(items):
    """*items*: ``{'profile', 'id', 'evidence', 'access'}`` already authorized and read in one snapshot.

    No evidence (None) omits the row; uncertain evidence ({}) is unknown without asking anyone.
    """
    if len(items) > MAX_BATCH:
        raise ValueError('too many sessions')

    def one(item):
        try:
            result = _resolve_evidence(item.get('evidence'), item.get('access'))
        except Exception:
            result = {'state': 'unknown'} if item.get('evidence') is not None else None
        return {'profile': item['profile'], 'id': item['id'], **result} if result is not None else None

    with ThreadPoolExecutor(max_workers=4) as pool:
        return [row for row in pool.map(one, items) if row is not None]
