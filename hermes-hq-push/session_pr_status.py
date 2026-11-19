"""Read-only GitHub PR status for session evidence. No transcript is executed."""
import importlib.util
import json
import re
import subprocess
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode

MAX_BATCH = 32
TTL = 60
MAX_CACHE = 512
_TIMEOUT = 8
_MAX_PAGES = 3
_cache = OrderedDict()
_lock = threading.Lock()
_gh_slots = threading.BoundedSemaphore(4)
_PROFILE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}\Z")
_ID = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.:-]{0,255}\Z")
_REPO = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_.-]{1,100}\Z")


def valid_identity(profile, session_id):
    return (isinstance(profile, str) and bool(_PROFILE.fullmatch(profile)) and profile not in ('.', '..')
            and isinstance(session_id, str) and bool(_ID.fullmatch(session_id)) and '..' not in session_id)


def _evidence(profile, session_id):
    # The evidence reader is maintained separately; load it lazily so a missing reader fails closed.
    path = Path(__file__).with_name('session_pr_evidence.py')
    spec = importlib.util.spec_from_file_location('hermes_hq_session_pr_evidence', path)
    if spec is None or spec.loader is None:
        raise ImportError('session evidence unavailable')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.evidence(profile, session_id)


def _api(endpoint):
    # Only GitHub.com, fixed executable/subcommand and validated endpoint; no shell or user-supplied host.
    with _gh_slots:
        proc = subprocess.run(['gh', 'api', '--hostname', 'github.com', endpoint],
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=_TIMEOUT, check=True)
    return json.loads(proc.stdout)


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


def _resolve_evidence(info):
    if info is None:
        return None  # no proven coding: omit this session altogether
    if not isinstance(info, dict):
        return None
    repo, branch, number = info.get('repo'), info.get('branch'), info.get('number')
    valid_repo = (isinstance(repo, str) and bool(_REPO.fullmatch(repo))
                  and repo.split('/')[1] not in ('.', '..') and '..' not in repo)
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
    try:
        if valid_number:
            return _pr_result(_api(f'/repos/{repo}/pulls/{number}'), repo, branch, number)
        # Query the exact head owner and branch, then validate the full head repo as
        # the owner can have multiple repositories with identically named branches.
        owner = repo.split('/')[0]
        matches = []
        for page in range(1, _MAX_PAGES + 1):
            query = urlencode({'state': 'all', 'head': f'{owner}:{branch}', 'per_page': 100, 'page': page})
            prs = _api(f'/repos/{repo}/pulls?{query}')
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
                metadata = _api(f'/repos/{repo}')
                if (isinstance(metadata, dict) and metadata.get('fork') is False
                        and str(metadata.get('full_name', '')).lower() == repo.lower()
                        and metadata.get('default_branch') != branch):
                    return {'state': 'none', **known}
                return unknown
        return unknown
    except Exception:
        return unknown


def resolve(profile, session_id):
    if not valid_identity(profile, session_id):
        return {'profile': profile, 'id': session_id, 'state': 'unknown'}
    key = (profile, session_id)
    now = time.monotonic()
    with _lock:
        entry = _cache.get(key)
        if entry and entry[0] > now:
            _cache.move_to_end(key)
            return ({'profile': profile, 'id': session_id, **entry[1]} if entry[1] is not None else None)
    try:
        result = _resolve_evidence(_evidence(profile, session_id))
    except Exception:
        result = None  # failed evidence collection does not establish coding
    with _lock:
        _cache[key] = (time.monotonic() + TTL, result)
        _cache.move_to_end(key)
        while len(_cache) > MAX_CACHE:
            _cache.popitem(last=False)
    return {'profile': profile, 'id': session_id, **result} if result is not None else None


def resolve_batch(sessions):
    if len(sessions) > MAX_BATCH:
        raise ValueError('too many sessions')
    with ThreadPoolExecutor(max_workers=4) as pool:
        return [row for row in pool.map(lambda row: resolve(row['profile'], row['id']), sessions) if row is not None]
