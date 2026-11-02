"""The YAML schema loads, is internally consistent, and matches the v1 Notion rules."""

from __future__ import annotations

import pytest

from atya.schema import SchemaError, load_schema, parse_schema

S = load_schema("default")


def test_all_notion_databases_present():
    labels = {t.label for t in S.types.values()}
    assert {"Tasks", "Projects", "Thoughts", "Decisions", "People", "Library", "Journal",
            "Fitness — Training Log", "Robotics Learning — Milestones", "Meals Log",
            "VLA Milestones"} == labels


def test_vocabularies_match_system_schema_page():
    assert S.vocabularies["area"]["options"] == [
        "work", "personal", "health", "relationships", "learning", "finance", "home"]
    assert S.vocabularies["area"]["deprecated"] == ["julep"]
    assert S.types["task"].fields["Status"].options == (
        "inbox", "next", "in_progress", "waiting", "done", "dropped")
    assert S.types["library"].fields["Status"].options == (
        "queued", "reading", "watching", "listening", "done", "abandoned")
    assert S.types["person"].fields["Relationship"].options[-1] == "ex-colleague"


def test_children_follow_spine():
    for t in S.child_types:
        assert t.child is not None
        assert t.fields["Project"].to == "project"
        assert t.child.date_field in t.fields
    assert S.types["vla_milestone"].child.parent_project == "VLA Learning"
    assert "Checkpoint" in S.types["vla_milestone"].child.human_only


def test_verbatim_types():
    assert {t.key for t in S.types.values() if t.verbatim_body} == {"thought", "journal"}


def test_lookup_by_label_or_key():
    assert S.type("tasks").key == "task"
    assert S.type("Meals Log").key == "meal"
    assert S.type("nope") is None


def test_validate_drops_bad_values():
    r = S.validate("task", {"Status": "next", "Area": "julep", "Priority": "P9",
                            "Due Date": "2026-11-05", "Bogus": 1, "Project": ["x"]})
    assert r.fields == {"Status": "next", "Due Date": "2026-11-05"}
    assert r.links == {"Project": ["x"]}
    assert len(r.dropped) == 3


def test_deprecated_allowed_for_imports():
    r = S.validate("task", {"Area": "julep"}, allow_deprecated=True)
    assert r.fields == {"Area": "julep"}


def test_coercion():
    r = S.validate("fitness", {"Pullups (max)": "3", "Date": "2026-11-01T07:00:00Z",
                               "Type": "Workout"})
    assert r.fields == {"Pullups (max)": 3.0, "Date": "2026-11-01T07:00:00+00:00", "Type": "Workout"}
    assert S.validate("vla_milestone", {"Emphasised in source": "yes"}).fields == {
        "Emphasised in source": True}


def test_broken_schema_rejected():
    with pytest.raises(SchemaError) as e:
        parse_schema({"types": {"a": {"prefix": "A", "title_field": "T", "fields": {
            "x": {"type": "relation", "to": "missing"},
            "y": {"type": "select", "options": {"vocab": "nope"}}}}}})
    assert "unknown type" in str(e.value) and "unknown vocabulary" in str(e.value)


def test_prompt_rendering_mentions_every_type():
    text = S.render_types()
    for t in S.types.values():
        assert t.label in text
    assert "VERBATIM" in text
    assert "10-Pullup Goal" in S.render_children()
