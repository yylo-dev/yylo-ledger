"""PDR revision and task-adoption contracts through both public APIs."""
import io
import json
from unittest.mock import patch

import pytest

from yylo_ledger.cli import TaskCLI
from yylo_ledger.records import payload_digest


def cli(*args):
    args = list(args)
    if args[0] in ('create', 'update') and '-f' in args:
        index = args.index('-f')
        args = args[index:index + 2] + args[:index] + args[index + 2:]
    out, err = io.StringIO(), io.StringIO()
    with patch('sys.stdout', out), patch('sys.stderr', err):
        code = TaskCLI().run(list(args))
    return code, out.getvalue(), err.getvalue()


def ok(*args):
    code, out, err = cli(*args)
    assert code == 0, (out, err)
    return json.loads(out)


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv('JUNO_TASK_ROOT', str(tmp_path))
    old = tmp_path / 'old.md'; old.write_text('# Approved\n')
    new = tmp_path / 'new.md'; new.write_text('# Revised\n')
    ok('pdr', 'create', '--id', 'Pdr123', '--title', 'Requirements', '--file', str(old))
    return tmp_path, old, new


def binding(revision=1, text='# Approved\n'):
    return {'record_id': 'Pdr123', 'revision': revision, 'payload_sha256': payload_digest(text)}


def task_with_binding(value):
    rows = ok('create', '--body', 'Implement requirements', '--field',
              'pdr_binding=' + json.dumps(value), '-f', 'json')
    return rows[0]['id']


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for directory in ('tasks', 'ledger', 'documents', 'document-ledger')
            for p in (root / '.juno_task' / directory).rglob('*') if p.is_file()}


def test_revision_roundtrip_history_search_and_archive(project):
    root, old, new = project
    assert cli('pdr', 'get', 'Pdr123', '--source')[1] == old.read_text()
    assert '<h1>Approved</h1>' in cli('pdr', 'get', 'Pdr123', '--rendered')[1]
    result = ok('pdr', 'update', 'Pdr123', '--expected-revision', '1', '--old-file', str(old), '--new-file', str(new))
    assert result['revision'] == 2
    assert cli('pdr', 'get', 'Pdr123', '--revision', '1', '--source')[1] == old.read_text()
    before = snapshot(root)
    assert cli('pdr', 'update', 'Pdr123', '--expected-revision', '1', '--old-file', str(old), '--new-file', str(new))[0] != 0
    assert snapshot(root) == before
    assert len(ok('pdr', 'history', 'Pdr123', '-f', 'json')) == 2
    typed = ok('pdr', 'search', '--text', 'Requirements', '-f', 'json')['records']
    common = ok('record', 'search', '--kind', 'document', '--profile', 'pdr', '-f', 'json')['records']
    assert [r['id'] for r in typed] == [r['id'] for r in common] == ['Pdr123']
    ok('pdr', 'archive', 'Pdr123', '--expected-revision', '2')
    assert ok('pdr', 'get', 'Pdr123')['lifecycle'] == 'archived'


def test_explicit_native_adoption_preserves_previous_approval(project):
    root, old, new = project
    task = task_with_binding(binding())
    ok('pdr', 'update', 'Pdr123', '--expected-revision', '1', '--old-file', str(old), '--new-file', str(new))
    assert ok('task', 'get', task)['fields']['pdr_binding'] == binding()
    before = root / 'before.json'; before.write_text(json.dumps({'pdr_binding': binding()}))
    after = root / 'after.json'; after.write_text(json.dumps({'pdr_binding': binding(2, new.read_text())}))
    ok('task', 'update', task, '--expected-revision', '1', '--path', '/fields',
       '--expect-file', str(before), '--value-file', str(after))
    assert ok('task', 'get', task)['fields']['pdr_binding']['revision'] == 2
    history = [json.loads(line) for line in cli('task', 'history', task)[1].splitlines()]
    assert history[-1]['operation'] == 'exact-replace'
    assert '/fields' in history[-1]['changed_paths']
    frozen = snapshot(root)
    assert cli('task', 'update', task, '--expected-revision', '1', '--path', '/fields',
               '--expect-file', str(before), '--value-file', str(after))[0] != 0
    assert snapshot(root) == frozen


@pytest.mark.parametrize('bad', [None, {}, {'record_id': 'Pdr123', 'revision': True, 'payload_sha256': 'x'},
    {**binding(), 'record_id': 'Xxx999'}, {**binding(), 'revision': 99},
    {**binding(), 'payload_sha256': '0' * 64}, {**binding(), 'extra': 'no'}])
def test_invalid_compatibility_adoptions_write_nothing(project, bad):
    root, _, _ = project
    task = task_with_binding(binding())
    frozen = snapshot(root)
    assert cli('update', task, '--field', 'pdr_binding=' + json.dumps(bad))[0] != 0
    assert snapshot(root) == frozen
    assert cli('create', '--body', 'Invalid pin', '--field', 'pdr_binding=' + json.dumps(bad))[0] != 0
    assert snapshot(root) == frozen


def test_wrong_profile_and_native_invalid_pin_fail_without_writes(project):
    root, old, _ = project
    wiki = ok('wiki', 'create', '--id', 'Wik123', '--title', 'Not requirements', '--file', str(old))
    bad = {**binding(), 'record_id': wiki['id']}
    task = ok('task', 'create', 'Implement')['id']
    before = root / 'before.json'; before.write_text('{}')
    after = root / 'after.json'; after.write_text(json.dumps({'pdr_binding': bad}))
    frozen = snapshot(root)
    assert cli('task', 'update', task, '--expected-revision', '1', '--path', '/fields',
               '--expect-file', str(before), '--value-file', str(after))[0] != 0
    assert cli('create', '--body', 'Invalid', '--field', 'pdr_binding=' + json.dumps(bad))[0] != 0
    assert snapshot(root) == frozen


def test_compatibility_adoption_is_explicit_and_status_preserves_pin(project):
    _, old, new = project
    task = task_with_binding(binding())
    ok('pdr', 'update', 'Pdr123', '--expected-revision', '1', '--old-file', str(old), '--new-file', str(new))
    adopted = binding(2, new.read_text())
    ok('update', task, '--field', 'pdr_binding=' + json.dumps(adopted), '-f', 'json')
    ok('update', task, '--status', 'in_progress', '-f', 'json')
    assert ok('task', 'get', task)['fields']['pdr_binding'] == adopted
