import json
import os
import subprocess
import sys
from pathlib import Path

from kanban.project_registry import ProjectRegistry


def test_two_project_route_preserves_stdin_and_sanitizes_source_state(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "destination with spaces"
    (source / ".juno_task").mkdir(parents=True)
    (source / ".juno_task/config.json").write_text(json.dumps({
        "kanbanRegistry": {"enabled": True, "allowedProjects": ["destination"]}
    }), encoding="utf-8")

    wrapper = target / ".juno_task/scripts/kanban.sh"
    wrapper.parent.mkdir(parents=True)
    args_file = target / "args.json"
    env_file = target / "environment.json"
    stdin_file = target / "stdin.bin"
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        f"pathlib.Path({str(args_file)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        f"pathlib.Path({str(env_file)!r}).write_text(json.dumps({{k: os.environ.get(k) for k in "
        "['JUNO_TASK_ROOT','JUNO_CONTROLLER_BRANCH','JUNO_WORKSPACE_ROLE','VIRTUAL_ENV',"
        "'PYTHONPATH','YYLO_LEDGER_REGISTRY_HOP','YYLO_LEDGER_INVOCATION_ROOT']}))\n"
        f"pathlib.Path({str(stdin_file)!r}).write_bytes(sys.stdin.buffer.read())\n"
        "print('destination-wrapper')\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    registry_path = tmp_path / "projects.json"
    ProjectRegistry(registry_path).add("destination", target)

    environment = dict(os.environ)
    environment.update({
        "YYLO_LEDGER_REGISTRY_PATH": str(registry_path),
        "JUNO_TASK_ROOT": str(source),
        "JUNO_CONTROLLER_BRANCH": "source-branch",
        "JUNO_WORKSPACE_ROLE": "controller",
        "VIRTUAL_ENV": str(source / ".venv_juno"),
        "PYTHONPATH": str(Path(__file__).parents[2] / "src"),
    })
    payload = b"line one\n$VARIABLE and `ticks`\nline three\n"
    command = [
        sys.executable,
        "-c",
        "import sys; from kanban.cli import main; sys.exit(main())",
        "--project", "destination", "create", "--body-file", "-",
    ]
    result = subprocess.run(
        command, cwd=source, env=environment, input=payload, capture_output=True
    )
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == b"destination-wrapper\n"
    assert json.loads(args_file.read_text()) == ["create", "--body-file", "-"]
    assert stdin_file.read_bytes() == payload
    routed_env = json.loads(env_file.read_text())
    for key in ("JUNO_TASK_ROOT", "JUNO_CONTROLLER_BRANCH", "JUNO_WORKSPACE_ROLE", "VIRTUAL_ENV", "PYTHONPATH"):
        assert routed_env[key] is None
    assert routed_env["YYLO_LEDGER_REGISTRY_HOP"] == "1"
    assert routed_env["YYLO_LEDGER_INVOCATION_ROOT"] == str(target.resolve())
