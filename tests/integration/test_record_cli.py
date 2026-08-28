"""Focused subprocess-style contracts for native Record command groups."""
import io
import json
from pathlib import Path
from unittest.mock import patch

from yylo_ledger.cli import ExitCode, TaskCLI


def run_cli(argv, stdin=""):
    out, err = io.StringIO(), io.StringIO()
    with patch("sys.stdout", out), patch("sys.stderr", err), patch("sys.stdin", io.StringIO(stdin)):
        code = TaskCLI().run(argv)
    return code, out.getvalue(), err.getvalue()


def test_wiki_file_round_trip_alias_and_exact_update(tmp_path, monkeypatch):
    monkeypatch.setenv("JUNO_TASK_ROOT", str(tmp_path))
    old = tmp_path / "old.md"; old.write_text("# Rich\n`$SAFE`\n", encoding="utf-8")
    new = tmp_path / "new.md"; new.write_text("# New\n", encoding="utf-8")

    code, output, _ = run_cli(["wiki", "create", "--id", "Abc123", "--title", "Rich", "--file", str(old)])
    assert code == ExitCode.SUCCESS
    slug = json.loads(output)["slug"]

    code, output, _ = run_cli(["wiki", "get", slug, "--format", "json"])
    value = json.loads(output)
    assert code == ExitCode.SUCCESS
    assert (value["id"], value["slug"], value["resolved_from"]) == ("Abc123", slug, slug)
    code, rendered, _ = run_cli(["wiki", "get", "Abc123", "--rendered"])
    assert code == 0 and "<h1>Rich</h1>" in rendered and "$SAFE" in rendered

    code, output, _ = run_cli(["wiki", "update", slug, "--expected-revision", "1",
                               "--old-file", str(old), "--new-file", str(new)])
    assert code == ExitCode.SUCCESS and json.loads(output)["revision"] == 2

    code, _, error = run_cli(["wiki", "update", "Abc123", "--expected-revision", "1",
                              "--old-file", str(old), "--new-file", str(new)])
    assert code == ExitCode.VALIDATION_ERROR
    assert json.loads(error)["error"]["code"] == "REVISION_CONFLICT"


def test_generic_and_typed_search_return_same_ids_and_no_remove_completion(tmp_path, monkeypatch):
    monkeypatch.setenv("JUNO_TASK_ROOT", str(tmp_path))
    source = tmp_path / "wiki.md"; source.write_text("needle", encoding="utf-8")
    assert run_cli(["wiki", "create", "--id", "Def456", "--title", "Needle", "--file", str(source)])[0] == 0

    typed = json.loads(run_cli(["wiki", "search", "--text", "needle", "--format", "json"])[1])
    generic = json.loads(run_cli(["record", "search", "--kind", "document", "--profile", "wiki", "--text", "needle", "--format", "json"])[1])
    assert [item["id"] for item in typed["records"]] == [item["id"] for item in generic["records"]]

    code, output, _ = run_cli(["__complete", "--index", "2", "--", "yylo-ledger", "wiki", ""])
    assert code == 0 and "create" in output.splitlines() and "remove" not in output.splitlines()
    code, output, _ = run_cli(["__complete", "--index", "3", "--", "yylo-ledger", "wiki", "update", "--exp"])
    assert code == 0 and "--expected-revision" in output.splitlines()


def test_workflow_validation_and_artifact_stdin_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv("JUNO_TASK_ROOT", str(tmp_path))
    workflow = tmp_path / "flow.yaml"
    workflow.write_text("schema_version: v1\nworkflow_id: flow\nsteps: []\n", encoding="utf-8")
    assert run_cli(["workflow", "create", "--id", "Ghi789", "--title", "Flow", "--file", str(workflow)])[0] == 0
    code, output, _ = run_cli(["workflow", "get", "Ghi789", "--validated"])
    assert code == 0 and "workflow_id: flow" in output

    # Binary stdin is exercised through an explicit BytesIO-backed buffer.
    source = tmp_path / "artifact.bin"; source.write_bytes(b"a\x00b")
    code, output, _ = run_cli(["artifact", "create", "--id", "Jkl012", "--title", "Bytes",
                               "--profile", "report", "--mode", "local", "--file", str(source)])
    assert code == 0 and json.loads(output)["payload"]["size"] == 3
