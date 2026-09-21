"""Tests for kanban.models module.

Why: Task is the core data model - every operation flows through it. Bugs here corrupt
task data, break serialization, or silently drop fields. The parse_related_task_ids
function is the inter-task linking mechanism used by the kanban CLI's [task_id] format.
"""

import json
import pytest
from kanban.models import Task, parse_related_task_ids, parse_blocked_by_ids, validate_task_ids
from kanban.validators import ValidationError


class TestTaskCreation:
    """Task construction and auto-generation."""

    def test_creates_with_defaults(self):
        task = Task(body="Test task")
        assert task.body == "Test task"
        assert task.status == "backlog"
        assert task.id.startswith('task_') and len(task.id) == 11
        assert task.created_date is not None
        assert task.last_modified is not None
        assert task.commit_hash is None
        assert task.agent_response == ""

    def test_creates_with_explicit_id(self):
        task = Task(id="Ab3cD4", body="Test")
        assert task.id == "Ab3cD4"

    def test_auto_generated_id_format(self):
        """Generated IDs must have both letters and digits."""
        for _ in range(20):
            task = Task(body="Test")
            assert task.id.startswith('task_')
            suffix = task.id.removeprefix('task_')
            assert len(suffix) == 6 and suffix.isalnum()
            assert not suffix.isdigit(), "suffix should not be all digits"
            assert not suffix.isalpha(), "suffix should not be all letters"

    def test_creates_with_all_fields(self):
        task = Task(
            id="Tx1yZ2",
            status="todo",
            body="Full task",
            commit_hash="abc1234",
            agent_response="Done",
            created_date="2026-02-18 10:00:00",
            last_modified="2026-02-18 11:00:00",
            feature_tags=["test", "urgent"],
            related_tasks=["Ab3cD4"],
            blocked_by=["Xy1zW2"],
        )
        assert task.status == "todo"
        assert task.commit_hash == "abc1234"
        assert task.feature_tags == ["test", "urgent"]
        assert task.related_tasks == ["Ab3cD4"]
        assert task.blocked_by == ["Xy1zW2"]

    def test_creates_with_blocked_by(self):
        task = Task(body="Test", blocked_by=["Ab3cD4"])
        assert task.blocked_by == ["Ab3cD4"]

    def test_creates_without_blocked_by(self):
        task = Task(body="Test")
        assert task.blocked_by is None

    def test_validation_on_create(self):
        with pytest.raises(ValidationError):
            Task(id="!!!!!!", body="Bad ID")

    def test_skip_validation(self):
        task = Task(id="!!!!!!", body="Bad ID", validate=False)
        assert task.id == "!!!!!!"


class TestTaskSerialization:
    """to_dict, to_ndjson, from_dict, from_ndjson round-trips."""

    def test_to_dict(self):
        task = Task(id="Ab3cD4", body="Test", created_date="2026-02-18 10:00:00",
                    last_modified="2026-02-18 10:00:00")
        d = task.to_dict()
        assert d["id"] == "Ab3cD4"
        assert d["body"] == "Test"
        assert d["status"] == "backlog"
        assert "created_date" in d
        assert "last_modified" in d

    def test_to_ndjson_valid_json(self):
        task = Task(body="Test")
        ndjson = task.to_ndjson()
        parsed = json.loads(ndjson)
        assert parsed["body"] == "Test"
        assert "\n" not in ndjson

    def test_to_ndjson_unicode(self):
        task = Task(body="Unicode: 日本語 🚀")
        ndjson = task.to_ndjson()
        parsed = json.loads(ndjson)
        assert "日本語" in parsed["body"]

    def test_from_dict(self):
        d = {
            "id": "Ab3cD4",
            "status": "todo",
            "body": "From dict",
            "created_date": "2026-02-18 10:00:00",
            "last_modified": "2026-02-18 10:00:00",
        }
        task = Task.from_dict(d)
        assert task.id == "Ab3cD4"
        assert task.body == "From dict"

    def test_from_ndjson(self):
        line = '{"id":"Ab3cD4","status":"todo","body":"From NDJSON","created_date":"2026-02-18 10:00:00","last_modified":"2026-02-18 10:00:00"}'
        task = Task.from_ndjson(line)
        assert task.id == "Ab3cD4"
        assert task.body == "From NDJSON"

    def test_from_ndjson_invalid_json(self):
        with pytest.raises(json.JSONDecodeError):
            Task.from_ndjson("not json")

    def test_round_trip(self):
        """dict -> Task -> dict should preserve all fields."""
        original = Task(body="Round trip", feature_tags=["test"], status="todo")
        d = original.to_dict()
        restored = Task.from_dict(d)
        assert original.id == restored.id
        assert original.body == restored.body
        assert original.feature_tags == restored.feature_tags

    def test_ndjson_round_trip(self):
        """Task -> ndjson -> Task should preserve data."""
        original = Task(body="NDJSON trip", feature_tags=["tag1"])
        ndjson = original.to_ndjson()
        restored = Task.from_ndjson(ndjson)
        assert original.id == restored.id
        assert original.body == restored.body

    def test_to_dict_includes_blocked_by(self):
        task = Task(body="Test", blocked_by=["Ab3cD4"])
        d = task.to_dict()
        assert d["blocked_by"] == ["Ab3cD4"]

    def test_to_dict_blocked_by_none(self):
        task = Task(body="Test")
        d = task.to_dict()
        assert d["blocked_by"] is None

    def test_from_dict_with_blocked_by(self):
        d = {
            "id": "Ab3cD4", "status": "todo", "body": "Test",
            "created_date": "2026-02-18 10:00:00",
            "last_modified": "2026-02-18 10:00:00",
            "blocked_by": ["Xy1zW2"],
        }
        task = Task.from_dict(d)
        assert task.blocked_by == ["Xy1zW2"]

    def test_from_dict_without_blocked_by(self):
        """Old tasks without blocked_by field should load fine."""
        d = {
            "id": "Ab3cD4", "status": "todo", "body": "Old task",
            "created_date": "2026-02-18 10:00:00",
            "last_modified": "2026-02-18 10:00:00",
        }
        task = Task.from_dict(d)
        assert task.blocked_by is None

    def test_blocked_by_round_trip(self):
        """blocked_by should survive dict round-trip."""
        original = Task(body="Test", blocked_by=["Ab3cD4", "Xy1zW2"])
        d = original.to_dict()
        restored = Task.from_dict(d)
        assert restored.blocked_by == ["Ab3cD4", "Xy1zW2"]

    def test_blocked_by_ndjson_round_trip(self):
        """blocked_by should survive NDJSON round-trip."""
        original = Task(body="Test", blocked_by=["Ab3cD4"])
        ndjson = original.to_ndjson()
        restored = Task.from_ndjson(ndjson)
        assert restored.blocked_by == ["Ab3cD4"]


class TestTaskUpdate:
    """Task.update() with validation."""

    def test_update_status(self):
        task = Task(body="Test")
        old_modified = task.last_modified
        task.update(status="todo")
        assert task.status == "todo"

    def test_update_agent_response(self):
        task = Task(body="Test")
        task.update(agent_response="Completed")
        assert task.agent_response == "Completed"

    def test_update_commit_hash(self):
        task = Task(body="Test")
        task.update(commit_hash="abc1234")
        assert task.commit_hash == "abc1234"

    def test_update_feature_tags(self):
        task = Task(body="Test")
        task.update(feature_tags=["new-tag"])
        assert task.feature_tags == ["new-tag"]

    def test_update_ignores_unknown_fields(self):
        task = Task(body="Test")
        original_id = task.id
        task.update(id="XXXXXX", unknown_field="ignored")
        assert task.id == original_id  # 'id' is not in allowed_updates

    def test_update_validates(self):
        task = Task(body="Test")
        with pytest.raises(ValidationError):
            task.update(status="nonexistent_status")

    def test_update_with_transition_enforcement(self):
        config = {
            "status_workflow": {
                "enforce_transitions": True,
                "transitions": {"backlog": ["todo"]},
                "values": ["backlog", "todo", "done"],
            }
        }
        task = Task(body="Test", status="backlog",
                    created_date="2026-02-18 10:00:00",
                    last_modified="2026-02-18 10:00:00",
                    config=config)
        task.update(config=config, status="todo")
        assert task.status == "todo"

    def test_update_blocked_by(self):
        task = Task(body="Test")
        task.update(blocked_by=["Ab3cD4"])
        assert task.blocked_by == ["Ab3cD4"]

    def test_update_blocked_by_to_none(self):
        task = Task(body="Test", blocked_by=["Ab3cD4"])
        task.update(blocked_by=None)
        assert task.blocked_by is None

    def test_update_rejects_invalid_transition(self):
        config = {
            "status_workflow": {
                "enforce_transitions": True,
                "transitions": {"backlog": ["todo"]},
                "values": ["backlog", "todo", "done"],
            }
        }
        task = Task(body="Test", status="backlog",
                    created_date="2026-02-18 10:00:00",
                    last_modified="2026-02-18 10:00:00",
                    config=config)
        with pytest.raises(ValidationError):
            task.update(config=config, status="done")


class TestTaskHelpers:
    """is_open, has_tag, add_tag, remove_tag, age_days."""

    def test_is_open_no_response(self):
        task = Task(body="Test")
        assert task.is_open() is True

    def test_is_open_empty_response(self):
        task = Task(body="Test", agent_response="")
        assert task.is_open() is True

    def test_is_open_whitespace_response(self):
        task = Task(body="Test", agent_response="   ")
        assert task.is_open() is True

    def test_is_not_open_with_response(self):
        task = Task(body="Test", agent_response="Done")
        assert task.is_open() is False

    def test_has_tag(self):
        task = Task(body="Test", feature_tags=["urgent"])
        assert task.has_tag("urgent") is True
        assert task.has_tag("missing") is False

    def test_has_tag_no_tags(self):
        task = Task(body="Test", feature_tags=None)
        assert task.has_tag("anything") is False

    def test_add_tag(self):
        task = Task(body="Test", feature_tags=["existing"])
        task.add_tag("new")
        assert "new" in task.feature_tags

    def test_add_tag_no_duplicate(self):
        task = Task(body="Test", feature_tags=["existing"])
        task.add_tag("existing")
        assert task.feature_tags.count("existing") == 1

    def test_add_tag_initializes_list(self):
        task = Task(body="Test", feature_tags=None)
        task.add_tag("first")
        assert task.feature_tags == ["first"]

    def test_add_invalid_tag_rolls_back(self):
        task = Task(body="Test", feature_tags=["existing"])
        with pytest.raises(ValidationError):
            task.add_tag("invalid tag with spaces")
        assert "invalid tag with spaces" not in task.feature_tags

    def test_remove_tag(self):
        task = Task(body="Test", feature_tags=["a", "b"])
        task.remove_tag("a")
        assert "a" not in task.feature_tags
        assert "b" in task.feature_tags

    def test_remove_missing_tag_noop(self):
        task = Task(body="Test", feature_tags=["a"])
        task.remove_tag("nonexistent")
        assert task.feature_tags == ["a"]

    def test_remove_tag_none_tags(self):
        task = Task(body="Test", feature_tags=None)
        task.remove_tag("anything")  # Should not raise


class TestTaskEquality:
    """__eq__ and __hash__ based on ID."""

    def test_equal_by_id(self):
        t1 = Task(id="Ab3cD4", body="One")
        t2 = Task(id="Ab3cD4", body="Two")
        assert t1 == t2

    def test_not_equal_different_id(self):
        t1 = Task(body="One")
        t2 = Task(body="Two")
        assert t1 != t2

    def test_not_equal_non_task(self):
        t1 = Task(body="One")
        assert t1 != "not a task"

    def test_hash_same_for_same_id(self):
        t1 = Task(id="Ab3cD4", body="One")
        t2 = Task(id="Ab3cD4", body="Two")
        assert hash(t1) == hash(t2)

    def test_usable_in_set(self):
        t1 = Task(id="Ab3cD4", body="One")
        t2 = Task(id="Ab3cD4", body="Two")
        s = {t1, t2}
        assert len(s) == 1


class TestTaskRepr:
    """String representations."""

    def test_repr(self):
        task = Task(id="Ab3cD4", body="Test task")
        r = repr(task)
        assert "Ab3cD4" in r
        assert "backlog" in r

    def test_str(self):
        task = Task(id="Ab3cD4", body="Test task", status="todo")
        s = str(task)
        assert "Ab3cD4" in s
        assert "todo" in s

    def test_repr_truncates_long_body(self):
        task = Task(body="x" * 100)
        r = repr(task)
        assert "..." in r


class TestParseRelatedTaskIds:
    """Parsing [task_id]...[/task_id] from body text."""

    def test_single_id(self):
        body = "Related to [task_id]Ab3cD4[/task_id]"
        assert parse_related_task_ids(body) == ["Ab3cD4"]

    def test_short_close_tag(self):
        body = "Related to [task_id]Ab3cD4[/]"
        assert parse_related_task_ids(body) == ["Ab3cD4"]

    def test_comma_separated(self):
        body = "[task_id]Ab3cD4, Xy1zW2[/task_id]"
        ids = parse_related_task_ids(body)
        assert "Ab3cD4" in ids
        assert "Xy1zW2" in ids

    def test_space_separated(self):
        body = "[task_id]Ab3cD4 Xy1zW2[/]"
        ids = parse_related_task_ids(body)
        assert "Ab3cD4" in ids
        assert "Xy1zW2" in ids

    def test_multiple_tags(self):
        body = "[task_id]Ab3cD4[/task_id] and [task_id]Xy1zW2[/]"
        ids = parse_related_task_ids(body)
        assert ids == ["Ab3cD4", "Xy1zW2"]

    def test_deduplicates(self):
        body = "[task_id]Ab3cD4[/] and [task_id]Ab3cD4[/]"
        ids = parse_related_task_ids(body)
        assert ids == ["Ab3cD4"]

    def test_empty_body(self):
        assert parse_related_task_ids("") == []

    def test_none_body(self):
        assert parse_related_task_ids(None) == []

    def test_no_tags(self):
        assert parse_related_task_ids("Just normal text") == []

    def test_case_insensitive_tags(self):
        body = "[TASK_ID]Ab3cD4[/TASK_ID]"
        assert parse_related_task_ids(body) == ["Ab3cD4"]

    def test_hash_marker_single_id_with_space(self):
        body = "Need follow-up ## Ab3cD4"
        assert parse_related_task_ids(body) == ["Ab3cD4"]

    def test_hash_marker_single_id_without_space(self):
        body = "Need follow-up ##Ab3cD4"
        assert parse_related_task_ids(body) == ["Ab3cD4"]

    def test_hash_marker_block_multiple_ids(self):
        body = "Depends on ## {Ab3cD4} Xy1zW2 ## before merge"
        assert parse_related_task_ids(body) == ["Ab3cD4", "Xy1zW2"]

    def test_hash_marker_and_task_id_tag_preserve_order_with_dedupe(self):
        body = "Refs ## Xy1zW2 ## and [task_id]Ab3cD4 Xy1zW2[/task_id]"
        assert parse_related_task_ids(body) == ["Xy1zW2", "Ab3cD4"]

    def test_hash_marker_ignores_markdown_headings_without_task_ids(self):
        body = "## Heading\nDetails line"
        assert parse_related_task_ids(body) == []


class TestParseBlockedByIds:
    """Parsing [blocked_by], [block_by], [block], [parent_task] from body text.

    Why: parse_blocked_by_ids is the dependency-declaration mechanism — it lets tasks declare
    their blockers using body markup. Supports 4 synonym tags so users can use whichever mental
    model fits. Bugs here mean dependencies get silently dropped or mislinked.
    """

    # --- [blocked_by] canonical form ---

    def test_blocked_by_single_id(self):
        body = "Needs [blocked_by]Ab3cD4[/blocked_by] done first"
        assert parse_blocked_by_ids(body) == ["Ab3cD4"]

    def test_blocked_by_short_close(self):
        body = "[blocked_by]Ab3cD4[/]"
        assert parse_blocked_by_ids(body) == ["Ab3cD4"]

    def test_blocked_by_comma_separated(self):
        body = "[blocked_by]Ab3cD4, Xy1zW2[/blocked_by]"
        ids = parse_blocked_by_ids(body)
        assert "Ab3cD4" in ids
        assert "Xy1zW2" in ids

    def test_blocked_by_space_separated(self):
        body = "[blocked_by]Ab3cD4 Xy1zW2[/]"
        ids = parse_blocked_by_ids(body)
        assert "Ab3cD4" in ids
        assert "Xy1zW2" in ids

    # --- [block_by] synonym ---

    def test_block_by_synonym(self):
        body = "[block_by]Ab3cD4[/block_by]"
        assert parse_blocked_by_ids(body) == ["Ab3cD4"]

    def test_block_by_short_close(self):
        body = "[block_by]Ab3cD4[/]"
        assert parse_blocked_by_ids(body) == ["Ab3cD4"]

    # --- [block] synonym ---

    def test_block_synonym(self):
        body = "[block]Ab3cD4, Xy1zW2[/block]"
        ids = parse_blocked_by_ids(body)
        assert "Ab3cD4" in ids
        assert "Xy1zW2" in ids

    def test_block_short_close(self):
        body = "[block]Ab3cD4[/]"
        assert parse_blocked_by_ids(body) == ["Ab3cD4"]

    # --- [parent_task] synonym ---

    def test_parent_task_synonym(self):
        body = "[parent_task]Ab3cD4[/parent_task]"
        assert parse_blocked_by_ids(body) == ["Ab3cD4"]

    def test_parent_task_short_close(self):
        body = "[parent_task]Ab3cD4[/]"
        assert parse_blocked_by_ids(body) == ["Ab3cD4"]

    # --- Mixed synonym forms ---

    def test_mixed_synonyms_merged(self):
        body = "[blocked_by]Ab3cD4[/] and [block]Xy1zW2[/] and [parent_task]Mn5oP6[/]"
        ids = parse_blocked_by_ids(body)
        assert ids == ["Ab3cD4", "Xy1zW2", "Mn5oP6"]

    def test_mixed_synonyms_deduplicated(self):
        body = "[block]Ab3cD4[/] and [parent_task]Ab3cD4[/]"
        ids = parse_blocked_by_ids(body)
        assert ids == ["Ab3cD4"]

    # --- Case insensitivity ---

    def test_case_insensitive(self):
        body = "[BLOCKED_BY]Ab3cD4[/BLOCKED_BY]"
        assert parse_blocked_by_ids(body) == ["Ab3cD4"]

    def test_mixed_case_synonym(self):
        body = "[Block_By]Ab3cD4[/Block_By]"
        assert parse_blocked_by_ids(body) == ["Ab3cD4"]

    # --- Edge cases ---

    def test_empty_body(self):
        assert parse_blocked_by_ids("") == []

    def test_none_body(self):
        assert parse_blocked_by_ids(None) == []

    def test_no_tags(self):
        assert parse_blocked_by_ids("Just normal text") == []

    def test_multiple_tags_same_type(self):
        body = "[blocked_by]Ab3cD4[/] then [blocked_by]Xy1zW2[/]"
        ids = parse_blocked_by_ids(body)
        assert ids == ["Ab3cD4", "Xy1zW2"]
