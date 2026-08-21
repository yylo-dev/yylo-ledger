import json
from pathlib import Path

from kanban.cli import ExitCode, main


def make_source(tmp_path: Path, enabled=True, allowed=None) -> Path:
    root = tmp_path / "source"
    (root / ".juno_task").mkdir(parents=True)
    (root / ".juno_task/config.json").write_text(json.dumps({
        "kanbanRegistry": {
            "enabled": enabled,
            "allowedProjects": allowed or [],
        }
    }), encoding="utf-8")
    return root


def make_target(tmp_path: Path) -> Path:
    root = tmp_path / "target"
    wrapper = root / ".juno_task/scripts/kanban.sh"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    wrapper.chmod(0o755)
    return root


def test_project_management_requires_opt_in_and_does_not_initialize_task_storage(
    tmp_path, monkeypatch, capsys
):
    source = make_source(tmp_path, enabled=False)
    monkeypatch.chdir(source)
    monkeypatch.setenv("YYLO_LEDGER_REGISTRY_PATH", str(tmp_path / "registry.json"))

    assert main(["project", "status"]) == ExitCode.SUCCESS
    assert json.loads(capsys.readouterr().out) == {"enabled": False, "source": "project-config"}
    assert main(["project", "list"]) == ExitCode.CONFIG_ERROR
    assert "disabled" in capsys.readouterr().err
    assert not (source / ".juno_task/tasks").exists()


def test_project_add_list_show_remove_contract(tmp_path, monkeypatch, capsys):
    source = make_source(tmp_path, enabled=True, allowed=["target"])
    target = make_target(tmp_path)
    monkeypatch.chdir(source)
    monkeypatch.setenv("YYLO_LEDGER_REGISTRY_PATH", str(tmp_path / "registry.json"))

    assert main(["project", "add", "target", "--path", str(target)]) == 0
    added = json.loads(capsys.readouterr().out)
    assert added["alias"] == "target"
    assert added["path"] == str(target.resolve())

    assert main(["project", "list"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["alias"] == "target"
    assert main(["project", "show", "target"]) == 0
    assert json.loads(capsys.readouterr().out)["path"] == str(target.resolve())

    assert main(["project", "remove", "target"]) == 0
    assert json.loads(capsys.readouterr().out)["alias"] == "target"
    assert main(["project", "show", "target"]) == ExitCode.CONFIG_ERROR
    assert "not registered" in capsys.readouterr().err
    assert not (source / ".juno_task/tasks").exists()


def test_global_project_option_rejects_duplicate_without_consuming_stdin(
    tmp_path, monkeypatch, capsys
):
    source = make_source(tmp_path, enabled=True, allowed=["target"])
    monkeypatch.chdir(source)
    assert main(["--project", "target", "--project=target", "list"]) == ExitCode.CONFIG_ERROR
    assert "only once" in capsys.readouterr().err
