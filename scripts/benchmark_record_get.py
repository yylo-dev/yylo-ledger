#!/usr/bin/env python3
"""Disposable individual exact-read benchmark; no timing-based pass/fail gates.

Run with the task-local test Python. Results separate resolver IO from CLI startup.
"""
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from yylo_ledger.record_cli import RecordCLI
from yylo_ledger.records import RecordError


def main():
    with tempfile.TemporaryDirectory(prefix="ledger-get-benchmark-") as temporary:
        root = Path(temporary) / ".juno_task"
        # A prefixed read must never touch the task store or its derived cache.
        tasks = SimpleNamespace(juno_root=root, git_project_root=root.parent, git_repository_ids={})
        def forbidden(*args):
            raise AssertionError("unselected task store was accessed")
        tasks.get_record = tasks.resolve_record_id = forbidden
        reader = RecordCLI(SimpleNamespace(storage=tasks))
        for i in range(100):
            reader.documents.create(record_id=f"doc_A{i:05d}", title="Benchmark", profile="wiki",
                                    media_type="text/markdown", text="small readable document")
            reader.artifacts.create(record_id=f"artifact_A{i:05d}", title="Benchmark", profile="report",
                                    mode="local", media_type="text/plain", content=b"small report")
        results = {}
        for identity in ("doc_A00050", "artifact_A00050", "doc_Z99999", "artifact_Z99999"):
            samples = []
            for _ in range(100):
                started = time.perf_counter_ns()
                try:
                    reader._resolve(identity)
                except RecordError as exc:
                    if exc.code != "RECORD_NOT_FOUND":
                        raise
                samples.append((time.perf_counter_ns() - started) / 1e6)
            results[identity] = {"median_ms": statistics.median(samples), "p95_ms": sorted(samples)[94]}
        print(json.dumps({"hot_records": 200, "reads_per_identity": 100, "results": results}, indent=2))


if __name__ == "__main__":
    main()
