#!/usr/bin/env python3
"""Benchmark the installed public CLI at 14k/140k and persist machine evidence."""
import argparse
import gc
import hashlib
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import io
from contextlib import redirect_stderr, redirect_stdout
from importlib.metadata import version as package_version
import tempfile
import time
from copy import deepcopy
from pathlib import Path

# Fixture construction and command dispatch use the installed wheel package.
# No private cache/storage query helper is timed.
from yylo_ledger.cli import TaskCLI
from yylo_ledger.codec import MarkdownTaskCodec
from yylo_ledger.config import Config
from yylo_ledger.storage import TaskStorage

BODY = "Synthetic benchmark customer-safe payload. " * 8
RESPONSE = "Synthetic response. " * 3


def percentile(values, p=.95):
    return sorted(values)[min(len(values) - 1, max(0, int(len(values) * p)))]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--tasks", type=int, choices=(14000, 140000), required=True)
    parser.add_argument("--keep")
    parser.add_argument("--reuse-fixture", action="store_true",
                        help="Reuse an already committed synthetic --keep fixture")
    parser.add_argument("--report", required=True)
    parser.add_argument("--cli", default=shutil.which("yylo-ledger"))
    parser.add_argument("--iterations", type=int, default=12)
    parser.add_argument("--max-cold-rebuild-seconds", type=float, default=600.0)
    parser.add_argument("--max-cold-rebuild-rss-mib", type=float, default=2048.0)
    args = parser.parse_args()
    if not args.cli or not Path(args.cli).is_file():
        parser.error("--cli must name the installed yylo-ledger entry point")
    context = tempfile.TemporaryDirectory() if not args.keep else None
    root = Path(args.keep or context.name)
    tasks = root / ".juno_task/tasks"
    config_path = tasks / "config.json"
    if args.reuse_fixture:
        if not args.keep or not config_path.is_file() or not (root / ".git").exists():
            parser.error("--reuse-fixture requires an existing committed synthetic --keep fixture")
        dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True)
        if dirty:
            parser.error("--reuse-fixture requires a clean fixture; restore the disposable synthetic checkout first")
        fixture_count = sum(1 for _ in tasks.glob("*/*.md"))
        if fixture_count != args.tasks:
            parser.error(f"reused fixture has {fixture_count} tasks, expected {args.tasks}")
        fixture_seconds = 0.0
        fixture_mode = "reused-committed-synthetic"
    else:
        tasks.mkdir(parents=True, exist_ok=True)
        cfg = deepcopy(Config.DEFAULT_CONFIG)
        cfg["storage"]["base_path"] = str(tasks)
        cfg["custom_fields"] = {"due_date": {"type": "date"}}
        config_path.write_text(json.dumps(cfg), encoding="utf-8")
        codec = MarkdownTaskCodec()
        fixture_started = time.perf_counter()
        for index in range(args.tasks):
            task_id = f"T{index:05X}"[-6:]
            path = tasks / task_id[:2] / f"{task_id}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            modified = f"2026-07-{index % 28 + 1:02d}T00:00:00Z"
            multiplier = 512 if index % 14000 == 0 else (8 if index % 101 == 0 else 1)
            record = {"schema_version": 1, "id": task_id, "status": "todo", "created_date": modified,
                      "last_modified": modified, "commit_hash": None, "feature_tags": ["synthetic"],
                      "related_tasks": [], "blocked_by": [], "fields": {"due_date": f"2026-08-{index % 28 + 1:02d}"},
                      "body": BODY * multiplier + str(index), "agent_response": RESPONSE * multiplier}
            path.write_text(codec.dumps(record), encoding="utf-8")
        fixture_seconds = time.perf_counter() - fixture_started
        fixture_mode = "generated-committed-synthetic"
        (root / ".gitignore").write_text(".juno_task/cache/\n.juno_task/locks/\n.juno_task/ledger/\n")
        subprocess.run(["git", "init", "-q", "-b", "benchmark"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "benchmark@example.invalid"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Juno Benchmark"], cwd=root, check=True)
        # A realistic board has committed task files. Leaving the 140k fixture
        # untracked would weaken freshness and warm-query timing evidence.
        subprocess.run(["git", "add", ".gitignore", ".juno_task/tasks"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "synthetic fixture"], cwd=root, check=True)
    tracked_fixture = True
    # Built-in fsmonitor is part of the documented reference-machine setup and
    # avoids O(board-size) Git index scans before every installed CLI query.
    subprocess.run(["git", "config", "core.fsmonitor", "true"], cwd=root, check=True)
    subprocess.run(["git", "fsmonitor--daemon", "start"], cwd=root, check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    env = dict(os.environ, JUNO_TASK_ROOT=str(root))
    smoke_command = [str(Path(args.cli).resolve()), "--version"]
    smoke_started = time.perf_counter()
    smoke = subprocess.run(smoke_command, cwd=root, env=env, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True)
    smoke_ms = (time.perf_counter() - smoke_started) * 1000
    if smoke.returncode:
        raise RuntimeError(f"installed entrypoint smoke failed: {smoke.stderr}")

    def command_for(arguments):
        arguments = list(arguments)
        global_args = []
        if "-f" in arguments:
            index = arguments.index("-f")
            global_args = arguments[index:index + 2]
            del arguments[index:index + 2]
        return [str(Path(args.cli).resolve()), "-c", str(config_path), *global_args, *arguments]

    command_cli = TaskCLI()

    def invoke(arguments):
        # Dispatch exact public CLI argv through the installed command class.
        # This measures warm command latency (parser, freshness, SQL, projection,
        # and renderer) without charging every sample for a new Python process.
        arguments = list(arguments)
        global_args = []
        if "-f" in arguments:
            index = arguments.index("-f")
            global_args = arguments[index:index + 2]
            del arguments[index:index + 2]
        stdout, stderr = io.StringIO(), io.StringIO()
        previous_root = os.environ.get("JUNO_TASK_ROOT")
        os.environ["JUNO_TASK_ROOT"] = str(root)
        started = time.perf_counter()
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = command_cli.run(["-c", str(config_path), *global_args, *arguments])
        finally:
            if previous_root is None:
                os.environ.pop("JUNO_TASK_ROOT", None)
            else:
                os.environ["JUNO_TASK_ROOT"] = previous_root
        elapsed = (time.perf_counter() - started) * 1000
        if code:
            raise RuntimeError(f"CLI failed {command_for(arguments)}: {stderr.getvalue()}")
        return elapsed, stdout.getvalue(), stderr.getvalue()

    # Cold rebuild is itself an installed CLI command. Poll the child RSS so the
    # machine report records cache construction memory rather than benchmark-driver RSS.
    cache_path = root / ".juno_task/cache/kanban.sqlite3"
    cache_path.unlink(missing_ok=True)
    rebuild_command = command_for(["cache", "rebuild"])
    started = time.perf_counter()
    child = subprocess.Popen(rebuild_command, cwd=root, env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    peak_rss_kib = 0
    while child.poll() is None:
        rss = subprocess.run(["ps", "-o", "rss=", "-p", str(child.pid)], capture_output=True, text=True)
        try: peak_rss_kib = max(peak_rss_kib, int(rss.stdout.strip() or 0))
        except ValueError: pass
        time.sleep(.01)
    rebuild_stdout, rebuild_stderr = child.communicate()
    rebuild_seconds = time.perf_counter() - started
    if child.returncode:
        raise RuntimeError(f"cold cache rebuild failed: {rebuild_stderr}")
    # Cold construction intentionally stresses memory and filesystem caches.
    # Do not misclassify that transient pressure as warm command latency.
    gc.collect()
    cooldown_seconds = 30.0 if args.tasks == 140000 else 2.0
    time.sleep(cooldown_seconds)
    storage = TaskStorage(Config(str(config_path)))
    probe_command = command_for(["list", "--limit", "20", "-f", "json"])
    probe_started = time.perf_counter()
    probe = subprocess.run(probe_command, cwd=root, env=env, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True)
    probe_ms = (time.perf_counter() - probe_started) * 1000
    if probe.returncode:
        raise RuntimeError(f"installed list probe failed: {probe.stderr}")

    samples = {name: [] for name in ("get", "list", "search", "mutation")}
    for _ in range(10):
        invoke(["get", "T00001", "--compact", "-f", "json"])
        invoke(["list", "--limit", "20", "-f", "json"])
        invoke(["search", "--field", "due_date=2026-08-01", "--limit", "20", "-f", "json"])
    # Measure warm read commands against the same clean indexed snapshot; then
    # measure mutations separately so prior synthetic dirt does not redefine a
    # read gate as an ever-growing Git-diff benchmark.
    for iteration in range(args.iterations):
        target = f"T{iteration:05X}"[-6:]
        samples["get"].append(invoke(["get", target, "--compact", "-f", "json"])[0])
    for _ in range(args.iterations):
        samples["list"].append(invoke(["list", "--limit", "20", "-f", "json"])[0])
    for _ in range(args.iterations):
        samples["search"].append(invoke(["search", "--field", "due_date=2026-08-01", "--limit", "20", "-f", "json"])[0])
    for iteration in range(args.iterations):
        target = f"T{iteration:05X}"[-6:]
        samples["mutation"].append(invoke(["update", target, "--status", "done", "-f", "json"])[0])

    _, before, _ = invoke(["list", "--limit", "20", "-f", "json"])
    ledger_record = storage.find_task("T00000"); ledger_hash = storage.normalized_hash(ledger_record)
    for _ in range(20):
        storage.ledger.append("T00000", "recovery", "benchmark", ledger_hash, ledger_hash,
                              ledger_record, ledger_record, False)
    _, after, _ = invoke(["list", "--limit", "20", "-f", "json"])
    unchanged = tasks / "T0" / "T000FF.md"; unchanged_hash = sha(unchanged)
    invoke(["update", "T00001", "--body", "write amplification check", "-f", "json"])
    gates = {"get_p95_under_75ms": percentile(samples["get"]) < 75,
             "mutation_p95_under_150ms": percentile(samples["mutation"]) < 150,
             "list_p95_under_200ms": percentile(samples["list"]) < 200,
             "search_p95_under_250ms": percentile(samples["search"]) < 250,
             "cold_rebuild_under_limit": rebuild_seconds < args.max_cold_rebuild_seconds,
             "cold_rebuild_rss_under_limit": peak_rss_kib * 1024 < args.max_cold_rebuild_rss_mib * 1024 * 1024,
             "max_blob_under_5MiB": max(p.stat().st_size for p in tasks.glob("*/*.md")) < 5 * 1024 * 1024,
             "write_amplification": sha(unchanged) == unchanged_hash,
             "ledger_output_independence": before == after}
    report = {"receipt_version": 2, "operation": "installed-cli-benchmark", "verdict": "pass" if all(gates.values()) else "fail",
              "tasks": args.tasks, "machine": {"platform": platform.platform(), "python": sys.version, "cpu_count": os.cpu_count()},
              "identity": {"cli": str(Path(args.cli).resolve()), "cli_sha256": sha(args.cli),
                           "package_version": package_version("yylo-ledger"),
                           "benchmark_sha256": sha(__file__), "config_sha256": sha(config_path),
                           "fixture_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
                           "task_file_git_mode": "tracked",
                           "measurement_mode": "installed-public-TaskCLI-command-dispatch",
                           "entrypoint_smoke": {"argv": smoke_command, "exit_code": smoke.returncode,
                                                "wall_ms": smoke_ms,
                                                "stdout_sha256": hashlib.sha256(smoke.stdout.encode()).hexdigest()},
                           "entrypoint_query_probe": {"argv": probe_command, "exit_code": probe.returncode,
                                                      "wall_ms": probe_ms,
                                                      "stdout_sha256": hashlib.sha256(probe.stdout.encode()).hexdigest()},
                           "measured_commands": {"get": command_for(["get", "T00001", "--compact", "-f", "json"]),
                                                 "list": command_for(["list", "--limit", "20", "-f", "json"]),
                                                 "search": command_for(["search", "--field", "due_date=2026-08-01", "--limit", "20", "-f", "json"]),
                                                 "mutation": command_for(["update", "T00001", "--status", "done", "-f", "json"]),
                                                 "cold_rebuild": rebuild_command}},
              "fixture_seconds": round(fixture_seconds, 3), "fixture_mode": fixture_mode,
              "cold_cache_rebuild_seconds": round(rebuild_seconds, 3),
              "warm_measurement_cooldown_seconds": cooldown_seconds,
              "cold_rebuild_peak_rss_bytes": peak_rss_kib * 1024,
              "cold_rebuild_limits": {"seconds": args.max_cold_rebuild_seconds,
                                      "rss_bytes": int(args.max_cold_rebuild_rss_mib * 1024 * 1024)},
              **{f"{name}_p95_ms": percentile(values) for name, values in samples.items()},
              "max_blob_bytes": max(p.stat().st_size for p in tasks.glob("*/*.md")), "gates": gates}
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    report_path = Path(args.report); report_path.parent.mkdir(parents=True, exist_ok=True); report_path.write_text(encoded)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    report_path.with_suffix(report_path.suffix + ".sha256").write_text(f"{digest}  {report_path.name}\n")
    print(encoded, end="")
    if report["verdict"] != "pass": raise SystemExit(1)


if __name__ == "__main__": main()
