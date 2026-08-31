"""End-to-end CLI contract for plan-bound, one-item Record migration."""
import io
import json
from unittest.mock import patch

from yylo_ledger.cli import TaskCLI


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with patch("sys.stdout", out), patch("sys.stderr", err):
        code = TaskCLI().run(argv)
    return code, out.getvalue(), err.getvalue()


def test_cli_inventory_plan_apply_status_verify_and_retry(tmp_path, monkeypatch):
    controller = tmp_path / "controller"
    source = tmp_path / "legacy"
    receipts = tmp_path / "receipts"
    controller.mkdir(); source.mkdir(); receipts.mkdir()
    (source / "wiki").mkdir()
    page = source / "wiki" / "guide.md"
    page.write_text("# Guide\n\nCLI migration needle\n", encoding="utf-8")
    report = source / "report.json"
    report.write_text('{"result":"ok"}\n', encoding="utf-8")
    declarations = receipts / "declarations.json"
    declarations.write_text(json.dumps([
        {"kind": "artifact", "path": "report.json", "profile": "receipt", "mode": "local",
         "media_type": "application/json"}
    ]), encoding="utf-8")
    inventory = receipts / "inventory.json"
    plan = receipts / "plan.json"
    status = receipts / "status.json"
    monkeypatch.setenv("JUNO_TASK_ROOT", str(controller))

    code, output, error = run_cli([
        "migration", "inventory", "--source-root", str(source), "--wiki-root", "wiki",
        "--declarations", str(declarations), "--output", str(inventory)])
    assert code == 0, error
    assert json.loads(output)["total"] == 2
    code, output, error = run_cli([
        "migration", "plan", "--source-root", str(source), "--inventory", str(inventory),
        "--output", str(plan)])
    assert code == 0, error
    planned = json.loads(plan.read_text())
    ids = [item["record_id"] for item in planned["items"]]

    for record_id in ids:
        code, _, error = run_cli([
            "migration", "apply", "--source-root", str(source), "--plan", str(plan),
            "--status-file", str(status), "--id", record_id])
        assert code == 0, error
    code, output, error = run_cli([
        "migration", "status", "--source-root", str(source), "--plan", str(plan),
        "--status-file", str(status)])
    assert code == 0, error
    assert json.loads(output)["summary"]["states"] == {"verified": 2}
    code, output, error = run_cli([
        "migration", "verify", "--source-root", str(source), "--plan", str(plan),
        "--status-file", str(status)])
    assert code == 0, error
    assert json.loads(output)["ok"] is True
    wiki_id = next(item["record_id"] for item in planned["items"] if item["kind"] == "document")
    artifact_id = next(item["record_id"] for item in planned["items"] if item["kind"] == "artifact")
    wiki_search = json.loads(run_cli(["wiki", "search", "--id", wiki_id, "--format", "json"])[1])
    artifact_search = json.loads(run_cli(["artifact", "search", "--id", artifact_id, "--format", "json"])[1])
    assert [item["id"] for item in wiki_search["records"]] == [wiki_id]
    assert [item["id"] for item in artifact_search["records"]] == [artifact_id]

    code, output, error = run_cli([
        "migration", "apply", "--source-root", str(source), "--plan", str(plan),
        "--status-file", str(status), "--id", ids[0]])
    assert code == 0, error
    assert json.loads(output)["applied"][0]["status"]["idempotent_reuse"] is True
    assert page.exists() and report.exists()


def test_cli_refuses_implicit_or_conflicting_batch_selection(tmp_path, monkeypatch):
    controller = tmp_path / "controller"; controller.mkdir()
    source = tmp_path / "legacy"; source.mkdir()
    receipts = tmp_path / "receipts"; receipts.mkdir()
    page = source / "page.md"; page.write_text("page\n")
    inventory = receipts / "inventory.json"; plan = receipts / "plan.json"; status = receipts / "status.json"
    declarations = receipts / "declarations.json"
    declarations.write_text(json.dumps([{"kind": "wiki", "path": "page.md"}]))
    monkeypatch.setenv("JUNO_TASK_ROOT", str(controller))
    assert run_cli(["migration", "inventory", "--source-root", str(source), "--declarations", str(declarations),
                    "--output", str(inventory)])[0] == 0
    assert run_cli(["migration", "plan", "--source-root", str(source), "--inventory", str(inventory),
                    "--output", str(plan)])[0] == 0
    record_id = json.loads(plan.read_text())["items"][0]["record_id"]
    code, _, error = run_cli(["migration", "apply", "--source-root", str(source), "--plan", str(plan),
                              "--status-file", str(status)])
    assert code == 5 and json.loads(error)["error"]["code"] == "MIGRATION_SELECTION_INVALID"
    code, _, error = run_cli(["migration", "apply", "--source-root", str(source), "--plan", str(plan),
                              "--status-file", str(status), "--id", record_id, "--all"])
    assert code == 5 and json.loads(error)["error"]["code"] == "MIGRATION_SELECTION_INVALID"
