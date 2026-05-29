"""Tests for theodosia.Assembly."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from burr.core import ApplicationBuilder, action

import theodosia
from theodosia import Assembly


@pytest.fixture
def tiny_workflow():
    @action(reads=[], writes=["done"])
    def begin(state):
        return state.update(done=True)

    return (
        ApplicationBuilder()
        .with_actions(begin=begin)
        .with_state(done=False)
        .with_entrypoint("begin")
        .build()
    )


def test_assembly_minimal_fields(tiny_workflow):
    a = Assembly(name="x", workflow=tiny_workflow)
    assert a.name == "x"
    assert a.version == "0.1.0"
    assert a.personas is None
    assert a.upstream is None


def test_assembly_is_frozen(tiny_workflow):
    a = Assembly(name="x", workflow=tiny_workflow)
    with pytest.raises(Exception):  # noqa: B017 - dataclasses.FrozenInstanceError
        a.name = "y"  # type: ignore[misc]


def test_assembly_with_overrides_returns_new_instance(tiny_workflow):
    a = Assembly(name="x", workflow=tiny_workflow, upstream={"g": "url1"})
    b = a.with_overrides(upstream={"g": "url2"})
    assert a.upstream == {"g": "url1"}
    assert b.upstream == {"g": "url2"}
    assert a.name == b.name


def test_assembly_to_dict_round_trip(tiny_workflow):
    a = Assembly(
        name="x",
        workflow="my_module:build",
        upstream={"g": "url"},
        metadata={"team": "sre"},
    )
    d = a.to_dict()
    assert d["name"] == "x"
    assert d["workflow"] == "my_module:build"
    assert d["upstream"] == {"g": "url"}
    b = Assembly.from_dict(d)
    assert b == a


def test_assembly_to_yaml_round_trip(tmp_path: Path):
    a = Assembly(
        name="round-trip",
        workflow="my_mod:build",
        upstream={"g": "url"},
        metadata={"team": "sre"},
    )
    out = tmp_path / "a.yaml"
    text = a.to_yaml(out)
    assert "round-trip" in text
    assert out.exists()
    b = Assembly.from_yaml(out)
    assert b == a


def test_assembly_to_yaml_returns_text_without_path(tmp_path: Path):
    a = Assembly(name="x", workflow="m:f")
    text = a.to_yaml()
    assert "name: x" in text
    assert "workflow: m:f" in text


def test_assembly_from_yaml(tmp_path: Path):
    payload = {
        "name": "from-yaml",
        "workflow": "demo_module:build",
        "version": "2.0.0",
        "upstream": {"grafana": "http://localhost"},
    }
    f = tmp_path / "assembly.yaml"
    f.write_text(yaml.safe_dump(payload))
    a = Assembly.from_yaml(f)
    assert a.name == "from-yaml"
    assert a.workflow == "demo_module:build"
    assert a.version == "2.0.0"
    assert a.upstream == {"grafana": "http://localhost"}


def test_assembly_from_yaml_non_mapping_raises(tmp_path: Path):
    f = tmp_path / "bad.yaml"
    f.write_text("- a list at the top\n")
    with pytest.raises(ValueError, match="mapping"):
        Assembly.from_yaml(f)


def test_mount_accepts_assembly(tiny_workflow):
    a = Assembly(name="test-srv", workflow=tiny_workflow)
    server = theodosia.mount(a)
    assert server.name == "test-srv"


def test_assembly_serve_method(tiny_workflow):
    a = Assembly(name="serve-test", workflow=tiny_workflow)
    server = a.serve()
    assert server.name == "serve-test"


def test_mount_assembly_kwargs_override_assembly_fields(tiny_workflow):
    a = Assembly(name="from-assembly", workflow=tiny_workflow)
    server = theodosia.mount(a, name="from-override")
    assert server.name == "from-override"


def test_mount_assembly_with_persona(tiny_workflow):
    a = Assembly(
        name="with-persona",
        workflow=tiny_workflow,
        personas={"careful": "---\nname: careful\ndescription: c\n---\nbody\n"},
    )
    server = theodosia.mount(a)
    assert server.instructions is not None
    assert "careful" in server.instructions
    assert "body" in server.instructions


def test_mount_string_workflow_imports_target(tiny_workflow, monkeypatch):
    import sys
    import types

    mod = types.ModuleType("fake_workflow_mod")
    mod.build = lambda: tiny_workflow
    monkeypatch.setitem(sys.modules, "fake_workflow_mod", mod)

    a = Assembly(name="from-str", workflow="fake_workflow_mod:build")
    server = theodosia.mount(a)
    assert server.name == "from-str"


def test_assembly_in_public_exports():
    assert hasattr(theodosia, "Assembly")
    assert "Assembly" in theodosia.__all__
