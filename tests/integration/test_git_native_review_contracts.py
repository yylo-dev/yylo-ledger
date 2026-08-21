"""Acceptance regressions for cache, pagination, conversion, rollback, and receipts."""
import base64
import hashlib
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import textwrap
import zipfile

import pytest
import subprocess

from kanban import __version__
from kanban.cache import TaskCache
from kanban.cli import TaskCLI
from kanban.config import Config
from kanban.search import SearchFilters, TaskSearch
from kanban.storage import TaskStorage


def make_storage(root: Path, fields=None):
    tasks = root / ".juno_task/tasks"
    tasks.mkdir(parents=True)
    cfg = deepcopy(Config.DEFAULT_CONFIG)
    cfg["storage"]["base_path"] = str(tasks)
    cfg["custom_fields"] = fields or {}
    path = tasks / "config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return TaskStorage(Config(str(path))), path


def run(config, args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = TaskCLI().run(["-c", str(config), *args])
    return code, out.getvalue(), err.getvalue()


def write_receipt(path: Path, operation: str, **values):
    payload = {"receipt_version": 2, "operation": operation, "verdict": "pass", **values}
    payload = TaskStorage._seal_receipt(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def legacy(path: Path):
    row = {"id": "Ab1Cd2", "status": "todo", "body": "hello", "agent_response": "",
           "created_date": "2026-07-22 00:00:00", "last_modified": "2026-07-22 00:00:00",
           "commit_hash": None, "feature_tags": None, "related_tasks": None, "blocked_by": None}
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")


@pytest.fixture(scope="session")
def legacy_wheel(tmp_path_factory):
    """Build a deterministic executable legacy-NDJSON wheel without network/build tools."""
    root = tmp_path_factory.mktemp("legacy-wheel")
    wheel = root / "juno_kanban-1.42.0-py3-none-any.whl"
    cli = textwrap.dedent(r'''
        import json, sys
        from pathlib import Path
        def main():
            argv = sys.argv[1:]
            config = Path(argv[argv.index("-c") + 1]); del argv[argv.index("-c"):argv.index("-c") + 2]
            if "-f" in argv:
                i = argv.index("-f"); del argv[i:i + 2]
            cfg = json.loads(config.read_text())
            base = Path(cfg["storage"]["base_path"])
            rows = [json.loads(line) for line in (base / "backlog.ndjson").read_text().splitlines() if line]
            command, args = argv[0], argv[1:]
            by_id = {row["id"]: row for row in rows}
            if command == "list": selected = rows
            elif command == "get": selected = [by_id[args[0]]] if args[0] in by_id else []
            elif command == "search":
                task_id = args[args.index("--id") + 1]; selected = [by_id[task_id]] if task_id in by_id else []
            elif command == "ready":
                selected = [row for row in rows if row.get("status") in ("backlog", "todo", "in_progress") and
                            all(by_id.get(dep, {}).get("status") in ("done", "archive") for dep in row.get("blocked_by") or [])]
            elif command == "deps":
                task_id = args[0]; blockers = by_id.get(task_id, {}).get("blocked_by") or []
                info = {"task_id": task_id,
                        "unmet_blockers": [by_id[x] for x in blockers if by_id.get(x, {}).get("status") not in ("done", "archive")],
                        "met_blockers": [by_id[x] for x in blockers if by_id.get(x, {}).get("status") in ("done", "archive")],
                        "dependents": [row["id"] for row in rows if task_id in (row.get("blocked_by") or [])]}
                print(json.dumps([info])); return 0
            else: return 2
            print(json.dumps(selected))
            if command == "list":
                counts = {}
                for row in rows: counts[row.get("status")] = counts.get(row.get("status"), 0) + 1
                print(json.dumps({"summary": {"total_tasks": len(rows), "status_counts": counts}}))
            return 0
    ''') + "\nif __name__ == '__main__': raise SystemExit(main())\n"
    dist = "juno_kanban-1.42.0.dist-info"
    files = {
        "legacy_kanban/__init__.py": "",
        "legacy_kanban/cli.py": cli,
        f"{dist}/METADATA": "Metadata-Version: 2.1\nName: juno-kanban\nVersion: 1.42.0\n",
        f"{dist}/WHEEL": "Wheel-Version: 1.0\nGenerator: O2qgSE-fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{dist}/entry_points.txt": "[console_scripts]\njuno-kanban = legacy_kanban.cli:main\n",
        f"{dist}/RECORD": "",
    }
    with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            info = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0)); info.external_attr = 0o644 << 16
            archive.writestr(info, content)
    return wheel


def benchmark_receipt(path: Path, package_version=__version__):
    return write_receipt(path, "installed-cli-benchmark", tasks=140000, gates={"all": True},
        identity={"package_version": package_version, "cli_sha256": "a" * 64,
                  "benchmark_sha256": hashlib.sha256((Path(__file__).parents[2] / "src/yylo_ledger/benchmark_git_native.py").read_bytes()).hexdigest(),
                  "measured_commands": {key: [key] for key in ("get", "list", "search", "mutation", "cold_rebuild")}})


def test_cache_refreshes_manual_edits_config_and_non_git_deletes(tmp_path):
    storage, _ = make_storage(tmp_path)
    task = storage.create_task(id="Ab1Cd2", body="before", status="todo")
    assert [t["body"] for t in storage.read_all_tasks()] == ["before"]
    path = Path(storage.find_task_file(task.id))
    path.write_text(path.read_text().replace("before", "after"), encoding="utf-8")
    assert [t["body"] for t in storage.read_all_tasks()] == ["after"]
    canonical = list(storage.read_all_tasks_canonical())
    assert list(storage.read_all_tasks()) == [json.loads(json.dumps(canonical[0], default=str))]
    path.unlink()
    assert list(storage.read_all_tasks()) == []


def test_cache_backed_search_parity_and_timezone_boundaries(tmp_path):
    storage, config = make_storage(tmp_path, {"when": {"type": "datetime"}})
    storage.create_task(id="Ab1Cd2", body="needle", fields={"when": "2026-01-01T01:00:00+01:00"})
    storage.create_task(id="Xy9Za8", body="other", fields={"when": "2026-01-01T00:30:00Z"})
    cached = TaskSearch(storage.config, storage).search(SearchFilters(body_text="needle", limit=20))
    storage.cache.path.unlink()
    rebuilt = TaskSearch(storage.config, storage).search(SearchFilters(body_text="needle", limit=20))
    assert cached == rebuilt
    assert [r["id"] for r in storage.query_fields(field_before={"when": "2026-01-01T00:15:00Z"})] == ["Ab1Cd2"]
    # Public CLI range filters compare instants, not raw offset strings.
    code, out, err = run(config, ["search", "--field-before", "when=2026-01-01T01:00:00+01:00", "--format", "json"])
    assert code == 0 and "Ab1Cd2" not in out and "Xy9Za8" not in out, err
    code, out, err = run(config, ["search", "--field-after", "when=2025-12-31T19:15:00-05:00", "--format", "json"])
    assert code == 0 and "Xy9Za8" in out and "Ab1Cd2" not in out, err
    code, _, err = run(config, ["search", "--field-before", "when=2026-01-01T00:15:00", "--format", "json"])
    assert code != 0 and "timezone-aware" in err
    import sqlite3
    with sqlite3.connect(storage.cache.path) as db:
        assert db.execute("SELECT scalar_type, normalized_date FROM custom_fields WHERE task_id=? AND path=?",
                          ("Ab1Cd2", "when")).fetchone() == ("string", "2026-01-01T00:00:00Z")


@pytest.mark.parametrize("command,args", [
    ("list", ["list", "--limit", "1", "--format", "json"]),
    ("search", ["search", "--body", "needle", "--limit", "1", "--format", "json"]),
    ("ready", ["ready", "--limit", "1", "--format", "json"]),
])
def test_normal_collection_commands_never_materialize_full_cache(tmp_path, monkeypatch, command, args):
    storage, config = make_storage(tmp_path / command)
    storage.create_task(id="Ab1Cd2", body="needle", status="todo", fields={"customer": "a"})
    storage.create_task(id="Xy9Za8", body="needle", status="done")
    monkeypatch.setattr(TaskCache, "all", lambda self: (_ for _ in ()).throw(AssertionError("full cache scan")))
    code, out, err = run(config, args)
    assert code == 0, err
    assert "Ab1Cd2" in out or "Xy9Za8" in out


def test_custom_field_and_dependency_filters_use_sql_without_all(tmp_path, monkeypatch):
    storage, config = make_storage(tmp_path, {"due_date": {"type": "date"}})
    storage.create_task(id="Ab1Cd2", body="needle", status="todo", fields={"due_date": "2025-01-01"})
    storage.create_task(id="Xy9Za8", body="blocker", status="todo", blocked_by=["Ab1Cd2"])
    monkeypatch.setattr(TaskCache, "all", lambda self: (_ for _ in ()).throw(AssertionError("full cache scan")))
    assert run(config, ["search", "--field", "due_date=2025-01-01", "--format", "json"])[0] == 0
    plan = storage.cache.explain_query_plan(
        filters={"field_equals": {"due_date": "2025-01-01"}}, limit=1)
    assert any("custom_fields_lookup" in row for row in plan), plan
    assert not any(row == "SCAN t" for row in plan), plan
    code, out, err = run(config, ["ready", "--format", "json"])
    assert code == 0 and "Ab1Cd2" in out and "Xy9Za8" not in out, err


def test_dependency_show_and_cycle_checks_never_build_board_graph(tmp_path, monkeypatch):
    storage, config = make_storage(tmp_path)
    storage.create_task(id="Ab1Cd2", body="root", status="todo")
    storage.create_task(id="Xy9Za8", body="child", status="todo", blocked_by=["Ab1Cd2"])
    monkeypatch.setattr(TaskCLI, "_load_all_tasks_for_graph",
                        lambda self: (_ for _ in ()).throw(AssertionError("board graph scan")))
    code, out, err = run(config, ["--format", "json", "deps", "Xy9Za8"])
    assert code == 0 and "Ab1Cd2" in out, err
    code, _, err = run(config, ["deps", "add", "--id", "Ab1Cd2", "--blocked-by", "Xy9Za8"])
    assert code != 0 and "cycle" in err.lower()


def test_indexed_text_search_preserves_case_insensitive_substring_semantics(tmp_path):
    storage, config = make_storage(tmp_path)
    storage.create_task(id="Ab1Cd2", body="PrefixNeedleSuffix", status="todo")
    code, out, err = run(config, ["search", "--body", "NEEDLE", "--format", "json"])
    assert code == 0 and "Ab1Cd2" in out, err
    code, out, err = run(config, ["search", "--body", "le", "--format", "json"])
    assert code == 0 and "Ab1Cd2" in out, err


def test_cursor_is_keyset_cache_revision_bound_and_integrity_checked(tmp_path):
    storage, config = make_storage(tmp_path)
    for task_id in ("Ab1Cd2", "Xy9Za8", "Qr7St6"):
        storage.create_task(id=task_id, body=task_id, status="todo")
    code, out, _ = run(config, ["list", "--limit", "1", "--show-cursor", "--format", "json"])
    cursor = json.loads(out.splitlines()[1])["summary"]["next_cursor"]
    payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    assert "offset" not in payload and len(payload["last"]) == 3 and payload["revision"]
    payload["last"][2] = "tampered"
    # Even a caller who recomputes an unkeyed digest cannot forge the cache-secret HMAC.
    unsigned = dict(payload)
    unsigned.pop("integrity")
    payload["integrity"] = __import__("hashlib").sha256(
        json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    bad = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    assert run(config, ["list", "--limit", "1", "--cursor", bad, "--format", "json"])[0] != 0
    storage.update_task("Ab1Cd2", {"body": "changed"})
    code, _, err = run(config, ["list", "--limit", "1", "--cursor", cursor, "--format", "json"])
    assert code != 0 and "stale" in err


def test_complete_cli_mutation_receipts(tmp_path, monkeypatch):
    monkeypatch.delenv("JUNO_TASK_ROOT", raising=False)
    _, config = make_storage(tmp_path)
    receipts = []
    create_receipt = tmp_path / "create.json"
    code, out, _ = run(config, ["create", "body", "--receipt-file", str(create_receipt)])
    assert code == 0
    task_id = json.loads(out)[0]["id"]
    receipts.append(json.loads(create_receipt.read_text()))
    for operation, args in (
        ("update", ["update", task_id, "--status", "todo"]),
        ("mark", ["mark", "done", task_id, "--response", "done"]),
        ("archive", ["archive", task_id]),
    ):
        path = tmp_path / f"{operation}.json"
        assert run(config, [*args, "--receipt-file", str(path)])[0] == 0
        receipts.append(json.loads(path.read_text()))
    assert [r["operation"] for r in receipts] == ["create", "update", "mark", "archive"]
    assert all(set(r) == {"task_id", "operation", "before_sha256", "after_sha256",
                                "ledger_event_id", "changed_paths", "persisted_path",
                                "transaction"} for r in receipts)
    assert all(r["transaction"]["schema_version"] == "juno_kanban_mutation.v1"
               and len(r["transaction"]["plan_sha256"]) == 64
               and set(r["transaction"]["identity"]) == {
                   "git_common_dir", "controller_path", "controller_ref", "controller_head"}
               for r in receipts)


@pytest.mark.parametrize("point", [
    "after_staging_validation", "after_tasks_backup", "after_tasks_activation",
    "after_ledger_activation", "after_archive_activation", "after_config_save",
    "after_cache_rebuild", "after_source_disposition",
])
def test_every_conversion_fault_point_restores_all_assets(tmp_path, monkeypatch, point):
    storage, _ = make_storage(tmp_path / point)
    source = tmp_path / point / "legacy.ndjson"
    legacy(source)
    prior_archive = storage.juno_root / "archive" / "prior-marker.txt"
    prior_archive.parent.mkdir(parents=True, exist_ok=True)
    prior_archive.write_text("preserve me\n", encoding="utf-8")
    before_config = (storage.tasks_root / "config.json").read_bytes()
    monkeypatch.setattr(storage, "_conversion_fault",
                        lambda current: (_ for _ in ()).throw(OSError(current)) if current == point else None)
    with pytest.raises(OSError, match=point):
        storage.convert_legacy(source)
    assert (storage.tasks_root / "config.json").read_bytes() == before_config
    assert not list(storage.tasks_root.glob("*/*.md"))
    assert source.exists()
    assert not (storage.juno_root / "ledger").exists()
    assert not list((storage.juno_root / "archive").glob("**/pack-*.ndjson"))
    assert prior_archive.read_text(encoding="utf-8") == "preserve me\n"


def test_refused_git_cutover_does_not_strand_conversion_freeze(tmp_path):
    repo = tmp_path / "repo"
    storage, _ = make_storage(repo)
    source = repo / ".juno_task/backlog.ndjson"
    legacy(source)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "pre-cutover"], cwd=repo, check=True, capture_output=True)

    with pytest.raises(ValueError, match="requires tag, external backup"):
        storage.convert_legacy(source)

    assert not (storage.juno_root / "CONVERSION_FREEZE.json").exists()
    assert subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=repo, text=True
    ).strip() == ""


def test_conversion_refuses_uncommitted_archive_state(tmp_path):
    repo = tmp_path / "repo-archive-dirty"
    storage, _ = make_storage(repo)
    source = repo / ".juno_task/backlog.ndjson"
    legacy(source)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "legacy"], cwd=repo, check=True, capture_output=True)
    dirty = storage.juno_root / "archive" / "uncommitted.txt"
    dirty.parent.mkdir(parents=True, exist_ok=True)
    dirty.write_text("must not be replaced\n", encoding="utf-8")

    with pytest.raises(ValueError, match="uncommitted task-storage changes"):
        storage.convert_legacy(source)

    assert dirty.read_text(encoding="utf-8") == "must not be replaced\n"
    assert not (storage.juno_root / "CONVERSION_FREEZE.json").exists()


def test_git_cutover_requires_and_records_verified_recovery_assets(tmp_path, legacy_wheel):
    repo = tmp_path / "repo"
    storage, _ = make_storage(repo)
    source = repo / ".juno_task/backlog.ndjson"
    legacy(source)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    # Legacy repositories can still track the SQLite cache despite current
    # ignore rules; cutover must remove that derived state from the index.
    (repo / ".gitignore").write_text(".juno_task/cache/\n", encoding="utf-8")
    legacy_cache = repo / ".juno_task/cache/kanban.sqlite3"
    legacy_cache.parent.mkdir(parents=True, exist_ok=True)
    legacy_cache.write_bytes(b"legacy derived cache")
    subprocess.run(["git", "add", "-f", ".gitignore", ".juno_task/cache/kanban.sqlite3"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "pre-cutover"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "tag", "pre-cutover-test"], cwd=repo, check=True)
    source_hash = __import__("hashlib").sha256(source.read_bytes()).hexdigest()
    benchmark = benchmark_receipt(tmp_path / "benchmark.json")
    report = storage.convert_legacy(
        source, pre_cutover_tag="pre-cutover-test", backup_path=tmp_path / "external-backup",
        legacy_package=legacy_wheel, new_package_version=__version__,
        benchmark_receipt=benchmark, report_path=tmp_path / "conversion.json",
    )
    assets = report["pre_cutover_assets"]
    assert assets["production_ready"] is True and Path(assets["backup"]).exists()
    assert Path(assets["manifest"]).exists() and report["source_disposition"] == "removed_from_active_storage"
    assert assets["legacy_package"]["sha256"] == hashlib.sha256(legacy_wheel.read_bytes()).hexdigest()
    assert not source.exists() and report["post_activation_doctor"] == []
    assert report["cutover_commit"] == storage._git_head() and report["cutover_parent"] == assets["commit"]
    tracked = subprocess.check_output(["git", "ls-files"], cwd=repo, text=True).splitlines()
    assert ".juno_task/cache/kanban.sqlite3" not in tracked
    assert storage.cache.path.exists()
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True).strip() == ""


def test_conversion_precommit_fault_restores_legacy_and_creates_no_cutover(tmp_path, legacy_wheel, monkeypatch):
    repo = tmp_path / "repo"
    storage, _ = make_storage(repo)
    source = repo / ".juno_task/backlog.ndjson"; legacy(source)
    (repo / ".gitignore").write_text(".juno_task/cache/\n.juno_task/locks/\n")
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "legacy"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "tag", "pre-cutover-test"], cwd=repo, check=True)
    original = storage._git_head()
    monkeypatch.setattr(storage, "_conversion_fault",
        lambda point: (_ for _ in ()).throw(OSError(point)) if point == "before_cutover_commit" else None)
    with pytest.raises(OSError, match="before_cutover_commit"):
        storage.convert_legacy(source, pre_cutover_tag="pre-cutover-test", backup_path=tmp_path / "backup",
            legacy_package=legacy_wheel, new_package_version=__version__,
            benchmark_receipt=benchmark_receipt(tmp_path / "benchmark.json"), report_path=tmp_path / "conversion.json")
    assert storage._git_head() == original and source.exists() and not list(storage.tasks_root.glob("*/*.md"))
    assert not list((storage.juno_root / "archive").glob("**/pack-*.ndjson"))
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True).strip() == ""


def test_content_addressed_benchmark_receipt_with_sidecar_is_accepted(tmp_path):
    evidence = tmp_path / "installed-cli-140k.json"
    evidence.write_text(json.dumps({
        "receipt_version": 2,
        "operation": "installed-cli-benchmark",
        "verdict": "pass",
        "tasks": 140000,
    }), encoding="utf-8")
    digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
    Path(str(evidence) + ".sha256").write_text(
        f"{digest}  {evidence.name}\n", encoding="utf-8")
    receipt = TaskStorage._read_verified_receipt(evidence, "benchmark")
    assert receipt["operation"] == "installed-cli-benchmark" and receipt["tasks"] == 140000


def test_receipt_driven_window_lift_and_nested_shape_refusal(tmp_path):
    storage, _ = make_storage(tmp_path)
    source = tmp_path / "legacy.ndjson"
    legacy(source)
    report = storage.convert_legacy(source)
    storage.config.config["compatibility_window"]["allowed_field_shapes"] = ["customer", "customer.name"]
    storage.config.save()
    with pytest.raises(ValueError, match="new field shapes"):
        storage.update_task("Ab1Cd2", {"fields": {"customer": {"name": "a", "secret": "x"}}})
    # A future-dated caller-authored receipt cannot lift the gate early.
    window = storage.config.config["compatibility_window"]
    premature = tmp_path / "premature.json"
    write_receipt(premature, "seven-day-acceptance", window_id=window["id"],
        observed_end=(datetime.fromisoformat(window["end"].replace("Z", "+00:00")) + timedelta(seconds=1)).isoformat(),
        passed_gates=["conversion_parity", "mutation_conflicts", "reconciliation", "cache_parity",
                      "worktree_merges", "privacy", "performance", "rollback_rehearsal"])
    with pytest.raises(ValueError, match="future|machine-generated"):
        storage.lift_compatibility_window(premature)

    window["end"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    storage.config.save()
    gates = ["conversion_parity", "mutation_conflicts", "reconciliation", "cache_parity",
             "worktree_merges", "privacy", "performance", "rollback_rehearsal"]
    evidence = {}
    conversion = tmp_path / "conversion-evidence.json"
    conversion_payload = TaskStorage._seal_receipt({"receipt_version": 2, "operation": "convert", "verdict": "pass",
        "semantic_hashes_match": True, "compatibility_window": {"id": window["id"]}})
    conversion.write_text(json.dumps(conversion_payload)); evidence["conversion_parity"] = conversion
    performance = tmp_path / "performance.json"
    benchmark_receipt(performance)
    evidence["performance"] = performance
    current_hash = hashlib.sha256(__import__("kanban.codec", fromlist=["normalized_bytes"]).normalized_bytes(sorted(
        (__import__("kanban.codec", fromlist=["plain_value"]).plain_value(row)
         for row in storage.read_all_tasks_canonical()), key=lambda row: row["id"]))).hexdigest()
    command_result = {"argv": ["python", "-m", "pytest", "gate"], "exit_code": 0,
                      "stdout_sha256": hashlib.sha256(b"passed").hexdigest()}
    for gate in set(gates) - {"conversion_parity", "performance"}:
        path = tmp_path / f"{gate}.json"
        write_receipt(path, f"acceptance-{gate}", window_id=window["id"],
                      generator="juno-kanban acceptance-gate", source_commit=storage._git_head(),
                      config_sha256=storage._config_hash(), current_state_sha256=current_hash,
                      finished_at=datetime.now(timezone.utc).isoformat(), command_results=[command_result])
        evidence[gate] = path
    acceptance = tmp_path / "acceptance.json"
    generated = storage.generate_acceptance_receipt(evidence, acceptance)
    assert generated["generator"] == "juno-kanban compatibility accept"
    lifted = storage.lift_compatibility_window(acceptance)
    assert lifted["window"]["active"] is False and lifted["window"]["acceptance_receipt_sha256"]


def test_immediate_rollback_executes_only_receipt_bound_cutover_revert(tmp_path, legacy_wheel, monkeypatch):
    repo = tmp_path / "repo"
    storage, _ = make_storage(repo)
    source = repo / ".juno_task/backlog.ndjson"
    legacy(source)
    (repo / ".gitignore").write_text(".juno_task/cache/\n.juno_task/locks/\n")
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "legacy"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "tag", "pre-cutover-test"], cwd=repo, check=True)
    conversion_path = tmp_path / "conversion.json"
    conversion = storage.convert_legacy(source, pre_cutover_tag="pre-cutover-test",
        backup_path=tmp_path / "backup", legacy_package=legacy_wheel, new_package_version=__version__,
        benchmark_receipt=benchmark_receipt(tmp_path / "benchmark.json"), report_path=conversion_path)
    monkeypatch.setattr(storage, "_rollback_fault", lambda point: (_ for _ in ()).throw(OSError(point)))
    with pytest.raises(OSError, match="before_immediate_revert"):
        storage.immediate_rollback(conversion_path, tmp_path / "fault.json")
    assert storage._git_head() == conversion["cutover_commit"]
    monkeypatch.setattr(storage, "_rollback_fault", lambda point: None)
    report = storage.immediate_rollback(conversion_path, tmp_path / "immediate.json")
    assert report["verdict"] == "pass" and report["rollback_commit"] != conversion["cutover_commit"]
    assert source.exists() and not list(storage.tasks_root.glob("*/*.md"))
    assert json.loads((tmp_path / "immediate.json").read_text())["content_sha256"]
    forged = tmp_path / "forged.json"
    forged_payload = dict(conversion, cutover_commit=report["rollback_commit"])
    forged.write_text(json.dumps(TaskStorage._seal_receipt(forged_payload)))
    with pytest.raises(ValueError):
        storage.immediate_rollback(forged, tmp_path / "second.json")


def init_current_repo(repo: Path):
    storage, _ = make_storage(repo)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    storage.create_task(id="Ab1Cd2", body="x", status="todo", blocked_by=[])
    storage.create_task(id="Xy9Za8", body="done", status="done", blocked_by=[])
    (repo / ".gitignore").write_text(".juno_task/cache/\n.juno_task/locks/\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "current"], cwd=repo, check=True, capture_output=True)
    return storage


def test_acceptance_rejects_stale_snapshot_and_self_declared_gate(tmp_path):
    storage, _ = make_storage(tmp_path)
    source = tmp_path / "legacy.ndjson"; legacy(source)
    conversion = storage.convert_legacy(source)
    window = storage.config.config["compatibility_window"]
    window["end"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    storage.config.save()
    evidence = {}
    for gate in ("mutation_conflicts", "reconciliation", "cache_parity", "worktree_merges", "privacy", "rollback_rehearsal"):
        forged = tmp_path / f"{gate}.json"
        write_receipt(forged, f"acceptance-{gate}", window_id=window["id"], verified=True)
        evidence[gate] = forged
    conversion_path = tmp_path / "conversion.json"; conversion_path.write_text(json.dumps(conversion))
    evidence["conversion_parity"] = conversion_path
    benchmark_receipt(tmp_path / "performance.json"); evidence["performance"] = tmp_path / "performance.json"
    with pytest.raises(ValueError, match="self-declared"):
        storage.generate_acceptance_receipt(evidence, tmp_path / "acceptance.json")


def test_post_write_rollback_installs_executes_activates_and_commits_exact_legacy(tmp_path, legacy_wheel):
    repo = tmp_path / "repo"
    storage = init_current_repo(repo)
    external = tmp_path / "external"; external.mkdir()
    receipt = storage.execute_post_write_rollback(
        legacy_wheel, external / "venv", external / "archive.tar.gz", external / "rollback.json")
    assert receipt["rollback_commit"] == storage._git_head()
    assert receipt["archive_sha256"] and receipt["export"]["sha256"]
    assert [Path(result["argv"][0]).name for result in receipt["legacy_parity_commands"]] == ["juno-kanban"] * 5
    assert (storage.tasks_root / "backlog.ndjson").exists() and not list(storage.tasks_root.glob("*/*.md"))
    assert (storage.juno_root / "kanban-runtime").stat().st_mode & 0o111
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True).strip() == ""
    assert json.loads((external / "rollback.json").read_text())["content_sha256"]


def test_post_write_rollback_refuses_incompatible_state_and_removed_force_paths(tmp_path, legacy_wheel):
    repo = tmp_path / "repo"
    storage = init_current_repo(repo)
    storage.config.config["compatibility_window"] = {"active": False}
    storage.config.save()
    storage.update_task("Ab1Cd2", {"fields": {"new_only": {"nested": True}}})
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "incompatible"], cwd=repo, check=True, capture_output=True)
    external = tmp_path / "external"; external.mkdir()
    with pytest.raises(ValueError, match="not representable"):
        storage.execute_post_write_rollback(
            legacy_wheel, external / "venv", external / "archive.tar.gz", external / "rollback.json")
    _, config = make_storage(tmp_path / "cli")
    assert run(config, ["convert", "legacy.ndjson", "--allow-dirty", "--report", str(tmp_path / "r.json")])[0] != 0
    assert run(config, ["export-legacy", str(tmp_path / "x.ndjson"), "--rollback-commit", "arbitrary"])[0] != 0


@pytest.mark.parametrize("point", [
    "after_freeze", "after_export", "after_archive", "after_legacy_install",
    "after_legacy_list", "after_legacy_get", "after_legacy_search", "after_legacy_ready",
    "after_legacy_dependency", "before_activation", "before_activation_commit",
])
def test_every_post_write_rollback_fault_is_frozen_and_preserves_current_truth(tmp_path, legacy_wheel, monkeypatch, point):
    repo = tmp_path / point
    storage = init_current_repo(repo)
    before = {path.relative_to(storage.juno_root): path.read_bytes()
              for path in storage.juno_root.rglob("*") if path.is_file() and "cache" not in path.parts}
    monkeypatch.setattr(storage, "_rollback_fault",
        lambda current: (_ for _ in ()).throw(OSError(current)) if current == point else None)
    external = tmp_path / f"external-{point}"; external.mkdir()
    with pytest.raises(OSError, match=point):
        storage.execute_post_write_rollback(
            legacy_wheel, external / "venv", external / "archive.tar.gz", external / "rollback.json")
    after = {path.relative_to(storage.juno_root): path.read_bytes()
             for path in storage.juno_root.rglob("*") if path.is_file() and "cache" not in path.parts
             and path.name != "ROLLBACK_FREEZE.json"}
    assert after == before
    assert (storage.juno_root / "ROLLBACK_FREEZE.json").exists()
    with pytest.raises(ValueError, match="frozen"):
        storage.update_task("Ab1Cd2", {"body": "must fail"})
