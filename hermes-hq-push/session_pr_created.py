"""The pull requests one session opened, read from its stored history. Nothing is run.

A pull request belongs to a session only when the session's own tool call opened it and the paired
result printed the link:

* ``gh pr create``: the result of that very call holds a line that is only the pull request's link.
* a publisher script (``public-prs.py``, not ``--dry-run``): once the session has run it, a result
  line in the script's own shape, ``<8 hex>  <link>  <title>``, counts. The script is often run in
  the background, so the line may arrive in a later result or in the completion notice.

A link the bot merely read, reviewed, listed or wrote in a message is never a pull request of the
session. History is data: no command is evaluated and no path in it is opened.
"""
import json
import re
import shlex
import sqlite3

_PR = re.compile(r'https://github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_.-]{1,100})/pull/([1-9][0-9]{0,8})/?')
_PUBLISHED = re.compile(r'\s*[0-9a-f]{8}\s+' + _PR.pattern + r'\s+\S.*')
_ENV = re.compile(r'[A-Za-z_][A-Za-z0-9_]*=')
_TERMINAL = ('terminal', 'functions.terminal')
_NOTICE = '[IMPORTANT: Background process'
_MAX_FIELD = 400_000
_MAX_ROWS = 2000
MAX_CREATED = 6
_MAX_DEPTH = 3
_MAX_FAMILY = 200


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


def _opens(tokens):
    """``gh [-R repo] pr create …`` (after any ``NAME=value`` words)."""
    i = 0
    while i < len(tokens) and _ENV.match(tokens[i]):
        i += 1
    if tokens[i:i + 1] != ['gh']:
        return False
    i += 1
    while i + 1 < len(tokens) and tokens[i] in ('-R', '--repo'):
        i += 2
    return tokens[i:i + 2] == ['pr', 'create'] and '--help' not in tokens and '--web' not in tokens


def _publishes(tokens):
    return any(t.endswith('public-prs.py') for t in tokens[:4]) and '--dry-run' not in tokens \
        and tokens[0] not in ('cat', 'sed', 'grep', 'rg', 'head', 'tail', 'less', 'wc', 'ls', 'git', 'echo')


def _texts(content):
    """Every string a stored tool result carries (its output, a waited process's log)."""
    try:
        value = json.loads(content)
    except (ValueError, TypeError):
        return [content] if isinstance(content, str) else []
    out, stack = [], [value]
    while stack and len(out) < 64:
        item = stack.pop()
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return out


def _failed(content):
    try:
        value = json.loads(content)
    except (ValueError, TypeError):
        return False
    if not isinstance(value, dict):
        return False
    code = value.get('exit_code')
    return bool(value.get('error')) or (code is not None and code != 0)


def _family(db, session_id):
    """The session and the subagents it started (theirs is its work), nearest first."""
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


def created_from_conn(db, session_id):
    """``[(repo, number), …]`` in the order the session (or a subagent it started) opened them;
    ``[]`` for none; ``None`` when the session isn't in this store or its history can't be read
    (never "no pull request")."""
    if not isinstance(session_id, str) or not session_id:
        return None
    try:
        if db.execute('SELECT 1 FROM sessions WHERE id = ?', (session_id,)).fetchone() is None:
            return None
        family = _family(db, session_id)
        if len(family) > _MAX_FAMILY:
            return None
        seen = []
        for member in family:
            part = _scan(db, member)
            if part is None:
                return None
            seen.extend(part)
    except (sqlite3.Error, ValueError):
        return None
    found = []
    for _, pair in sorted(seen):
        if pair in found:
            found.remove(pair)   # opened again or reported again: it is the newer one
        found.append(pair)
    return found[-MAX_CREATED:]


def _scan(db, session_id):
    """``[(message row id, (repo, number)), …]`` for one stored session; ``None`` if too long to judge."""
    rows = db.execute(
        "SELECT id, role, tool_calls, tool_call_id, substr(content, 1, ?) FROM messages "
        "WHERE session_id = ? AND ("
        " (role = 'assistant' AND tool_calls IS NOT NULL AND (tool_calls LIKE '%public-prs.py%'"
        "   OR (tool_calls LIKE '%gh %' AND tool_calls LIKE '%pr%create%')))"
        " OR (role IN ('tool', 'user') AND content LIKE '%github.com/%/pull/%')) ORDER BY id LIMIT ?",
        (_MAX_FIELD, session_id, _MAX_ROWS + 1)).fetchall()
    if len(rows) > _MAX_ROWS:
        return None
    opened, published, found = set(), False, []
    for row_id, role, raw_calls, call_id, content in rows:
        if role == 'assistant':
            try:
                calls = json.loads(raw_calls)
            except (ValueError, TypeError):
                continue
            for call in calls if isinstance(calls, list) else []:
                fn = call.get('function') if isinstance(call, dict) else None
                if not isinstance(fn, dict) or fn.get('name') not in _TERMINAL:
                    continue
                try:
                    args = json.loads(fn.get('arguments') or '{}')
                except (ValueError, TypeError):
                    continue
                command = args.get('command') if isinstance(args, dict) else None
                if not isinstance(command, str):
                    continue
                for tokens in _segments(command):
                    if _opens(tokens) and call.get('id'):
                        opened.add(call['id'])
                    elif _publishes(tokens):
                        published = True
            continue
        if not isinstance(content, str):
            continue
        if role == 'user':
            if not (published and content.startswith(_NOTICE)):
                continue
            texts = [content]
        else:
            texts = _texts(content)
            if call_id in opened and not _failed(content):
                for text in texts:
                    for line in text.splitlines():
                        match = _PR.fullmatch(line.strip())
                        if match:
                            found.append((row_id, (match.group(1), int(match.group(2)))))
        if published:
            for text in texts:
                for line in text.splitlines():
                    match = _PUBLISHED.fullmatch(line)
                    if match:
                        found.append((row_id, (match.group(1), int(match.group(2)))))
    return found
