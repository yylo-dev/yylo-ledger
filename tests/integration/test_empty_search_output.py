"""Empty legacy collections retain their selected wire format, including the wrapper."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

from yylo_ledger.cli import OutputFormatter
from yylo_ledger.config import Config
from yylo_ledger.storage import TaskStorage


@pytest.fixture
def collection(tmp_path):
    root = tmp_path / "controller"
    task_dir = root / ".juno_task/tasks"
    task_dir.mkdir(parents=True)
    config_path = root / ".juno_task/config.json"
    config_data = json.loads(json.dumps(Config.DEFAULT_CONFIG))
    config_data["storage"]["base_path"] = str(task_dir)
    config_path.write_text(json.dumps(config_data))
    storage = TaskStorage(Config(config_path=str(config_path)))
    task = storage.create_task(body="present task", status="todo")
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("JUNO_", "YYLO_", "PYTHON"))
           and key != "VIRTUAL_ENV"}
    src = Path(__file__).resolve().parents[2] / "src"
    env.update(JUNO_TASK_ROOT=str(root), PYTHONPATH=str(src))
    return root, config_path, env, task


@pytest.fixture(params=["direct", "wrapper"])
def command(request, collection):
    root, config, env, _ = collection
    if request.param == "direct":
        return [sys.executable, "-c", "from yylo_ledger.cli import main; raise SystemExit(main())",
                "--config", str(config)]
    # Exercise the real canonical wrapper/resolver/policy against disposable
    # storage and this checkout's CLI, never the live installed controller.
    templates = Path(__file__).resolve().parents[3] / "juno-code/src/templates/scripts"
    if not templates.is_dir():
        pytest.skip("canonical wrapper integration requires the monorepo")
    scripts = root / ".juno_task/scripts"
    scripts.mkdir()
    for name in ("kanban.sh", "controller_resolver.py", "juno-toolchain-policy.sh"):
        shutil.copyfile(templates / name, scripts / name)
    binary = root / ".venv_juno/bin"
    binary.mkdir(parents=True)
    (binary / "activate").write_text(
        f"export VIRTUAL_ENV={shlex.quote(str(binary.parent))}\n")
    executable = binary / "yylo-ledger"
    executable.write_text(
        f"#!{sys.executable}\nfrom yylo_ledger.cli import main\nraise SystemExit(main())\n")
    executable.chmod(0o755)
    return ["bash", str(scripts / "kanban.sh")]


def run(command, collection, args):
    root, _, env, _ = collection
    return subprocess.run(command + args, cwd=root, env=env, text=True,
                          capture_output=True, stdin=subprocess.DEVNULL, timeout=30)


@pytest.mark.parametrize("flags,expected", [
    (["-f", "json"], "[]\n"),
    (["-f", "json", "--raw"], "[]\n"),
    (["--format", "json"], "[]\n"),
    (["-f", "ndjson"], ""),
    (["-f", "xml"], '<?xml version="1.0" encoding="UTF-8"?>\n<tasks>\n</tasks>\n'),
    (["-f", "table"], "No results found\n"),
    (["--pretty"], "No results found\n"),
    ([], "[]\n"),
])
def test_empty_search_formats(command, collection, flags, expected):
    result = run(command, collection, flags + ["search", "--body", "absent phrase", "--limit", "50"])
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout == expected
    if expected == "[]\n":
        assert json.loads(result.stdout) == []
    elif "<tasks>" in expected:
        assert list(ET.fromstring(result.stdout)) == []


@pytest.mark.parametrize("operation", ["list", "search", "ready"])
def test_empty_shared_collections(command, collection, operation):
    result = run(command, collection, ["-f", "json", operation, "--status", "done"])
    assert (result.returncode, result.stdout, result.stderr) == (0, "[]\n", "")


@pytest.mark.parametrize("output_format", ["json", "ndjson", "xml", "table"])
def test_nonempty_payload_unchanged(command, collection, output_format):
    result = run(command, collection, ["-f", output_format, "search", "--body", "present task"])
    assert result.returncode == 0, result.stderr
    assert "No results found" not in result.stdout
    # Existing summaries remain separate from the task payload.
    if output_format == "json":
        payload, _ = json.JSONDecoder().raw_decode(result.stdout)
        assert payload[0]["id"] == collection[3].id
    elif output_format == "ndjson":
        assert json.loads(result.stdout.splitlines()[0])["id"] == collection[3].id
    elif output_format == "xml":
        assert ET.fromstring(result.stdout).find("task/id").text == collection[3].id
    else:
        assert collection[3].id in result.stdout


@pytest.mark.parametrize("output_format,expected", [
    ("json", "[]"), ("ndjson", ""), ("table", ""), ("pretty", ""),
    ("xml", '<?xml version="1.0" encoding="UTF-8"?>\n<tasks>\n</tasks>'),
])
def test_empty_formatter(output_format, expected):
    assert OutputFormatter.format_tasks([], output_format) == expected
