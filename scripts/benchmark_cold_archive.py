#!/usr/bin/env python3
"""Generate 10k/100k boards and measure cold archival through an installed CLI.

Fixture construction uses the package codecs because creating 100,000 tasks one
process at a time is not a useful CLI benchmark. Every operation recorded in the
report (plan/create/query/cache/doctor) is dispatched through the installed
``yylo-ledger`` entry point.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from copy import deepcopy
from importlib.metadata import version as package_version
from pathlib import Path

from yylo_ledger.codec import MarkdownTaskCodec
from yylo_ledger.config import Config
from yylo_ledger.ledger import _hash_event
from yylo_ledger.storage import TaskStorage

OLD = "2025-01-01T00:00:00Z"
RECENT = "2026-07-23T00:00:00Z"


def sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha_file(path: Path) -> str:
    return sha_bytes(path.read_bytes())


def task_id(index: int) -> str:
    # Six exact, case-sensitive alphanumeric characters with stable ordering.
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    value = index
    encoded = ""
    for _ in range(5):
        encoded = alphabet[value % 36] + encoded
        value //= 36
    return "T" + encoded


def run(argv: list[str], *, cwd: Path, env: dict[str, str], expect: int = 0):
    started = time.perf_counter()
    result = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    elapsed = time.perf_counter() - started
    if result.returncode != expect:
        raise RuntimeError(f"command failed ({result.returncode}): {argv}\n{result.stderr}")
    return result, elapsed


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--tasks", required=True, type=int, choices=(10000, 100000))
    parser.add_argument("--report", required=True)
    parser.add_argument("--cli", default=shutil.which("yylo-ledger"))
    parser.add_argument("--keep")
    args = parser.parse_args()
    cli = Path(args.cli or "").resolve()
    if not cli.is_file():
        parser.error("--cli must name an installed yylo-ledger entry point")
    report_path = Path(args.report).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    context = None if args.keep else tempfile.TemporaryDirectory(prefix="juno-cold-archive-")
    root = Path(args.keep).resolve() if args.keep else Path(context.name) / "board"
    tasks_root = root / ".juno_task" / "tasks"
    ledger_root = root / ".juno_task" / "ledger"
    tasks_root.mkdir(parents=True)
    ledger_root.mkdir(parents=True)

    cfg = deepcopy(Config.DEFAULT_CONFIG)
    cfg["storage"] = {"base_path": str(tasks_root), "file_pattern": "*/*.md", "default_file": ""}
    config_path = tasks_root / "config.json"
    config_path.write_text(json.dumps(cfg, sort_keys=True), encoding="utf-8")
    codec = MarkdownTaskCodec()
    fixture_started = time.perf_counter()
    eligible = min(1000, args.tasks)
    expected = {}
    for index in range(args.tasks):
        identifier = task_id(index)
        terminal = index < eligible
        timestamp = OLD if terminal else RECENT
        record = {
            "schema_version": 1, "id": identifier,
            "status": "done" if terminal else "todo",
            "created_date": timestamp, "last_modified": timestamp,
            "commit_hash": None, "feature_tags": ["synthetic", "cold-scale"],
            "related_tasks": [], "blocked_by": [], "fields": {"fixture_index": index},
            "body": f"Production-shaped Unicode payload Ω task {index}\n\n- bounded metadata\n",
            "agent_response": f"Terminal evidence {index}" if terminal else "",
        }
        task_path = tasks_root / identifier[:2].lower() / f"{identifier}.md"
        task_path.parent.mkdir(parents=True, exist_ok=True)
        task_path.write_text(codec.dumps(record), encoding="utf-8", newline="\n")
        digest = TaskStorage.normalized_hash(record)
        event = {
            "event_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"juno-cold-scale-{args.tasks}-{identifier}")),
            "task_id": identifier, "timestamp": timestamp, "operation": "create",
            "source": "cold-archive-scale-fixture", "before_sha256": None,
            "after_sha256": digest, "previous_event_sha256": None,
            "changed_paths": sorted("/" + key for key in record), "changes": [],
            "snapshot": record,
        }
        event["event_sha256"] = _hash_event(event)
        ledger_path = ledger_root / identifier[:2].lower() / identifier / "000001.ndjson"
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        ledger_path.write_text(json.dumps(event, ensure_ascii=False, sort_keys=True,
                                          separators=(",", ":")) + "\n", encoding="utf-8")
        if terminal:
            expected[identifier] = {"task_sha256": digest,
                                    "ledger_sha256": sha_file(ledger_path)}
    fixture_seconds = time.perf_counter() - fixture_started
    (root / ".gitignore").write_text(".juno_task/cache/\n.juno_task/locks/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "cold-scale"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "archive-scale@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Archive Scale"], cwd=root, check=True)
    subprocess.run(["git", "add", ".gitignore", ".juno_task/tasks", ".juno_task/ledger"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "production-shaped cold archive fixture"], cwd=root, check=True)
    fixture_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    env = dict(os.environ, JUNO_TASK_ROOT=str(root))

    def command(*arguments: str) -> list[str]:
        values = list(arguments)
        global_arguments = []
        if "-f" in values:
            index = values.index("-f")
            global_arguments = values[index:index + 2]
            del values[index:index + 2]
        return [str(cli), "-c", str(config_path), *global_arguments, *values]

    before_task_files = sum(1 for _ in tasks_root.glob("*/*.md"))
    before_ledger_files = sum(1 for _ in ledger_root.glob("*/*/*.ndjson"))
    baseline, baseline_seconds = run(command("list", "--limit", "20", "-f", "json"), cwd=root, env=env)

    checkout_parent = root.parent / "checkout-before"
    _, checkout_before_seconds = run(["git", "worktree", "add", "--detach", "-q",
                                      str(checkout_parent), "HEAD"], cwd=root, env=env)
    subprocess.run(["git", "worktree", "remove", "--force", str(checkout_parent)], cwd=root, check=True)

    plan_path = root.parent / "archive-plan.json"
    create_path = root.parent / "archive-create.json"
    plan_result, plan_seconds = run(command("archive-pack", "plan", "--status", "done,archive",
                                            "--older-than", "90d", "--max-tasks", "1000",
                                            "--target-bytes", "26214400", "--hard-max-bytes", "47185920",
                                            "--report", str(plan_path)), cwd=root, env=env)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    create_result, create_seconds = run(command("archive-pack", "create", "--plan", str(plan_path),
                                                "--report", str(create_path)), cwd=root, env=env)
    receipt = json.loads(create_path.read_text(encoding="utf-8"))

    after_task_files = sum(1 for _ in tasks_root.glob("*/*.md"))
    after_ledger_files = sum(1 for _ in ledger_root.glob("*/*/*.ndjson"))
    default_after, default_after_seconds = run(command("list", "--limit", "20", "-f", "json"), cwd=root, env=env)
    exact, exact_seconds = run(command("get", task_id(0), "--compact", "-f", "json"), cwd=root, env=env)
    history, history_seconds = run(command("history", task_id(0), "--include-content", "-f", "json"), cwd=root, env=env)
    search, search_seconds = run(command("archive-search", "--tag", "cold-scale", "--limit", "20",
                                         "--projection", "metadata", "-f", "json"), cwd=root, env=env)
    cache = root / ".juno_task" / "cache" / "kanban.sqlite3"
    for candidate in (cache, Path(str(cache) + "-wal"), Path(str(cache) + "-shm")):
        candidate.unlink(missing_ok=True)
    rebuild, rebuild_seconds = run(command("cache", "rebuild"), cwd=root, env=env)
    default_rebuilt, default_rebuilt_seconds = run(command("list", "--limit", "20", "-f", "json"), cwd=root, env=env)
    exact_rebuilt, exact_rebuilt_seconds = run(command("get", task_id(0), "--compact", "-f", "json"), cwd=root, env=env)
    archive_doctor_result, archive_doctor_seconds = run(command("archive-pack", "doctor"), cwd=root, env=env)
    doctor_result, doctor_seconds = run(command("doctor"), cwd=root, env=env)

    checkout_after = root.parent / "checkout-after"
    _, checkout_after_seconds = run(["git", "worktree", "add", "--detach", "-q",
                                     str(checkout_after), "HEAD"], cwd=root, env=env)
    subprocess.run(["git", "worktree", "remove", "--force", str(checkout_after)], cwd=root, check=True)

    packs = list((root / ".juno_task" / "archive").glob("*/*/pack-*.ndjson"))
    manifests = list((root / ".juno_task" / "archive").glob("*/*/pack-*.manifest.json"))
    selected = plan["selected_ids"]
    exact_task = {key: value for key, value in json.loads(exact.stdout)[0].items()
                  if not key.startswith("_")}
    default_tasks_after, _ = json.JSONDecoder().raw_decode(default_after.stdout)
    default_tasks_rebuilt, _ = json.JSONDecoder().raw_decode(default_rebuilt.stdout)
    gates = {
        "selected_cap_1000": len(selected) == eligible,
        "task_file_reduction": before_task_files - after_task_files == len(selected),
        "ledger_file_reduction": before_ledger_files - after_ledger_files == len(selected),
        "default_output_independent": (default_tasks_after == default_tasks_rebuilt and
                                       all(task["id"] not in selected for task in default_tasks_after) and
                                       len(default_after.stdout.encode()) < 25000),
        "exact_get_cache_rebuild_parity": exact.stdout == exact_rebuilt.stdout,
        "exact_task_hash_parity": TaskStorage.normalized_hash(exact_task) == expected[task_id(0)]["task_sha256"],
        "history_present": task_id(0) in history.stdout and "snapshot" in history.stdout,
        "archive_search_projected": ('"id"' in search.stdout and "Production-shaped" not in search.stdout and
                                     "Terminal evidence" not in search.stdout and '"ledger"' not in search.stdout),
        "archive_doctor": json.loads(archive_doctor_result.stdout)["ok"] is True,
        "global_doctor": json.loads(doctor_result.stdout)["ok"] is True,
        "pack_hard_max": all(path.stat().st_size <= 47185920 for path in packs),
        "pack_and_manifest_present": bool(packs) and len(packs) == len(manifests),
        "clean_after_create": subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True) == "",
    }
    timings = {
        "fixture": fixture_seconds, "baseline_list": baseline_seconds, "plan": plan_seconds,
        "create": create_seconds, "exact_get": exact_seconds, "history": history_seconds,
        "archive_search": search_seconds, "cache_rebuild": rebuild_seconds,
        "exact_get_after_rebuild": exact_rebuilt_seconds, "archive_doctor": archive_doctor_seconds,
        "global_doctor": doctor_seconds, "default_list_after_rebuild": default_rebuilt_seconds,
        "checkout_before": checkout_before_seconds,
        "checkout_after": checkout_after_seconds, "default_list_after": default_after_seconds,
    }
    payload = {
        "evidence_schema": 1, "operation": "cold-archive-installed-cli-scale",
        "verdict": "pass" if all(gates.values()) else "fail", "tasks": args.tasks,
        "selected_tasks": len(selected), "gates": gates,
        "counts": {"hot_task_files_before": before_task_files, "hot_task_files_after": after_task_files,
                   "hot_ledger_files_before": before_ledger_files, "hot_ledger_files_after": after_ledger_files,
                   "packs": len(packs), "max_pack_bytes": max(path.stat().st_size for path in packs)},
        "timings_seconds": {key: round(value, 3) for key, value in timings.items()},
        "identity": {"fixture_commit": fixture_commit, "archive_commit": receipt["archive_commit"],
                     "source_head": subprocess.check_output(["git", "-C", str(Path(__file__).resolve().parents[1]),
                                                              "rev-parse", "HEAD"], text=True).strip(),
                     "benchmark_sha256": sha_file(Path(__file__)), "cli": str(cli),
                     "cli_sha256": sha_file(cli), "package_version": package_version("yylo-ledger"),
                     "platform": platform.platform(), "python": sys.version},
        "commands": {"plan": command("archive-pack", "plan", "--report", "<external>"),
                     "create": command("archive-pack", "create", "--plan", "<external>", "--report", "<external>"),
                     "exact_get": command("get", task_id(0), "--compact", "-f", "json"),
                     "archive_search": command("archive-search", "--tag", "cold-scale", "--limit", "20",
                                                "--projection", "metadata", "-f", "json")},
        "receipt": {"plan_sha256": plan["plan_sha256"], "receipt_sha256": receipt["receipt_sha256"],
                    "pack_sha256": [sha_file(path) for path in packs]},
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    report_path.write_text(encoded, encoding="utf-8")
    Path(str(report_path) + ".sha256").write_text(sha_bytes(encoded.encode()) + "  " + report_path.name + "\n")
    print(json.dumps({"report": str(report_path), "verdict": payload["verdict"], "tasks": args.tasks}))
    if context is not None:
        context.cleanup()
    return 0 if payload["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
