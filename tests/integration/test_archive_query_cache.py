"""Cold exact/query/cache contracts: canonical packs remain the only cold truth."""
from pathlib import Path
import sqlite3
import time

import pytest

from kanban.archive import (ArchiveFormatError, archive_doctor, create_archive,
                            plan_archive, read_indexed_archive_record,
                            scan_archive_id_inventory, scan_archive_index)
from kanban.cli import ExitCode, TaskCLI
from kanban.search import TaskSearch
from kanban.storage import ArchivedTaskError, TaskStorage
from tests.integration.test_archive_transaction import git, make_repository


def archived_repository(tmp_path):
    storage, root, plan = make_repository(tmp_path)
    create_archive(storage, plan, tmp_path / "archive-receipt.json", "test")
    assert storage.find_task("Aa1Aa1") is None  # hot-only internal lookup
    return storage, root


def test_exact_get_and_history_survive_deleted_and_corrupt_cache(tmp_path):
    storage, _ = archived_repository(tmp_path)
    expected = TaskSearch(storage=storage).search_by_id("Aa1Aa1")
    expected_history = storage.history("Aa1Aa1", include_content=True)
    assert expected["body"] == "complete body"
    assert len(expected_history) == 2

    for path in (storage.cache.path, Path(str(storage.cache.path) + "-wal"),
                 Path(str(storage.cache.path) + "-shm")):
        path.unlink(missing_ok=True)
    assert storage.find_task_exact("Aa1Aa1") == expected
    assert storage.history("Aa1Aa1", include_content=True) == expected_history

    storage.cache.path.write_bytes(b"not a sqlite database")
    assert storage.find_task_exact("Aa1Aa1") == expected
    assert storage.history("Aa1Aa1") == storage.history("Aa1Aa1", include_content=False)

    import sqlite3
    with sqlite3.connect(storage.cache.path) as db:
        db.execute("DELETE FROM archive_tasks WHERE id='Aa1Aa1'")
    assert storage.find_task_exact("Aa1Aa1") == expected


def test_hot_get_missing_get_and_create_do_not_rebuild_complete_cache(tmp_path, monkeypatch):
    """Archive misses use checksummed manifests, not a full pack/cache rebuild.

    Why this regression matters: every hot ID and every genuinely new ID is absent
    from the cold SQLite table. Treating that ordinary miss as cache corruption made
    get/create latency proportional to the complete hot board plus cold pack bytes.
    """
    storage, _ = archived_repository(tmp_path)
    storage.create_task(id="Bb2Bb2", body="hot task", status="todo")

    rebuilds = 0
    original_rebuild = storage.rebuild_cache

    def counted_rebuild():
        nonlocal rebuilds
        rebuilds += 1
        return original_rebuild()

    monkeypatch.setattr(storage, "rebuild_cache", counted_rebuild)

    assert storage.find_task_exact("Bb2Bb2")["body"] == "hot task"
    assert storage.find_task_exact("Cc3Cc3") is None
    assert storage.find_task_exact("Aa1Aa1")["body"] == "complete body"
    storage.create_task(id="Dd4Dd4", body="new task", status="todo")
    assert rebuilds == 0


def test_public_cli_hot_cold_missing_get_and_create_avoid_full_rebuild(tmp_path, monkeypatch, capsys):
    storage, root = archived_repository(tmp_path)
    storage.create_task(id="Bb2Bb2", body="hot task", status="todo")
    monkeypatch.setenv("JUNO_TASK_ROOT", str(root))

    rebuilds = 0
    original_rebuild = storage.rebuild_cache

    def counted_rebuild():
        nonlocal rebuilds
        rebuilds += 1
        return original_rebuild()

    monkeypatch.setattr(TaskStorage, "rebuild_cache", lambda instance: counted_rebuild())
    config = str(storage.config.config_path)
    assert TaskCLI().run(["-c", config, "-f", "json", "get", "Bb2Bb2", "--compact"]) == ExitCode.SUCCESS
    assert TaskCLI().run(["-c", config, "-f", "json", "get", "Aa1Aa1", "--compact"]) == ExitCode.SUCCESS
    assert TaskCLI().run(["-c", config, "-f", "json", "get", "Cc3Cc3", "--compact"]) == ExitCode.GENERAL_ERROR
    assert TaskCLI().run(["-c", config, "-f", "json", "create", "public CLI task",
                          "--status", "todo"]) == ExitCode.SUCCESS
    capsys.readouterr()
    assert rebuilds == 0


def test_hot_cli_get_succeeds_with_named_warning_when_cache_is_missing(tmp_path, monkeypatch, capsys):
    storage, root = archived_repository(tmp_path)
    storage.create_task(id="Bb2Bb2", body="hot task", status="todo")
    monkeypatch.setenv("JUNO_TASK_ROOT", str(root))
    for path in (storage.cache.path, Path(str(storage.cache.path) + "-wal"),
                 Path(str(storage.cache.path) + "-shm")):
        path.unlink(missing_ok=True)
    monkeypatch.setattr(TaskStorage, "rebuild_cache",
                        lambda self: (_ for _ in ()).throw(AssertionError("hot get rebuilt cache")))

    code = TaskCLI().run(["-c", str(storage.config.config_path), "-f", "json",
                          "get", "Bb2Bb2", "--compact"])

    captured = capsys.readouterr()
    assert code == ExitCode.SUCCESS
    assert '"id": "Bb2Bb2"' in captured.out
    assert "Warning [exact_get_enrichment_unavailable]" in captured.err
    assert "derived cache is missing" in captured.err


def test_hot_cli_get_bounds_real_sqlite_lock_and_returns_canonical_task(tmp_path, monkeypatch, capsys):
    storage, root = archived_repository(tmp_path)
    storage.create_task(id="Bb2Bb2", body="hot task", status="todo")
    storage.rebuild_cache()
    monkeypatch.setenv("JUNO_TASK_ROOT", str(root))
    monkeypatch.setenv("YYLO_LEDGER_CACHE_TIMEOUT_SECONDS", "0.1")
    holder = sqlite3.connect(storage.cache.path)
    holder.execute("PRAGMA journal_mode=DELETE")
    holder.execute("PRAGMA locking_mode=EXCLUSIVE")
    holder.execute("BEGIN EXCLUSIVE")
    holder.execute("UPDATE metadata SET value=value WHERE key='schema_version'")
    started = time.monotonic()
    try:
        code = TaskCLI().run(["-c", str(storage.config.config_path), "-f", "json",
                              "get", "Bb2Bb2", "--compact"])
    finally:
        holder.rollback()
        holder.close()
    elapsed = time.monotonic() - started

    captured = capsys.readouterr()
    assert code == ExitCode.SUCCESS
    assert elapsed < 1.0
    assert '"id": "Bb2Bb2"' in captured.out
    assert "exact_get_enrichment_unavailable" in captured.err
    assert "database is locked" in captured.err
    assert str(storage.cache.path) in captured.err


def test_deleted_cold_cache_row_rebuilds_once_from_checksumbound_inventory(tmp_path, monkeypatch):
    storage, _ = archived_repository(tmp_path)
    import sqlite3
    with sqlite3.connect(storage.cache.path) as db:
        db.execute("DELETE FROM archive_tasks WHERE id='Aa1Aa1'")

    rebuilds = 0
    original_rebuild = storage.rebuild_cache

    def counted_rebuild():
        nonlocal rebuilds
        rebuilds += 1
        return original_rebuild()

    monkeypatch.setattr(storage, "rebuild_cache", counted_rebuild)
    assert storage.find_task_exact("Aa1Aa1")["body"] == "complete body"
    assert rebuilds == 1


def test_self_consistent_archive_cache_tamper_cannot_hide_or_release_id(tmp_path):
    storage, _ = archived_repository(tmp_path)

    def hide_archived_id_while_cache_appears_complete():
        import sqlite3
        with sqlite3.connect(storage.cache.path) as db:
            db.execute("DELETE FROM archive_tasks WHERE id='Aa1Aa1'")
            db.execute("UPDATE metadata SET value='0' WHERE key='archive_record_count'")
        assert storage.cache.archive_entry("Aa1Aa1") is None
        assert storage.cache.archive_index_complete()

    hide_archived_id_while_cache_appears_complete()
    assert storage.find_task_exact("Aa1Aa1")["body"] == "complete body"

    hide_archived_id_while_cache_appears_complete()
    with pytest.raises(ArchivedTaskError, match="new task.*related_tasks"):
        storage.create_task(id="Aa1Aa1", body="must remain reserved", status="todo")
    assert not storage.task_path("Aa1Aa1").exists()

    hide_archived_id_while_cache_appears_complete()
    with pytest.raises(ValueError, match="case-insensitive task ID collision"):
        storage.create_task(id="aa1aa1", body="case reuse", status="todo")
    assert not storage.task_path("aa1aa1").exists()


def test_archive_miss_proof_rejects_unchecksummed_manifest_tamper(tmp_path):
    storage, _ = archived_repository(tmp_path)
    manifest = next((storage.juno_root / "archive").glob("*/*/pack-*.manifest.json"))
    manifest.chmod(0o644)
    manifest.write_bytes(manifest.read_bytes().replace(b'"Aa1Aa1"', b'"Zz9Zz9"', 1))

    with pytest.raises(ArchiveFormatError, match="checksum sidecar mismatch"):
        scan_archive_id_inventory(storage.juno_root)


def test_exact_cold_get_verifies_pack_hash_without_rebuilding_every_record(tmp_path, monkeypatch):
    """Exact reads verify immutable bytes and only decode the selected envelope.

    Reconstructing all 1,000 records in a selected production pack made one exact
    read CPU-bound. Doctor/cache rebuild retain complete semantic verification;
    exact lookup must still bind the complete pack hash and fully verify its row.
    """
    storage, _ = archived_repository(tmp_path)
    entry = storage.cache.archive_entry("Aa1Aa1")

    monkeypatch.setattr("kanban.archive.rebuild_manifest", lambda *args, **kwargs:
                        (_ for _ in ()).throw(AssertionError("rebuilt complete pack")))

    assert read_indexed_archive_record(entry)["task"]["body"] == "complete body"


def test_repeated_exact_lookups_reuse_unchanged_manifest_inventory(tmp_path, monkeypatch):
    storage, _ = archived_repository(tmp_path)
    storage.create_task(id="Bb2Bb2", body="hot task", status="todo")
    storage._archive_id_inventory = None
    import kanban.archive as archive_module
    scans = 0
    original = archive_module.scan_archive_id_inventory

    def counted(juno_root):
        nonlocal scans
        scans += 1
        return original(juno_root)

    monkeypatch.setattr(archive_module, "scan_archive_id_inventory", counted)
    assert storage.find_task_exact("Bb2Bb2")["id"] == "Bb2Bb2"
    assert storage.find_task_exact("Bb2Bb2")["id"] == "Bb2Bb2"
    assert storage.find_task_exact("Cc3Cc3") is None
    assert scans == 1


def test_hot_cold_duplicate_and_pack_corruption_fail_closed(tmp_path):
    storage, _ = archived_repository(tmp_path)
    cold = storage.find_task_exact("Aa1Aa1")
    storage._write_current(cold)
    with pytest.raises(ArchiveFormatError, match="both hot and cold"):
        storage.find_task_exact("Aa1Aa1")
    storage.task_path("Aa1Aa1").unlink()
    storage.rebuild_cache()

    pack = next((storage.juno_root / "archive").glob("*/*/pack-*.ndjson"))
    original = pack.read_bytes()
    pack.chmod(0o644)
    pack.write_bytes(original.replace(b"complete body", b"corrupt! body", 1))
    with pytest.raises(ArchiveFormatError):
        storage.find_task_exact("Aa1Aa1")
    pack.write_bytes(original)


def test_archived_ids_are_reserved_and_every_mutation_is_refused(tmp_path):
    storage, _ = archived_repository(tmp_path)
    with pytest.raises(ArchivedTaskError, match="new task.*related_tasks"):
        storage.update_task("Aa1Aa1", {"status": "todo"})
    with pytest.raises(ArchivedTaskError):
        storage.replace_task_record(storage.find_task_exact("Aa1Aa1"))
    with pytest.raises(ArchivedTaskError):
        storage.create_task(id="Aa1Aa1", body="reuse", status="todo")
    with pytest.raises(ValueError, match="collision"):
        storage.create_task(id="aa1aa1", body="case reuse", status="todo")


def test_archived_terminal_blocker_satisfies_ready_without_entering_hot_collections(tmp_path):
    storage, _ = archived_repository(tmp_path)
    storage.create_task(id="Bb2Bb2", body="follow-up", status="todo",
                        blocked_by=["Aa1Aa1"], related_tasks=["Aa1Aa1"])
    result = storage.query_collection(filters={}, limit=None, ready=True)
    assert [task["id"] for task in result["tasks"]] == ["Bb2Bb2"]
    dependency = storage.dependency_info("Bb2Bb2")
    assert dependency["blockers"] == [("Aa1Aa1", "done")]
    assert [task["id"] for task in storage.read_all_tasks()] == ["Bb2Bb2"]


def test_archive_search_is_explicit_bounded_sorted_and_reads_no_ledger(tmp_path):
    storage, _ = archived_repository(tmp_path)
    first = storage.archive_search(statuses=["done"], limit=1, offset=0,
                                   sort_order="desc")
    assert first["total"] == 1
    assert [task["id"] for task in first["tasks"]] == ["Aa1Aa1"]
    assert "ledger" not in first["tasks"][0]
    assert storage.archive_search(statuses=["archive"], limit=1, offset=0,
                                  sort_order="desc")["tasks"] == []
    assert storage.archive_search(statuses=["done"], before="2000-01-01T00:00:00Z",
                                  limit=1, offset=0)["tasks"] == []


def test_archive_search_cli_shares_projection_redaction_and_pagination(tmp_path, capsys, monkeypatch):
    storage, _ = archived_repository(tmp_path)
    cli = TaskCLI()
    cli.config, cli.storage = storage.config, storage
    cli.search = TaskSearch(storage.config, storage)
    monkeypatch.setenv("YYLO_LEDGER_LIST_BODY_TRUNCATE_CHARS", "3")

    args = cli.parser.parse_args(["archive-search", "--status", "done", "--limit", "1",
                                  "--projection", "summary", "-f", "ndjson"])
    assert cli.cmd_archive_search(args) == ExitCode.SUCCESS
    output = capsys.readouterr()
    assert "[Truncated full size:" in output.out
    assert '"ledger"' not in output.out

    args = cli.parser.parse_args(["archive-search", "--status", "done", "--limit", "1",
                                  "--offset", "1", "--projection", "metadata", "-f", "ndjson"])
    assert cli.cmd_archive_search(args) == ExitCode.SUCCESS
    assert "No archived results found" in capsys.readouterr().out

    with pytest.raises(SystemExit):
        cli.parser.parse_args(["archive-search", "--lim", "1"])


def test_archive_search_is_dispatched_by_public_run_path(tmp_path, capsys, monkeypatch):
    storage, root = archived_repository(tmp_path)
    monkeypatch.setenv("JUNO_TASK_ROOT", str(root))
    cli = TaskCLI()
    assert cli.run(["-c", str(storage.config.config_path), "archive-search", "--id", "Aa1Aa1",
                    "--limit", "1", "--projection", "metadata", "-f", "json"]) == ExitCode.SUCCESS
    output = capsys.readouterr()
    assert "Aa1Aa1" in output.out
    assert "complete body" not in output.out


def test_canonical_scan_and_derived_index_are_differentially_identical(tmp_path):
    storage, _ = archived_repository(tmp_path)
    from kanban.archive import read_indexed_archive_record
    canonical, inventory = scan_archive_index(storage.juno_root)
    storage.rebuild_cache()
    derived = storage.cache.archive_entry("Aa1Aa1")
    assert derived["record_sha256"] == canonical[0]["record_sha256"]
    assert storage.cache.archive_identity() is not None
    assert read_indexed_archive_record(derived)["task"] == storage.find_task_exact("Aa1Aa1")
    assert inventory


def test_unrelated_commit_does_not_rebuild_unchanged_archive_index(tmp_path, monkeypatch):
    """Cold cache identity follows the archive tree, not every repository commit.

    A production/controller commit on a 13k-task cold tier previously made the
    next search or create decode every archive record even when archive bytes had
    not changed. Dirty archive checks and the committed archive tree identity keep
    the integrity boundary without that unrelated-HEAD latency cliff.
    """
    storage, root = archived_repository(tmp_path)
    storage.rebuild_cache()
    archive_identity = storage.cache.archive_identity()
    (root / "unrelated-after-archive.txt").write_text("product commit\n", encoding="utf-8")
    git(root, "add", "unrelated-after-archive.txt")
    git(root, "commit", "-qm", "unrelated product change")
    assert storage.cache.archive_tree_identity(root) == archive_identity

    rebuilds = 0
    original_rebuild = storage.rebuild_cache

    def counted_rebuild():
        nonlocal rebuilds
        rebuilds += 1
        return original_rebuild()

    monkeypatch.setattr(storage, "rebuild_cache", counted_rebuild)
    result = storage.query_collection(filters={"body_text": "complete"}, limit=10)

    assert result["total"] == 0  # broad discovery remains hot-only
    assert rebuilds == 0


@pytest.mark.parametrize("missing", ["manifest", "checksum", "pack"])
def test_incomplete_artifact_triplets_fail_closed_across_all_consumers(tmp_path, missing):
    storage, _ = archived_repository(tmp_path)
    pack = next((storage.juno_root / "archive").glob("*/*/pack-*.ndjson"))
    stem = pack.name[:-len(".ndjson")]
    paths = {
        "pack": pack,
        "manifest": pack.with_name(stem + ".manifest.json"),
        "checksum": pack.with_name(stem + ".sha256"),
    }
    paths[missing].unlink()
    storage.cache.path.unlink(missing_ok=True)

    with pytest.raises(ArchiveFormatError, match="incomplete archive artifact triplet.*missing %s" % missing):
        scan_archive_index(storage.juno_root)
    failures = archive_doctor(storage.juno_root)
    assert failures and "missing %s" % missing in failures[0]["error"]
    assert any("missing %s" % missing in item["error"] for item in storage.doctor())
    with pytest.raises(ArchiveFormatError, match="missing %s" % missing):
        storage.find_task_exact("Aa1Aa1")
    with pytest.raises(ArchiveFormatError, match="missing %s" % missing):
        storage.history("Aa1Aa1")
    with pytest.raises(ArchiveFormatError, match="missing %s" % missing):
        plan_archive(storage)
    with pytest.raises(ArchiveFormatError, match="missing %s" % missing):
        storage.create_task(id="Aa1Aa1", body="must remain reserved", status="todo")


@pytest.mark.parametrize("sidecar_suffix", [".manifest.json", ".sha256"])
def test_orphan_archive_sidecars_are_integrity_failures(tmp_path, sidecar_suffix):
    storage, _ = archived_repository(tmp_path)
    pack = next((storage.juno_root / "archive").glob("*/*/pack-*.ndjson"))
    stem = pack.name[:-len(".ndjson")]
    source = pack.with_name(stem + sidecar_suffix)
    orphan = pack.with_name("pack-orphan" + sidecar_suffix)
    orphan.write_bytes(source.read_bytes())

    with pytest.raises(ArchiveFormatError, match="incomplete archive artifact triplet.*missing pack"):
        scan_archive_index(storage.juno_root)
    failures = archive_doctor(storage.juno_root)
    assert failures and "missing pack" in failures[0]["error"]


def test_archive_pack_doctor_cli_reports_incomplete_triplet(tmp_path, capsys):
    storage, _ = archived_repository(tmp_path)
    manifest = next((storage.juno_root / "archive").glob("*/*/pack-*.manifest.json"))
    manifest.unlink()
    cli = TaskCLI()
    cli.config, cli.storage = storage.config, storage
    args = cli.parser.parse_args(["archive-pack", "doctor"])

    assert cli.cmd_archive_pack(args) == ExitCode.VALIDATION_ERROR
    assert "missing manifest" in capsys.readouterr().out
