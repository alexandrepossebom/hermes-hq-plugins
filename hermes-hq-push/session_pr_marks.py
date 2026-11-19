"""What the owner said from the app about chats' code changes: which chats are closed, and which
repository each project's branches live in. Small JSON files in the add-on's data folder.

* ``session-pr-marks.json``: ``{"<profile>/<chat id>": <time marked>}``. A label only: marking a
  chat closed changes nothing in any repository.
* ``session-pr-links.json``: ``{"<project key>": {"path", "main", "published_marker", "public_repo"}}``.
  A link is accepted only for a folder that is a git repository on this computer; git itself is asked.
"""
import json
import os
import re
import threading
import time
from pathlib import Path

MARKS_NAME = 'session-pr-marks.json'
LINKS_NAME = 'session-pr-links.json'
MARKER = 'hq-public-published'      # the file a publisher keeps in the git folder: main's last public commit
MAX_MARKS = 5000
MAX_LINKS = 64
_GITHUB = re.compile(r'(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)'
                     r'([A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}?)(?:\.git)?/?\Z')
_PUBLIC = re.compile(r'[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}\Z')
_lock = threading.Lock()


def _read(data_dir, name):
    try:
        path = Path(data_dir) / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_000_000:
            return {}
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeDecodeError):
        return {}


def _write(data_dir, name, value):
    path = Path(data_dir) / name
    temp = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    with open(temp, 'w', opener=lambda p, flags: os.open(p, flags, 0o600)) as handle:
        json.dump(value, handle, indent=1, sort_keys=True)
    os.replace(temp, path)


# ---- closed marks ----------------------------------------------------------------------------------

def closed(data_dir):
    """The chats marked closed, as ``{"<profile>/<id>", …}``."""
    return frozenset(key for key, value in _read(data_dir, MARKS_NAME).items() if value)


def set_closed(data_dir, profile, session_id, on):
    key = f'{profile}/{session_id}'
    with _lock:
        marks = _read(data_dir, MARKS_NAME)
        if on:
            marks[key] = int(time.time())
            for old in sorted(marks, key=lambda k: marks[k] if isinstance(marks[k], int) else 0)[:-MAX_MARKS]:
                del marks[old]
        else:
            marks.pop(key, None)
        _write(data_dir, MARKS_NAME, marks)


# ---- repositories linked from the app --------------------------------------------------------------

def _local():
    import importlib.util
    import sys
    name = 'hermes_hq_session_pr_local'
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name('session_pr_local.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules[name] = module
    return module


def links(data_dir):
    """``{project key: checked repo}`` as the app linked them (anything malformed is left out)."""
    out = {}
    for project, raw in _read(data_dir, LINKS_NAME).items():
        repos = _local().load_repos([raw])
        if isinstance(project, str) and 0 < len(project) <= 1024 and repos:
            out[project] = repos[0]
    return out


def repos(data_dir):
    """The linked repositories, for the status reader (one entry per folder)."""
    seen, out = set(), []
    for repo in links(data_dir).values():
        if repo['path'] not in seen:
            seen.add(repo['path'])
            out.append(repo)
    return tuple(out)


def _shape(project, repo):
    return {'project': project, 'path': repo['path'], 'main': repo['main'], 'public_repo': repo['public_repo'],
            'mirror': bool(repo['published_marker'])}


def listed(data_dir, config):
    """What the app shows: its own links (with their project), then the hand-listed ones (no project)."""
    mine = links(data_dir)
    out = [_shape(project, repo) for project, repo in sorted(mine.items())]
    linked = {repo['path'] for repo in mine.values()}
    out += [_shape(None, repo) for repo in config.get('by_hand', ()) if repo['path'] not in linked]
    return out


def link(data_dir, project, path, public_repo=None):
    """Link *project* to the git repository at *path* on this computer. ``ValueError`` names why not:
    ``invalid_path``, ``invalid_public_repo`` or ``not_a_repository``."""
    local = _local()
    if (not isinstance(path, str) or not path.startswith('/') or '\x00' in path or '\n' in path or len(path) > 1024
            or not isinstance(project, str) or not 0 < len(project) <= 1024):
        raise ValueError('invalid_path')
    public_repo = (public_repo or '').strip() or None
    if public_repo is not None and not _PUBLIC.fullmatch(public_repo):
        raise ValueError('invalid_public_repo')
    path = os.path.normpath(path)
    if not os.path.isdir(path):
        raise ValueError('not_a_repository')
    top = (local._git(path, 'rev-parse', '--show-toplevel') or '').strip()
    common = (local._git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir') or '').strip()
    if not top or not common:
        raise ValueError('not_a_repository')
    # A worktree's folder links the repository it belongs to: branches live there.
    root = str(Path(common).parent) if Path(common).name == '.git' else top
    main = None
    for name in ('main', 'master'):
        if local._git(root, 'rev-parse', '--verify', '--quiet', f'refs/heads/{name}^{{commit}}'):
            main = name
            # A checkout that only ever fetches keeps its own main behind: the remote's is the one that moves.
            remote = f'origin/{name}'
            if (local._git(root, 'rev-parse', '--verify', '--quiet', remote + '^{commit}')
                    and local._git(root, 'merge-base', '--is-ancestor', name, remote) is not None
                    and local._git(root, 'merge-base', '--is-ancestor', remote, name) is None):
                main = remote
            break
    if main is None:
        head = (local._git(root, 'symbolic-ref', '--quiet', '--short', 'refs/remotes/origin/HEAD') or '').strip()
        if head and local._git(root, 'rev-parse', '--verify', '--quiet', head + '^{commit}'):
            main = head
    if main is None:
        raise ValueError('not_a_repository')
    marker = MARKER if (Path(common) / MARKER).is_file() else None
    if public_repo is None and marker is None:
        match = _GITHUB.fullmatch((local._git(root, 'remote', 'get-url', 'origin') or '').strip())
        public_repo = match.group(1) if match else None
    raw = {'path': root, 'main': main, 'published_marker': marker, 'public_repo': public_repo}
    checked = local.load_repos([raw])
    if not checked:
        raise ValueError('invalid_path')
    with _lock:
        stored = _read(data_dir, LINKS_NAME)
        stored[project] = raw
        if len(stored) > MAX_LINKS:
            raise ValueError('invalid_path')
        _write(data_dir, LINKS_NAME, stored)
    return _shape(project, checked[0])


def unlink(data_dir, project):
    with _lock:
        stored = _read(data_dir, LINKS_NAME)
        if stored.pop(project, None) is not None:
            _write(data_dir, LINKS_NAME, stored)
