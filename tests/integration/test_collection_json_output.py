"""Legacy collection framing is identical through direct CLI and canonical wrapper."""
import json
from pathlib import Path

import pytest

from yylo_ledger.cli import TaskCLI, ExitCode
from yylo_ledger.config import Config
from yylo_ledger.storage import TaskStorage
from .test_empty_search_output import collection, command, run  # shared real-CLI fixtures


@pytest.mark.parametrize('operation', ['list', 'search', 'ready', 'order'])
@pytest.mark.parametrize('style', [[], ['--raw'], ['--pretty']])
@pytest.mark.parametrize('position', ['before', 'after'])
def test_single_json_document(command, collection, operation, style, position):
    _, config, _, _ = collection
    storage = TaskStorage(Config(config_path=str(config)))
    storage.create_task(body='second task with unicode café', status='todo')
    flags = ['-f', 'json', *style]
    args = flags + [operation] if position == 'before' else [operation] + flags
    result = run(command, collection, args)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)  # rejects any trailing top-level document
    assert len(value['tasks']) == 2
    assert value['summary']['total_tasks'] == 2
    assert value['summary']['displayed_tasks'] == 2
    assert value['summary']['status_counts']['todo'] == 2
    assert result.stderr == ''
    if style == ['--raw']:
        assert len(result.stdout.splitlines()) == 1
    elif style == ['--pretty']:
        assert '\n  "tasks":' in result.stdout


@pytest.mark.parametrize('operation', ['list', 'search', 'ready'])
@pytest.mark.parametrize('offset,displayed', [(0, 1), (1, 1), (2, 0)])
def test_paginated_json(command, collection, operation, offset, displayed):
    storage = TaskStorage(Config(config_path=str(collection[1])))
    storage.create_task(body='second', status='todo')
    result = run(command, collection, [operation, '-f', 'json', '--raw',
                 '--limit', '1', '--offset', str(offset), '--show-cursor'])
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert len(value['tasks']) == displayed
    assert value['summary']['displayed_tasks'] == displayed
    assert value['summary']['total_tasks'] == 2
    assert bool(value['summary']['next_cursor']) == (offset == 0)


@pytest.mark.parametrize('operation', ['list', 'search', 'ready', 'order'])
def test_empty_order_and_collections(command, collection, operation):
    storage = TaskStorage(Config(config_path=str(collection[1])))
    storage.update_task(collection[3].id, {'status': 'done'})
    args = [operation, '-f', 'json', '--raw']
    if operation != 'order':
        args += ['--status', 'todo']
    result = run(command, collection, args)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['tasks'] == []
    assert json.loads(result.stdout)['summary']['total_tasks'] == 0


@pytest.mark.parametrize('flags', [['-f', 'xml', '--raw'], ['-f', 'table', '--raw'],
                                  ['-f', 'json', '--pretty', '--raw']])
def test_unsupported_before_initialization(monkeypatch, capsys, flags):
    cli = TaskCLI()
    monkeypatch.setattr(cli, '_init_components', lambda *_: pytest.fail('storage initialized'))
    assert cli.run(flags + ['list']) == ExitCode.INVALID_USAGE
    out = capsys.readouterr()
    assert out.out == ''
    assert '--raw' in out.err and '--help' in out.err


@pytest.mark.parametrize('operation', ['list', 'search', 'ready', 'order'])
def test_ndjson_and_help_contract(command, collection, operation):
    result = run(command, collection, [operation, '-f', 'ndjson', '--raw'])
    assert result.returncode == 0, result.stderr
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(rows) == 1 and rows[0]['id'] == collection[3].id
    assert all('summary' not in row for row in rows)
    assert ('SUMMARY:' in result.stderr) == (operation != 'order')
    help_result = run(command, collection, [operation, '--help'])
    assert help_result.returncode == 0
    assert '--raw' in help_result.stdout
    assert 'tasks and summary' in help_result.stdout
    assert 'not an NDJSON record' in ' '.join(help_result.stdout.split())


@pytest.mark.parametrize('flags', [['-f', 'xml', '--raw'], ['-f', 'table', '--raw'],
                                  ['-f', 'json', '--pretty', '--raw']])
def test_unsupported_public_invocation(command, collection, flags):
    result = run(command, collection, ['list', *flags])
    assert result.returncode != 0
    assert result.stdout == ''
    assert '--raw' in result.stderr and '--help' in result.stderr


@pytest.mark.parametrize('flags', [[], ['--raw'], ['--format', 'json', '--raw']])
def test_default_and_long_format(command, collection, flags):
    result = run(command, collection, [*flags, 'list'])
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['tasks'][0]['id'] == collection[3].id
    assert result.stderr == ''
    assert (len(result.stdout.splitlines()) == 1) == ('--raw' in flags)


@pytest.mark.parametrize('count', [1, 2])
def test_exact_task_get_remains_array_with_matching_help(command, collection, count):
    storage = TaskStorage(Config(config_path=str(collection[1])))
    ids = [collection[3].id]
    if count == 2:
        ids.append(storage.create_task(body='second', status='todo').id)
    result = run(command, collection, ['-f', 'json', 'get', *ids, '--compact'])
    assert result.returncode == 0, result.stderr
    assert [task['id'] for task in json.loads(result.stdout)] == ids
    help_result = run(command, collection, ['get', '--help'])
    assert 'array, including a single result' in ' '.join(help_result.stdout.split())


@pytest.mark.parametrize('count', [0, 1, 3])
@pytest.mark.parametrize('projection', ['summary', 'metadata'])
def test_real_artifact_json_through_native_and_wrapper(command, collection, count, projection):
    from yylo_ledger.artifacts import ArtifactStore
    store = ArtifactStore(collection[0] / '.juno_task')
    ids = []
    for i in range(count):
        item = store.create(record_id=f'artifact_Fix{i:03d}', title=f'Report {i}', profile='report', mode='inline',
                            content=b'fixture evidence', media_type='text/plain')
        ids.append(item['id'])
    seen, cursor = [], None
    while True:
        args = ['artifact', 'search', '--profile', 'report', '--projection', projection,
                '--limit', '1', '-f', 'json']
        if cursor:
            args += ['--cursor', cursor]
        result = run(command, collection, args)
        assert result.returncode == 0, result.stderr
        value = json.loads(result.stdout)
        seen.extend(record['id'] for record in value['records'])
        cursor = value['next_cursor']
        if cursor is None:
            break
    assert sorted(seen) == sorted(ids)
    if ids:
        result = run(command, collection, ['artifact', 'get', ids[0], '-f', 'json'])
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)['id'] == ids[0]
    help_result = run(command, collection, ['artifact', 'search', '--help'])
    assert 'records and pagination metadata' in ' '.join(help_result.stdout.split())
    failure = run(command, collection, ['artifact', 'search', '--cursor', 'invalid-cursor', '-f', 'json'])
    assert failure.returncode != 0
    assert failure.stdout == '' and failure.stderr


def test_independent_skill_documents_same_contract():
    skill = Path(__file__).resolve().parents[3] / 'yylo-skills/skills/ledger-tasks-yylo/SKILL.md'
    if not skill.is_file():
        pytest.skip('independent skill source requires monorepo')
    text = skill.read_text()
    assert 'one object containing `tasks` and' in text
    assert 'no summary record' in text
    assert '`--raw` accepts JSON/NDJSON only' in text
