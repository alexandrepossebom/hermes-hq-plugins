"""Evidence of coding work in one Hermes profile/session, without running history.

Only structured tool calls and their paired results are considered. In particular,
URLs in conversation, review output, and a checkout's *current* branch are not
historical PR evidence. Conflicting identities fail closed instead of guessing.

A transcript is data, never a pointer: no path recorded in history (a tool call's
file, a ``cd``/``-C``/workdir, the session's recorded cwd or repo root) is opened,
resolved, stat'ed or followed. Paths are compared as normalized text only. The one
filesystem read is an owner-configured checkout root's own receipt file
(``receipt_roots``), which comes from plugin configuration, not from history.
"""
import json
import os
import posixpath
import re
import shlex
import sqlite3
from contextlib import closing
from pathlib import Path

_PROFILE = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}\Z')
_REPO = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z')
_PR = re.compile(r'^https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)/?$')
_REMOTE = re.compile(r'^(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$')
_CODE = re.compile(r'\.(?:py|js|jsx|ts|tsx|swift|go|rs|java|kt|c|cc|cpp|h|hpp|rb|php|html|css|sql|sh|yaml|yml|toml|json)$', re.I)
_MAX_ROWS = 400
_MAX_FIELD = 65536
_RECEIPTS = 'hq-public-pr-receipts.jsonl'
_RECEIPT_REPO = 'mrcharlesiv/hermes-hq-app'
_MAX_RECEIPT_BYTES = 4_000_000
_MAX_WORKTREES = 256


def _path(value, base=None):
    """Normalize a recorded path as text. Never touches the filesystem (no resolve, no ~user lookup)."""
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    if any(c in value for c in '$`\x00\n\r') or value.startswith('~'):
        return None
    if not value.startswith('/'):
        if not base:
            return None
        value = base.rstrip('/') + '/' + value
    normal = posixpath.normpath(value)
    if normal.startswith('//'):
        normal = '/' + normal.lstrip('/')
    return normal


def _within(child, parent):
    """Whether *child* is *parent* or lies below it, as text. A shallow parent (``/``, ``/tmp``)
    is too broad to tie two unrelated checkouts together."""
    if child is None or parent is None:
        return False
    if child == parent:
        return True
    return len([p for p in parent.split('/') if p]) >= 2 and child.startswith(parent.rstrip('/') + '/')


def _canonical(path, aliases):
    """Map a path inside a worktree this session added to the checkout it was added from."""
    if path is None:
        return None
    for target, source in aliases.items():
        if source and _within(path, target):
            return source + path[len(target):]
    return path


def _read_bounded(path, limit):
    """Read a regular file under an owner-configured root, refusing symlinks and oversize files."""
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        import stat
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise OSError('not a bounded regular file')
        with os.fdopen(fd, 'rb', closefd=False) as handle:
            return handle.read(limit + 1).decode('utf-8')
    finally:
        os.close(fd)


def _checkouts(root):
    """The working trees of one configured main checkout: the root and its linked worktrees.

    Reads only ``<root>/.git`` (which must be a real directory) and its ``worktrees/*/gitdir``
    files; the worktree paths found there are compared as text, never opened.
    """
    gitdir = Path(root) / '.git'
    if gitdir.is_symlink() or not gitdir.is_dir():
        return None, set()
    trees = {root}
    worktrees = gitdir / 'worktrees'
    if worktrees.is_dir() and not worktrees.is_symlink():
        for index, entry in enumerate(sorted(worktrees.iterdir())):
            if index >= _MAX_WORKTREES:
                return None, set()
            try:
                text = _read_bounded(entry / 'gitdir', 4096).strip()
            except (OSError, UnicodeDecodeError):
                continue
            dotgit = _path(text)
            if dotgit and dotgit.endswith('/.git'):
                trees.add(dotgit[:-len('/.git')])
    return gitdir, trees


def _local_receipts(roots, contexts, branch):
    """Read explicitly enabled producer receipts from one configured checkout, rejecting conflicts.

    Every context must lie within the same configured checkout (its root or one of its linked
    worktrees, compared as text); the session's own paths decide nothing about where to read.
    """
    if (os.environ.get('HERMES_HQ_PUBLIC_PR_RECEIPTS') != '1' or not roots or not branch
            or not contexts or None in contexts):
        return set()
    owners = set()
    for root in roots:
        try:
            gitdir, trees = _checkouts(root)
        except OSError:
            continue
        if gitdir is not None and all(any(_within(context, tree) for tree in trees) for context in contexts):
            owners.add(gitdir)
    if len(owners) != 1:
        return set()
    try:
        text = _read_bounded(next(iter(owners)) / _RECEIPTS, _MAX_RECEIPT_BYTES)
        entries = [json.loads(line) for line in text.splitlines()]
        matches = set()
        for entry in entries:
            if not isinstance(entry, dict) or entry.get('version') != 1:
                return set()
            if entry.get('branch') == branch:
                match = _PR.fullmatch(entry.get('url', ''))
                if not match or match.group(1) != _RECEIPT_REPO:
                    return set()
                matches.add((match.group(1), int(match.group(2))))
        for repo, number in matches:
            url = f'https://github.com/{repo}/pull/{number}'
            if any(entry.get('branch') != branch and str(entry.get('url', '')).rstrip('/') == url
                   for entry in entries):
                return set()
        return matches
    except (OSError, ValueError, TypeError, UnicodeDecodeError):
        return set()


def _command_path(tokens, cwd):
    path = cwd
    if tokens and tokens[0] == 'git':
        i = 1
        while i < len(tokens):
            token = tokens[i]
            if token == '-C' and i + 1 < len(tokens):
                path = _path(tokens[i + 1], path)
                i += 2
            elif token.startswith('-C') and len(token) > 2:
                path = _path(token[2:], path)
                i += 1
            elif token in ('-c', '--git-dir', '--work-tree', '--namespace') and i + 1 < len(tokens):
                # Explicit git-dir/work-tree can override the meaning of -C.
                if token != '-c':
                    return None
                i += 2
            elif token.startswith(('--git-dir=', '--work-tree=', '--namespace=')):
                return None
            else:
                break
    return path


def _json(value, default):
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return default


def _result(content):
    result = _json(content, None)
    if isinstance(result, dict):
        code = result.get('exit_code')
        failed = bool(result.get('error')) or (code is not None and code != 0)
        return not failed, str(result.get('output') or '')
    return True, content if isinstance(content, str) else ''


def _commands(command):
    """Tokenize shell syntax as data; never evaluate, expand, or invoke it."""
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
    if not tokens or tokens[0] != 'git':
        return []
    i = 1
    while i < len(tokens):
        if tokens[i] in ('-C', '-c', '--git-dir', '--work-tree', '--namespace'):
            i += 2
        elif tokens[i].startswith(('--git-dir=', '--work-tree=', '--namespace=', '-C')):
            i += 1
        else:
            break
    return tokens[i:]


def _option(tokens, *names):
    for index, value in enumerate(tokens):
        if value in names and index + 1 < len(tokens):
            return tokens[index + 1]
        for name in names:
            if name.startswith('--') and value.startswith(name + '='):
                return value[len(name) + 1:]
    return None


def _branch(value):
    if not isinstance(value, str) or not value or value in ('HEAD', 'main', 'master'):
        return None
    if len(value) > 250 or value.startswith('-') or not re.fullmatch(r'[A-Za-z0-9_./-]+', value):
        return None
    return value


def _coding_git(git):
    return bool(git and (git[0] in ('commit', 'push', 'apply', 'am', 'cherry-pick', 'rebase', 'merge')
                or (git[0] == 'worktree' and len(git) > 1 and git[1] == 'add')
                or (git[0] in ('switch', 'checkout') and any(t in git for t in ('-b', '-B', '-c', '-C')))))


def _gh_create(tokens):
    if not tokens or tokens[0] != 'gh':
        return None
    i = 1
    while i < len(tokens) and tokens[i] in ('-R', '--repo') and i + 1 < len(tokens):
        i += 2
    if tokens[i:i + 2] != ['pr', 'create']:
        return None
    return tokens[1:i] + tokens[i + 2:]


def evidence(profile: str, session_id: str, receipt_roots=()) -> dict | None:
    """Open one profile's store read-only in a single snapshot and read a session's evidence.

    The HTTP route never uses this: it reads evidence on the same connection and snapshot it
    authorized on (:func:`evidence_from_conn`). This wrapper exists for tools and tests.
    """
    profile = profile or 'default'
    if not isinstance(profile, str) or not _PROFILE.fullmatch(profile) or profile in ('.', '..') or not session_id:
        return None
    try:
        from hermes_cli.profiles import get_profile_dir
        path = Path(get_profile_dir(profile)) / 'state.db'
        if not path.is_file():
            return None
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.5,
                                     isolation_level=None)) as db:
            db.execute('BEGIN')
            try:
                return evidence_from_conn(db, session_id, receipt_roots)
            finally:
                db.execute('ROLLBACK')
    except (sqlite3.Error, OSError, ValueError):
        return None  # No evidence that this is even a coding session.


def evidence_from_conn(db, session_id: str, receipt_roots=()) -> dict | None:
    """Return coding evidence, {} if identity is uncertain, or None for noncoding.

    *db* is the caller's read-only connection, inside the caller's read transaction, so the
    rows read here are the same snapshot the caller authorized against. Rows are isolated by
    session id. An inaccessible history is not coding; a truncated one is uncertain.
    """
    if not isinstance(session_id, str) or not session_id:
        return None
    try:
        snapshot = db.execute('SELECT cwd, git_branch, git_repo_root FROM sessions WHERE id = ?',
                              (session_id,)).fetchone()
        if snapshot is None:
            return None
        snapshot = tuple(snapshot)
        rows = [tuple(row) for row in db.execute(
            "SELECT role, substr(tool_calls,1,?), tool_call_id, CASE WHEN role='tool' THEN substr(content,1,?) END, "
            "length(tool_calls), CASE WHEN role='tool' THEN length(content) END FROM messages "
            "WHERE session_id = ? AND ((role = 'assistant' AND tool_calls IS NOT NULL) "
            "OR (role = 'tool' AND tool_name IN ('terminal','functions.terminal'))) ORDER BY id DESC LIMIT ?",
            (_MAX_FIELD, _MAX_FIELD, session_id, _MAX_ROWS + 1)).fetchall()]
    except (sqlite3.Error, ValueError):
        return None  # No evidence that this is even a coding session.

    incomplete = len(rows) > _MAX_ROWS or any((a or 0) > _MAX_FIELD or (b or 0) > _MAX_FIELD
                                              for _, _, _, _, a, b in rows)
    rows.reverse()

    pending = {}
    coding = False
    repos, branches, numbers = set(), set(), set()
    repo_contexts, branch_contexts, coding_contexts, git_contexts = [], [], [], []
    aliases = {}
    snapshot_cwd = _path(snapshot[2]) or _path(snapshot[0])
    for role, raw_calls, call_id, content, _, _ in rows:
        if role == 'assistant':
            calls = _json(raw_calls, [])
            if not isinstance(calls, list):
                continue
            for call in calls:
                if not isinstance(call, dict):
                    continue
                fn = call.get('function') or {}
                if not isinstance(fn, dict):
                    continue
                args = _json(fn.get('arguments'), {})
                if not isinstance(args, dict):
                    continue
                name = fn.get('name', '')
                if name in ('write_file', 'patch', 'functions.write_file', 'functions.patch'):
                    path_arg = args.get('path') or ''
                    if isinstance(path_arg, str) and _CODE.search(path_arg):
                        coding = True
                        coding_contexts.append(_path(path_arg, snapshot_cwd))
                    if name in ('patch', 'functions.patch'):
                        for line in str(args.get('patch') or '').splitlines():
                            for prefix in ('*** Update File: ', '*** Add File: '):
                                if line.startswith(prefix) and _CODE.search(line):
                                    coding = True
                                    coding_contexts.append(_path(line[len(prefix):], snapshot_cwd))
                if call.get('id') and name in ('terminal', 'functions.terminal'):
                    pending[call['id']] = args
        elif role == 'tool' and call_id in pending:
            args = pending.pop(call_id)
            command = args.get('command')
            if not isinstance(command, str):
                continue
            success, output = _result(content)
            commands = _commands(command)
            # A combined shell result cannot reliably be attributed to a particular
            # command unless exactly one relevant command occurs in that call.
            relevant = []
            cwd = _path(args.get('workdir')) or snapshot_cwd
            for tokens in commands:
                if tokens[0] == 'cd' and len(tokens) == 2:
                    cwd = _path(tokens[1], cwd)
                    continue
                git = _git_args(tokens)
                if git:
                    op = git[0]
                    if _coding_git(git):
                        coding = True
                        coding_contexts.append(_command_path(tokens, cwd))
                        git_contexts.append(_command_path(tokens, cwd))
                    if op in ('commit', 'worktree', 'switch', 'checkout', 'remote', 'branch'):
                        relevant.append(('git', git, _command_path(tokens, cwd)))
                elif (gh := _gh_create(tokens)) is not None:
                    coding = True
                    relevant.append(('create', gh, cwd))
            if not success or len(relevant) != 1:
                continue
            # Only a leading `cd` can be safely ignored when attributing output;
            # e.g. an echo before gh could otherwise fabricate a PR URL.
            if any(not (t[0] == 'cd' or (_git_args(t) and _git_args(t)[0] in
                        ('commit', 'worktree', 'switch', 'checkout', 'remote', 'branch'))
                        or _gh_create(t) is not None) for t in commands):
                continue
            kind, cmd, context = relevant[0]
            if kind == 'create':
                for line in output.splitlines():
                    match = _PR.fullmatch(line.strip())
                    if match:
                        repos.add(match.group(1))
                        repo_contexts.append((match.group(1), context))
                        numbers.add(int(match.group(2)))
                repo = _option(cmd, '-R', '--repo')
                if repo and _REPO.fullmatch(repo):
                    repos.add(repo)
                    repo_contexts.append((repo, context))
                branch = _branch(_option(cmd, '-H', '--head'))
                if branch:
                    branches.add(branch)
                    branch_contexts.append((branch, context))
            elif cmd[0] == 'worktree' and len(cmd) > 1 and cmd[1] == 'add':
                branch = _option(cmd[2:], '-b', '-B')
                if not branch:
                    match = re.search(r"Preparing worktree \(checking out '([^']+)'\)", output)
                    branch = match.group(1) if match else None
                if not branch:
                    positional = [v for v in cmd[2:] if not v.startswith('-')]
                    if len(positional) == 2:
                        branch = positional[1]
                if branch := _branch(branch):
                    branches.add(branch)
                    branch_contexts.append((branch, context))
                rest = cmd[2:]
                positional = []
                i = 0
                while i < len(rest):
                    if rest[i] in ('-b', '-B', '--orphan'):
                        i += 2
                    elif rest[i].startswith('-'):
                        i += 1
                    else:
                        positional.append(rest[i])
                        i += 1
                if positional and context:
                    target = _path(positional[0], cwd)
                    if target and target != context:
                        aliases[target] = context
            elif cmd[0] in ('switch', 'checkout'):
                branch = _branch(_option(cmd[1:], '-b', '-B', '-c', '-C'))
                if not branch:
                    match = re.search(r"(?:Switched to (?:a new )?branch|Already on) '([^']+)'", output)
                    branch = _branch(match.group(1)) if match else None
                if branch:
                    branches.add(branch)
                    branch_contexts.append((branch, context))
            elif cmd[0] == 'commit':
                match = re.search(r'^\[([^\s\]]+) [0-9a-fA-F]{4,}\]', output, re.M)
                if match and (branch := _branch(match.group(1))):
                    branches.add(branch)
                    branch_contexts.append((branch, context))
            elif cmd[:3] == ['remote', 'get-url', 'origin']:
                match = _REMOTE.fullmatch(output.strip())
                if match:
                    repos.add(match.group(1))
                    repo_contexts.append((match.group(1), context))
    # A running terminal tool has no result yet; it establishes coding intent,
    # but never an identity or a successful PR.
    for args in pending.values():
        command = args.get('command')
        if isinstance(command, str):
            for tokens in _commands(command):
                if _coding_git(_git_args(tokens)) or _gh_create(tokens) is not None:
                    coding = True
    if not coding:
        return None
    if incomplete:
        return {}  # Coding was seen, but conflicting identities may be outside the window.
    if branch := _branch(snapshot[1]):
        branches.add(branch)
        branch_contexts.append((branch, snapshot_cwd))
    # No repository is inferred from a recorded path: that would mean opening a checkout the
    # history names (its .git, config or commondir). A repository comes only from recorded
    # command output (a PR URL, `remote get-url origin`) or an explicit `gh -R`.
    private_receipt = False
    if len(branches) == 1 and branch_contexts:
        contexts = [context for _, context in branch_contexts] + coding_contexts
        for repo, number in _local_receipts(tuple(receipt_roots or ()), contexts, next(iter(branches))):
            private_receipt = True
            repos.add(repo)
            numbers.add(number)
            repo_contexts.append((repo, branch_contexts[0][1]))
    if len(repos) > 1 or len(branches) > 1 or len(numbers) > 1:
        return {}

    def related(a, b):
        # Text only: the same place, one inside the other, or inside a worktree this session added.
        if a is None or b is None:
            return a == b
        a, b = _canonical(a, aliases), _canonical(b, aliases)
        return _within(a, b) or _within(b, a)
    if repos and branches:
        if not all(any(related(bctx, rctx) for _, rctx in repo_contexts)
                   for _, bctx in branch_contexts):
            return {}
    if branches:
        # Git work (a commit, a new branch, a worktree) somewhere the branch was not is a
        # second checkout; which one a PR belongs to is unknown. A file written elsewhere
        # (a scratch script) is not.
        if any(context and not any(related(context, bctx) for _, bctx in branch_contexts)
               for context in git_contexts):
            return {}
    result = {}
    if repos:
        result['repo'] = next(iter(repos))
    # The private source branch is not the public PR head (which is sync/...).
    # Do not hand it to the GitHub resolver for comparison or a head search.
    if branches and not private_receipt:
        result['branch'] = next(iter(branches))
    if numbers:
        result['number'] = next(iter(numbers))
    return result
