"""Where a session's own code changes stand in the owner's checkouts: not merged, merged, public.

Many people never open a pull request by hand: a chat works on a branch, the branch is merged into
main, and a publisher exports main to a public repository. GitHub then only ever shows "merged", so
this module answers from the owner's own repositories instead.

What is a session's work: the branches its own tool calls made (``git worktree add -b``,
``git switch -c``, ``git checkout -b``) or committed on (git's own ``[branch 1a2b3c4] …`` line in the
result of a ``git commit`` it ran), and the commits those lines name. A subagent's are its parent's.
History is data: no command in it is run, and no path in it is opened. Git is asked only inside the
repositories the owner listed (``"repos"`` in ``session-pr.json``), read-only, with a branch name or
commit id that was checked letter by letter first.

What it says, per branch:

* ``draft``: the branch has commits that are not in main (work waiting to be merged).
* ``merged``: in main. Where main is published elsewhere (a ``published_marker``) that step is not a
  state of its own: the owner reads "merged into main" as merged (0.9.69 showed "open" until the
  publisher ran, which read as a mistake). Once public, a receipt file names the pull request.
* ``closed``: the branch was deleted with its commits in no branch and not in main (given up).
"""
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

_TERMINAL = ('terminal', 'functions.terminal')
_BRANCH = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,199}\Z')
_SHA = re.compile(r'[0-9a-f]{7,40}\Z')
_COMMITTED = re.compile(r'^\[([^\s\]]+) (?:\(root-commit\) )?([0-9a-f]{7,40})\] ', re.M)
_DETACHED = re.compile(r'^\[detached HEAD ([0-9a-f]{7,40})\] ', re.M)
_MADE = re.compile(r"(?:Preparing worktree \(new branch|Switched to a new branch) '([^']+)'")
_PR_URL = re.compile(r'https://github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_.-]{1,100})/pull/([1-9][0-9]{0,8})\Z')
_ENV = re.compile(r'[A-Za-z_][A-Za-z0-9_]*=')
_WRITES = ('commit', 'worktree', 'switch', 'checkout', 'merge', 'cherry-pick', 'am', 'rebase')
_MAX_ROWS = 3000
_MAX_FIELD = 200_000
_MAX_DEPTH = 3
_MAX_FAMILY = 200
MAX_BRANCHES = 12
_GIT_TIMEOUT = 4
_CHAIN = 4000        # how far back main's own line is read
TTL = 20             # a repository's refs are read again after this long

_lock = threading.Lock()
_repo_cache = {}     # path -> (expires, snapshot)
_answer_cache = {}   # (path, main sha, marker sha, branch, tip or shas) -> answer


def _plain_branch(name):
    return (isinstance(name, str) and bool(_BRANCH.fullmatch(name)) and '..' not in name and '//' not in name
            and not name.endswith(('/', '.', '.lock')) and name not in ('HEAD', 'main', 'master', 'detached'))


def _segments(command):
    """Split shell text into simple commands, as data. Never evaluated or expanded."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|\n')
        lexer.whitespace_split = True
        lexer.whitespace = ' \t\r'
        lexer.commenters = ''
        parts, current = [], []
        for token in lexer:
            if token and all(c in ';&|\n' for c in token):
                if current:
                    parts.append(current)
                    current = []
            else:
                current.append(token)
        if current:
            parts.append(current)
        return parts
    except (ValueError, TypeError):
        return []


def _git_args(tokens):
    """The words after ``git`` and its own options (``-C dir``, ``-c k=v``), or None."""
    i = 0
    while i < len(tokens) and _ENV.match(tokens[i]):
        i += 1
    if tokens[i:i + 1] != ['git']:
        return None
    i += 1
    while i < len(tokens):
        if tokens[i] in ('-C', '-c', '--git-dir', '--work-tree', '--namespace') and i + 1 < len(tokens):
            i += 2
        elif tokens[i].startswith('-'):
            i += 1
        else:
            break
    return tokens[i:]


def _option(args, *names):
    for index, value in enumerate(args):
        if value in names and index + 1 < len(args):
            return args[index + 1]
    return None


def _made_by(args):
    """The branch a git command makes, from its words alone (quiet commands print nothing)."""
    if not args:
        return None
    if args[0] == 'worktree' and args[1:2] == ['add']:
        return _option(args[2:], '-b', '-B')
    if args[0] == 'switch':
        return _option(args[1:], '-c', '-C', '--create', '--force-create')
    if args[0] == 'checkout':
        return _option(args[1:], '-b', '-B')
    return None


def _failed(content):
    try:
        value = json.loads(content)
    except (ValueError, TypeError):
        return False
    if not isinstance(value, dict):
        return False
    code = value.get('exit_code')
    return bool(value.get('error')) or (code is not None and code != 0)


def _output(content):
    try:
        value = json.loads(content)
    except (ValueError, TypeError):
        return content if isinstance(content, str) else ''
    return str(value.get('output') or '') if isinstance(value, dict) else ''


def _family(db, session_id):
    ids, frontier = [session_id], [session_id]
    for _ in range(_MAX_DEPTH):
        if not frontier or len(ids) > _MAX_FAMILY:
            break
        marks = ','.join('?' * len(frontier))
        frontier = [row[0] for row in db.execute(
            f"SELECT id FROM sessions WHERE source = 'subagent' AND parent_session_id IN ({marks}) LIMIT ?",
            (*frontier, _MAX_FAMILY + 1)) if row[0] not in ids]
        ids.extend(frontier)
    return ids


def work_from_conn(db, session_id):
    """``{'branches': {name: [commit ids]}, 'commits': [ids on no branch]}`` for a session and its
    subagents, newest branch last; empty when it made none; ``None`` when its history can't be read."""
    if not isinstance(session_id, str) or not session_id:
        return None
    try:
        if db.execute('SELECT 1 FROM sessions WHERE id = ?', (session_id,)).fetchone() is None:
            return None
        family = _family(db, session_id)
        if len(family) > _MAX_FAMILY:
            return None
        found = []    # (message row id, branch or None, commit id or None)
        for member in family:
            part = _scan(db, member)
            if part is None:
                return None
            found.extend(part)
    except (sqlite3.Error, ValueError):
        return None
    branches, commits = {}, []
    for _, branch, sha in sorted(found, key=lambda item: item[0]):
        if branch is None:
            if sha not in commits:
                commits.append(sha)
            continue
        shas = branches.pop(branch, [])     # worked on again: it is the newest
        if sha and sha not in shas:
            shas.append(sha)
        branches[branch] = shas[-8:]
    for name in list(branches)[:-MAX_BRANCHES]:
        del branches[name]
    return {'branches': branches, 'commits': commits[-8:]}


def _scan(db, session_id):
    rows = db.execute(
        "SELECT id, tool_calls FROM messages WHERE session_id = ? AND role = 'assistant' AND tool_calls IS NOT NULL "
        "AND tool_calls LIKE '%git %' AND (tool_calls LIKE '%commit%' OR tool_calls LIKE '%worktree%' "
        "OR tool_calls LIKE '%switch%' OR tool_calls LIKE '%checkout%') ORDER BY id LIMIT ?",
        (session_id, _MAX_ROWS + 1)).fetchall()
    if len(rows) > _MAX_ROWS:
        return None
    calls = {}     # tool call id -> (row id, [branches its words make], it commits)
    for row_id, raw in rows:
        try:
            listed = json.loads(raw)
        except (ValueError, TypeError):
            continue
        for call in listed if isinstance(listed, list) else []:
            fn = call.get('function') if isinstance(call, dict) else None
            if not isinstance(fn, dict) or fn.get('name') not in _TERMINAL or not call.get('id'):
                continue
            try:
                args = json.loads(fn.get('arguments') or '{}')
            except (ValueError, TypeError):
                continue
            command = args.get('command') if isinstance(args, dict) else None
            if not isinstance(command, str) or len(command) > _MAX_FIELD:
                continue
            made, writes = [], False
            for tokens in _segments(command):
                git = _git_args(tokens)
                if not git or git[0] not in _WRITES:
                    continue
                writes = True
                name = _made_by(git)
                if _plain_branch(name):
                    made.append(name)
            if writes:
                calls[call['id']] = (row_id, made)
    found = []
    ids = list(calls)
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        marks = ','.join('?' * len(chunk))
        for call_id, content in db.execute(
                f"SELECT tool_call_id, substr(content, 1, ?) FROM messages WHERE session_id = ? AND role = 'tool' "
                f"AND tool_call_id IN ({marks})", (_MAX_FIELD, session_id, *chunk)):
            row_id, made = calls[call_id]
            if not isinstance(content, str) or _failed(content):
                continue
            for name in made:
                found.append((row_id, name, None))
            text = _output(content)
            for name in _MADE.findall(text):
                if _plain_branch(name):
                    found.append((row_id, name, None))
            for name, sha in _COMMITTED.findall(text):
                if _plain_branch(name):
                    found.append((row_id, name, sha))
            for sha in _DETACHED.findall(text):
                found.append((row_id, None, sha))
    return found


# ---- the owner's repositories ---------------------------------------------------------------------

def load_repos(raw):
    """The ``"repos"`` list of ``session-pr.json``, checked; anything malformed is left out."""
    repos = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        path = item.get('path')
        main = item.get('main', 'main')
        marker, receipts, public = item.get('published_marker'), item.get('receipts'), item.get('public_repo')
        plain = lambda v: v is None or (isinstance(v, str) and re.fullmatch(r'[A-Za-z0-9._-]{1,80}', v) and '..' not in v)
        if (not isinstance(path, str) or not path.startswith('/') or '\x00' in path or len(path) > 1024
                or not isinstance(main, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,99}', main) or '..' in main
                or not plain(marker) or not plain(receipts)
                or not (public is None or (isinstance(public, str) and re.fullmatch(r'[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}', public)))):
            continue
        repos.append({'path': os.path.normpath(path), 'main': main, 'published_marker': marker,
                      'receipts': receipts, 'public_repo': public})
    return tuple(repos[:16])


_git_binary = None


def _git(path, *args):
    """One read-only git question inside an owner-listed repository; None when it fails."""
    global _git_binary
    if _git_binary is None:
        _git_binary = shutil.which('git') or '/usr/bin/git'
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': os.environ.get('HOME', '/'),
           'GIT_OPTIONAL_LOCKS': '0', 'GIT_TERMINAL_PROMPT': '0', 'GIT_CONFIG_NOSYSTEM': '1', 'LC_ALL': 'C'}
    try:
        # A repository's own config must not make a read run a program of its choosing.
        done = subprocess.run([_git_binary, '-C', path, '--no-pager', '-c', 'core.fsmonitor=false',
                               '-c', 'core.hooksPath=/dev/null', *args], capture_output=True, text=True,
                              timeout=_GIT_TIMEOUT, env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


# What git said is kept for as long as it stays true, because each question is a process started on a
# computer that may be busy building (measured: 180 questions and 12 s for 32 chats under load). A
# repository costs one question per TTL (its branches and their tips); everything else is asked once per
# main commit, per branch tip or per commit, and a batch of chats whose branches didn't move asks nothing.
_static = {}         # path -> its git folder
_by_main = {}        # (path, main sha) -> main's own line; (path, main sha, 'merges') -> its merge commits
_facts = {}          # (path, question, …) -> the answer, for questions whose answer can't change
_MAX_FACTS = 50000


def _fact(key, ask):
    with _lock:
        if key in _facts:
            return _facts[key]
    value = ask()
    with _lock:
        if len(_facts) > _MAX_FACTS:
            _facts.clear()
        _facts[key] = value
    return value


def _contains(path, commit, tip):
    """Whether *tip*'s history holds *commit* (two commit ids: the answer never changes)."""
    return _fact((path, 'in', commit, tip), lambda: _git(path, 'merge-base', '--is-ancestor', commit, tip) is not None)


def _snapshot(repo):
    """main, its own line of commits (newest first, with subjects), every branch's tip, the marker and
    the receipts."""
    path = repo['path']
    now = time.monotonic()
    with _lock:
        kept = _repo_cache.get(path)
        if kept and kept[0] > now:
            return kept[1]
    snap = None
    common = _static.get(path)
    if common is None:
        common = (_git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir') or '').strip()
        if common:
            _static[path] = common
    listed = _git(path, 'for-each-ref', '--format=%(objectname) %(refname)', 'refs/heads', 'refs/remotes') if common else None
    heads, remotes = {}, {}
    for line in (listed or '').splitlines():
        sha, _, ref = line.partition(' ')
        if ref.startswith('refs/heads/'):
            heads[ref[len('refs/heads/'):]] = sha
        elif ref.startswith('refs/remotes/'):
            remotes[ref[len('refs/remotes/'):]] = sha
    main = heads.get(repo['main']) or remotes.get(repo['main']) or ''
    if common and listed is not None and not main:      # a main named some other way (a tag, a full ref)
        main = (_git(path, 'rev-parse', '--verify', '--quiet', repo['main'] + '^{commit}') or '').strip()
    if common and _SHA.fullmatch(main):
        def line():
            chain, subjects = [], {}
            for row in (_git(path, 'log', '--first-parent', f'--max-count={_CHAIN}', '--format=%H %s', main) or '').splitlines():
                sha, _, subject = row.partition(' ')
                chain.append(sha)
                subjects[sha] = subject
            return chain, {sha: i for i, sha in enumerate(chain)}, subjects
        with _lock:
            for key in [k for k in _by_main if k[0] == path and k[1] != main]:
                del _by_main[key]            # an older main's line is not asked about again
            known = _by_main.get((path, main))
        if known is None:
            known = line()
            with _lock:
                _by_main[(path, main)] = known
        chain, index, subjects = known
        marker = None
        if repo['published_marker']:
            try:
                text = (Path(common) / repo['published_marker']).read_text()[:64].strip()
                marker = text if _SHA.fullmatch(text) else None
            except OSError:
                marker = None
        receipts = {}
        if repo['receipts']:
            try:
                file = Path(common) / repo['receipts']
                if file.is_file() and not file.is_symlink() and file.stat().st_size <= 4_000_000:
                    for row in file.read_text().splitlines():
                        try:
                            entry = json.loads(row)
                        except ValueError:
                            continue
                        commit, url = (entry.get('commit'), entry.get('url')) if isinstance(entry, dict) else (None, None)
                        match = _PR_URL.fullmatch(url) if isinstance(url, str) else None
                        if isinstance(commit, str) and re.fullmatch(r'[0-9a-f]{8,40}', commit) and match:
                            receipts[commit[:8]] = (match.group(1), int(match.group(2)))
            except (OSError, UnicodeDecodeError):
                receipts = {}
        snap = {'main': main, 'chain': chain, 'index': index, 'subjects': subjects, 'marker': marker,
                'receipts': receipts, 'heads': heads,
                # Every branch and its tip, in one word: an answer is good until this changes.
                'refs': hashlib.sha1((listed or '').encode()).hexdigest()}
    with _lock:
        _repo_cache[path] = (now + TTL, snap)
    return snap


def _merges(repo, snap):
    """The merge commits in main's history, newest first, as ``(commit, subject)`` (asked once per main)."""
    key = (repo['path'], snap['main'], 'merges')
    with _lock:
        known = _by_main.get(key)
    if known is None:
        out = _git(repo['path'], 'log', '--merges', '--max-count=20000', '--format=%H %s', snap['main']) or ''
        known = [tuple(row.partition(' ')[::2]) for row in out.splitlines()]
        with _lock:
            _by_main[key] = known
    return known


def _merged_branches(repo, snap):
    """The branches whose tip main already holds: one question for all of them, per main and set of tips."""
    def ask():
        out = _git(repo['path'], 'for-each-ref', '--merged', snap['main'], '--format=%(refname)', 'refs/heads')
        return None if out is None else frozenset(ref[len('refs/heads/'):] for ref in out.splitlines())
    return _fact((repo['path'], 'merged', snap['main'], snap['refs']), ask)


def _landed(repo, snap, commit):
    """The commit of main's own line that brought *commit* in: the oldest one that contains it."""
    chain = snap['chain']
    if commit in snap['index']:
        return commit
    low, high = 0, len(chain) - 1          # newest first: containing commits are a prefix
    if high < 0 or _contains(repo['path'], commit, chain[high]):
        return None                         # older than the part of main that was read
    while low < high:
        middle = (low + high + 1) // 2
        if _contains(repo['path'], commit, chain[middle]):
            low = middle
        else:
            high = middle - 1
    return chain[low]


def _merged_answer(repo, snap, commit, branch):
    """In main. *commit* (the branch's tip or a commit of it, when known) says which pull request
    carried it, once main's public copy has it."""
    answer = {'state': 'merged', 'branch': branch}
    marker = snap['marker']
    public = True
    if repo['published_marker'] and marker != snap['main']:
        public = marker is not None and commit is not None and _contains(repo['path'], commit, marker)
    if public and commit is not None and snap['receipts']:
        landed = _landed(repo, snap, commit)
        receipt = snap['receipts'].get(landed[:8]) if landed else None
        if receipt:
            answer.update(repo=receipt[0], number=receipt[1], url=f'https://github.com/{receipt[0]}/pull/{receipt[1]}')
    if 'repo' not in answer and repo['public_repo']:
        answer['repo'] = repo['public_repo']
    return answer


def _draft(repo, branch):
    answer = {'state': 'draft', 'branch': branch}
    if repo['public_repo']:
        answer['repo'] = repo['public_repo']
    if not repo['published_marker']:
        answer['direct'] = True     # pull requests are opened on this repository itself: GitHub may know better
    return answer


_NOTHING = {'state': 'nothing'}     # the branch is known here and holds no work of its own


def _given_up(repo, snap, commit, branch):
    """A commit whose branch is gone: merged if main has it, still waiting if another branch carries
    it (a rename), else closed."""
    path = repo['path']
    if _contains(path, commit, snap['main']):
        return _merged_answer(repo, snap, commit, branch)
    carried = _fact((path, 'carried', commit, snap['refs']), lambda: bool(
        (_git(path, 'for-each-ref', '--count=1', '--format=%(refname)', '--contains', commit, 'refs/heads') or '').strip()))
    if carried:
        return _draft(repo, branch)
    answer = {'state': 'closed', 'branch': branch}
    if repo['public_repo']:
        answer['repo'] = repo['public_repo']
    return answer


def _branch_answer(repo, snap, branch, shas, tips=None):
    """An answer, ``_NOTHING``, or None when this repository doesn't know the branch at all.

    *tips* remembers the tip of each branch seen waiting, so a branch deleted later can still be told
    apart: merged, or given up."""
    path = repo['path']
    memory = f'{path}\n{branch}'
    tip = snap['heads'].get(branch, '')
    if _SHA.fullmatch(tip):
        merged = _merged_branches(repo, snap)
        if merged is None:
            return None
        if branch in merged:
            if tip in snap['index'] and not any(_exists(repo, snap, sha) for sha in shas):
                # On main's own line: main was moved up to it (a fast-forward), or it never got a
                # commit. Its own log says: the commit it was made from, and whether it moved since.
                moves = _fact((path, 'moves', branch, tip), lambda: (
                    _git(path, 'reflog', 'show', '--format=%H', f'refs/heads/{branch}') or '').split())
                if not moves or moves[-1] == tip:
                    return _NOTHING
            return _merged_answer(repo, snap, tip, branch)
        ahead = _fact((path, 'cherry', snap['main'], tip), lambda: _git(path, 'cherry', snap['main'], tip))
        if ahead is None:
            return None
        if any(line.startswith('+') for line in ahead.splitlines()):
            if tips is not None and tips.get(memory) != tip:
                tips[memory] = tip
            return _draft(repo, branch)
        if not ahead.strip():
            return _NOTHING        # nothing of its own (a branch behind main)
        return _merged_answer(repo, snap, None, branch)      # every change is in main under another commit
    # The branch is gone (deleted after its merge, as many do). Its commits, or a merge that names it, say.
    known = [sha for sha in shas if _exists(repo, snap, sha)]
    remembered = tips.get(memory) if tips is not None else None
    if remembered and _SHA.fullmatch(remembered) and _exists(repo, snap, remembered):
        known.append(remembered)
    if known:
        return _given_up(repo, snap, known[-1], branch)
    # A merge commit in main's history that names the branch ("Merge branch 'x'", "… into x").
    named = re.compile(r'(?<![A-Za-z0-9._/-])' + re.escape(branch) + r'(?![A-Za-z0-9_/-])')
    for sha, subject in _merges(repo, snap):
        if branch in subject and subject.startswith('Merge') and named.search(subject):
            return _merged_answer(repo, snap, sha, branch)
    return None


def _exists(repo, snap, sha):
    """Whether the repository has that commit. Yes is for good; no holds until a branch moves."""
    if not _SHA.fullmatch(sha):
        return False
    path = repo['path']
    with _lock:
        if (path, 'has', sha) in _facts:
            return True
    def ask():
        return _git(path, 'cat-file', '-e', sha + '^{commit}') is not None
    if _fact((path, 'lacks', sha, snap['refs']), lambda: not ask()):
        return False
    with _lock:
        _facts[(path, 'has', sha)] = True
    return True


_RANK = {'draft': 3, 'merged': 2, 'closed': 1}


def local_state(work, repos, tips=None):
    """``(answer, unplaced)`` for a session's own work in the owner's repositories. *answer* is None
    when none of it holds work; *unplaced* counts branches no listed repository knows (so "nothing"
    can't be said of them).

    Work still waiting wins: a chat with an unmerged branch is not done because an older one merged.
    """
    if not work or not repos:
        return None, 0
    snaps = [(repo, snap) for repo, snap in ((repo, _snapshot(repo)) for repo in repos) if snap is not None]
    best, unplaced = None, 0
    items = [(name, tuple(shas)) for name, shas in work.get('branches', {}).items()]
    items += [(None, (sha,)) for sha in work.get('commits', [])]
    for branch, shas in items:
        placed = False
        for repo, snap in snaps:
            remembered = tips.get(f"{repo['path']}\n{branch}") if tips is not None and branch is not None else None
            # Everything an answer rests on: main, the marker, every branch's tip, the receipts, the tip remembered.
            key = (repo['path'], snap['main'], snap['marker'], snap['refs'], len(snap['receipts']), branch, shas, remembered)
            with _lock:
                cached = _answer_cache.get(key, False)
            if cached is False:
                if branch is None:
                    sha = shas[0]
                    cached = None
                    if _exists(repo, snap, sha):
                        cached = _NOTHING
                        if sha not in snap['index'] and _contains(repo['path'], sha, snap['main']):
                            cached = _merged_answer(repo, snap, sha, None)
                            cached.pop('branch', None)
                elif not _plain_branch(branch):
                    cached = None
                else:
                    cached = _branch_answer(repo, snap, branch, shas, tips)
                with _lock:
                    if len(_answer_cache) > 20000:
                        _answer_cache.clear()
                    _answer_cache[key] = cached
            if cached is None:
                continue
            placed = True
            if cached['state'] != 'nothing' and (best is None or _RANK[cached['state']] >= _RANK[best['state']]):
                best = dict(cached)
        if not placed and branch is not None:
            unplaced += 1
    return best, unplaced
