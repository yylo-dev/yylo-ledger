"""Native package publication transport and preservation-first PDR migration."""
import hashlib
import io
import json
from unittest.mock import patch

from yylo_ledger.cli import TaskCLI
from yylo_ledger.package_wiki import MANIFEST_SCHEMA


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with patch("sys.stdout", out), patch("sys.stderr", err):
        code = TaskCLI().run(argv)
    return code, out.getvalue(), err.getvalue()


def test_cli_plan_publish_get_and_idempotent_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("JUNO_TASK_ROOT", str(tmp_path))
    text = "# Hydration\n\nUse exact locks, not `$UNTRUSTED` shell input.\n"
    bundle = {"schema_version": MANIFEST_SCHEMA, "package": "@yylo/cli", "version": "1.0.0",
              "artifact_sha256": "a" * 64, "entries": [
                  {"key": "controller/hydration.md", "title": "Hydration", "text": text,
                   "sha256": hashlib.sha256(text.encode()).hexdigest()}]}
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(bundle))
    code, output, error = run_cli(["wiki", "plan-package", "--manifest-file", str(manifest)])
    assert code == 0, error
    plan = tmp_path / "plan.json"
    plan.write_text(output)
    argv = ["wiki", "publish-package", "--manifest-file", str(manifest), "--plan-file", str(plan)]
    code, output, error = run_cli(argv)
    assert code == 0, error
    receipt = json.loads(output)
    assert run_cli(argv)[1] == output
    pin = receipt["records"][0]
    code, source, error = run_cli(["wiki", "get", pin["id"], "--revision", str(pin["revision"]), "--source"])
    assert code == 0, error
    assert source == text
    code, output, error = run_cli(["wiki", "history", pin["id"], "-f", "json"])
    assert code == 0, error
    assert len(json.loads(output)) == 1
    code, output, error = run_cli(["wiki", "bind-package", "--manifest-file", str(manifest),
                                  "--plan-file", str(plan)])
    assert code == 0, error
    binding = tmp_path / "binding.json"
    binding.write_text(output)
    code, _, error = run_cli(["wiki", "verify-package", "--binding-file", str(binding)])
    assert code == 0, error
    code, source, error = run_cli(["wiki", "get-package", "controller/hydration.md",
                                  "--binding-file", str(binding), "--source"])
    assert code == 0, error
    assert source == text
    binding.write_text("{}")
    code, _, error = run_cli(["wiki", "get-package", "controller/hydration.md",
                              "--binding-file", str(binding), "--source"])
    assert code == 5 and "BINDING_INVALID" in error


def test_cli_bad_manifest_is_typed_refusal(tmp_path, monkeypatch):
    monkeypatch.setenv("JUNO_TASK_ROOT", str(tmp_path))
    manifest = tmp_path / "manifest.json"
    manifest.write_text("[]")
    code, _, error = run_cli(["wiki", "plan-package", "--manifest-file", str(manifest)])
    assert code == 5
    assert json.loads(error)["error"]["code"] == "PACKAGE_WIKI_MANIFEST_INVALID"


def test_explicit_pdr_migration_keeps_source_and_mapping(tmp_path, monkeypatch):
    controller = tmp_path / "controller"
    source = tmp_path / "legacy"
    receipts = tmp_path / "receipts"
    for directory in (controller, source, receipts):
        directory.mkdir()
    payload = "# Revisable requirement\n\nExplicit approval remains task-pinned.\n"
    original = source / "requirements.md"
    original.write_text(payload)
    declarations = receipts / "declarations.json"
    declarations.write_text(json.dumps([{"kind": "pdr", "path": "requirements.md"}]))
    inventory = receipts / "inventory.json"
    plan = receipts / "plan.json"
    status = receipts / "status.json"
    monkeypatch.setenv("JUNO_TASK_ROOT", str(controller))
    argv = ["migration", "inventory", "--source-root", str(source), "--declarations", str(declarations),
            "--output", str(inventory)]
    code, _, error = run_cli(argv)
    assert code == 0, error
    code, _, error = run_cli(["migration", "plan", "--source-root", str(source),
                              "--inventory", str(inventory), "--output", str(plan)])
    assert code == 0, error
    identity = json.loads(plan.read_text())["items"][0]["record_id"]
    args = ["migration", "apply", "--source-root", str(source), "--plan", str(plan),
            "--status-file", str(status), "--id", identity]
    code, _, error = run_cli(args)
    assert code == 0, error
    code, output, error = run_cli(args)
    assert code == 0, error
    assert json.loads(output)["applied"][0]["status"]["idempotent_reuse"]
    code, text, error = run_cli(["pdr", "get", identity, "--source"])
    assert code == 0, error
    assert text == payload == original.read_text()
