"""CLI contracts for history, fields, privacy, cache, and cursor pagination."""
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy

from kanban.cli import ExitCode, TaskCLI
from kanban.config import Config


def run(config, args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = TaskCLI().run(["-c", str(config), *args])
    return code, out.getvalue(), err.getvalue()


def test_git_native_cli_surface(tmp_path, monkeypatch):
    monkeypatch.delenv("JUNO_TASK_ROOT", raising=False)
    tasks = tmp_path / ".juno_task/tasks"
    tasks.mkdir(parents=True)
    cfg = deepcopy(Config.DEFAULT_CONFIG)
    cfg["storage"]["base_path"] = str(tasks)
    cfg["custom_fields"] = {"due_date": {"type": "date"}}
    config = tasks / "config.json"
    config.write_text(json.dumps(cfg), encoding="utf-8")

    code, out, _ = run(config, ["create", "safe contact@example.com token=secret", "--status", "todo",
                                "--field", "due_date=2026-07-01"])
    assert code == ExitCode.SUCCESS
    task_id = json.loads(out)[0]["id"]
    code, out, _ = run(config, ["search", "--overdue", "--limit", "1", "--format", "json"])
    assert code == 0 and "contact@example.com" not in out and "secret" not in out
    assert "[REDACTED_EMAIL]" in out and "[REDACTED_CREDENTIAL]" in out

    assert run(config, ["-f", "json", "history", task_id])[0] == 0
    assert run(config, ["doctor"])[0] == 0
    assert run(config, ["cache", "rebuild"])[0] == 0
    assert run(config, ["reconcile", "--check"])[0] == 0

    run(config, ["create", "second", "--status", "todo"])
    code, without_cursor, _ = run(config, ["list", "--limit", "1", "--format", "json"])
    assert "next_cursor" not in json.loads(without_cursor)["summary"]
    code, out, _ = run(config, ["list", "--limit", "1", "--show-cursor", "--format", "json"])
    document = json.loads(out)
    cursor = document["summary"]["next_cursor"]
    assert cursor
    code, page, _ = run(config, ["list", "--limit", "1", "--cursor", cursor, "--format", "json"])
    assert code == 0 and json.loads(page)["tasks"][0]["id"] != document["tasks"][0]["id"]

    code, _, err = run(config, ["list", "--limit", "1", "--sort", "asc", "--cursor", cursor, "--format", "json"])
    assert code == ExitCode.INVALID_USAGE
    assert "does not belong to this collection query" in err

    # Every broad collection renderer, including dependency order, applies the
    # same projection/redaction boundary before bytes reach stdout.
    code, ordered, _ = run(config, ["order", "--format", "json"])
    assert code == 0 and "contact@example.com" not in ordered and "secret" not in ordered

    # Dangerous long options are exact: argparse prefix matching is disabled.
    code, _, err = run(config, ["update", task_id, "--expected-rev", "bogus", "--status", "done"])
    assert code == ExitCode.INVALID_USAGE
    assert "unrecognized arguments" in err
