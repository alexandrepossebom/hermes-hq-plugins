import json
import subprocess

import session_pr_evidence as subject
import session_pr_status as status
from test_session_pr_evidence import history, snapshot


def test_opt_in_receipt_and_conflicts(tmp_path, history, monkeypatch):
    repo = tmp_path / 'private'
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    history('one', 'x', name='write_file', args={'path': str(repo / 'src' / 'app.py')})
    snapshot(tmp_path, 'one', 'x', repo, 'feature/example')
    receipt = repo / '.git' / 'hq-public-pr-receipts.jsonl'
    entry = {'version': 1, 'branch': 'feature/example',
             'url': 'https://github.com/mrcharlesiv/hermes-hq-app/pull/42'}
    receipt.write_text(json.dumps(entry) + '\n')
    roots = [str(repo)]
    assert subject.evidence('one', 'x', roots) == {'branch': 'feature/example'}
    monkeypatch.setenv('HERMES_HQ_PUBLIC_PR_RECEIPTS', '1')
    # A checkout the owner did not configure is never read, even when the history points at it.
    assert subject.evidence('one', 'x') == {'branch': 'feature/example'}
    evidence = subject.evidence('one', 'x', roots)
    assert evidence == {'repo': 'mrcharlesiv/hermes-hq-app', 'number': 42}
    seen = []
    def api(endpoint, access):
        seen.append((endpoint, access.token))
        return {'number': 42, 'base': {'repo': {'full_name': 'mrcharlesiv/hermes-hq-app'}},
                'head': {'ref': 'sync/public-export'}, 'state': 'closed', 'draft': False,
                'merged_at': '2025-01-01'}
    monkeypatch.setattr(status, '_api', api)
    status._cache.clear()
    assert status._resolve_evidence(evidence)['state'] == 'unknown'   # not declared public: not asked
    assert seen == []
    public = status.Access(public_repos={'mrcharlesiv/hermes-hq-app'})
    assert status._resolve_evidence(evidence, public)['state'] == 'merged'
    assert seen == [('/repos/mrcharlesiv/hermes-hq-app/pulls/42', None)]
    status._cache.clear()
    receipt.write_text(json.dumps(entry) + '\n' + json.dumps({**entry, 'url': entry['url'][:-2] + '43'}) + '\n')
    assert subject.evidence('one', 'x', roots) == {}
    receipt.write_text(json.dumps(entry) + '\n' + json.dumps({**entry, 'branch': 'feature/other'}) + '\n')
    assert subject.evidence('one', 'x', roots) == {'branch': 'feature/example'}
    receipt.write_text(json.dumps({**entry, 'url': 'https://github.com/example/other/pull/42'}) + '\n')
    assert subject.evidence('one', 'x', roots) == {'branch': 'feature/example'}
    receipt.write_text('{bad json\n')
    assert subject.evidence('one', 'x', roots) == {'branch': 'feature/example'}
    receipt.unlink()
    receipt.symlink_to(tmp_path / 'elsewhere.jsonl')
    (tmp_path / 'elsewhere.jsonl').write_text(json.dumps(entry) + '\n')
    assert subject.evidence('one', 'x', roots) == {'branch': 'feature/example'}   # never followed


def test_receipt_does_not_join_unrelated_private_contexts(tmp_path, history, monkeypatch):
    a, b = tmp_path / 'a', tmp_path / 'b'
    for path in (a, b): subprocess.run(['git', 'init', '-q', str(path)], check=True)
    monkeypatch.setenv('HERMES_HQ_PUBLIC_PR_RECEIPTS', '1')
    (a / '.git' / 'hq-public-pr-receipts.jsonl').write_text(json.dumps({
        'version': 1, 'branch': 'feature/a', 'url': 'https://github.com/mrcharlesiv/hermes-hq-app/pull/42'}) + '\n')
    history('one', 'x', name='write_file', args={'path': str(b / 'app.py')})
    snapshot(tmp_path, 'one', 'x', a, 'feature/a')
    assert subject.evidence('one', 'x', [str(a), str(b)]) == {'branch': 'feature/a'}
    assert subject.evidence('one', 'x', [str(a)]) == {'branch': 'feature/a'}


def test_receipt_shared_across_linked_worktrees(tmp_path, history, monkeypatch):
    repo, worktree = tmp_path / 'private', tmp_path / 'worktree'
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Example', '-c', 'user.email=e@example.invalid',
                    'commit', '--allow-empty', '-qm', 'initial'], check=True)
    subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '-qb', 'feature/a', str(worktree)], check=True)
    (repo / '.git' / 'hq-public-pr-receipts.jsonl').write_text(json.dumps({
        'version': 1, 'branch': 'feature/a', 'url': 'https://github.com/mrcharlesiv/hermes-hq-app/pull/42'}) + '\n')
    monkeypatch.setenv('HERMES_HQ_PUBLIC_PR_RECEIPTS', '1')
    history('one', 'x', name='write_file', args={'path': str(worktree / 'app.py')})
    snapshot(tmp_path, 'one', 'x', worktree, 'feature/a')
    assert subject.evidence('one', 'x') == {'branch': 'feature/a'}
    assert subject.evidence('one', 'x', [str(repo)]) == {'repo': 'mrcharlesiv/hermes-hq-app', 'number': 42}
