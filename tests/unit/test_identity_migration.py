from pathlib import Path

from yylo_ledger.identity import migrate_environment, migrate_user_home


def test_legacy_environment_maps_without_overriding_canonical():
    env = {"JUNO_KANBAN_LOCK_TIMEOUT_SECONDS": "7", "YYLO_LEDGER_LOCK_TIMEOUT_SECONDS": "9"}
    migrate_environment(env)
    assert env["YYLO_LEDGER_LOCK_TIMEOUT_SECONDS"] == "9"
    env = {"JUNO_KANBAN_LOCK_TIMEOUT_SECONDS": "7"}
    migrate_environment(env)
    assert env["YYLO_LEDGER_LOCK_TIMEOUT_SECONDS"] == "7"


def test_legacy_user_state_copy_is_lossless_idempotent_and_rollback_safe(tmp_path: Path):
    legacy = tmp_path / ".juno-kanban"
    legacy.mkdir()
    (legacy / "projects.json").write_bytes(b'{"schema_version":1}\n')
    canonical = migrate_user_home(tmp_path)
    assert canonical == tmp_path / ".yylo-ledger"
    assert (canonical / "projects.json").read_bytes() == (legacy / "projects.json").read_bytes()
    (canonical / "projects.json").write_text("canonical\n")
    assert migrate_user_home(tmp_path) == canonical
    assert (canonical / "projects.json").read_text() == "canonical\n"
    assert (legacy / "projects.json").read_bytes() == b'{"schema_version":1}\n'
