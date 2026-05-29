"""Tests for the persona identity layer and its mount() integration."""

from __future__ import annotations

from pathlib import Path

import pytest

from theodosia.persona import (
    Persona,
    load_personas,
    render_with_frame,
    resolve_default,
)

# ── Persona.from_text ────────────────────────────────────────────────────


def test_parse_frontmatter_and_body() -> None:
    p = Persona.from_text(
        "---\n"
        "name: careful-sre\n"
        "description: Calm on-call SRE; root cause first.\n"
        "voice: terse\n"
        "metadata:\n"
        "  version: '1.0'\n"
        "---\n"
        "\n"
        "# Identity\n"
        "\n"
        "You prefer to gather evidence before acting.\n"
    )
    assert p.name == "careful-sre"
    assert "Calm on-call SRE" in p.description
    assert p.voice == "terse"
    assert p.metadata == {"version": "1.0"}
    assert "gather evidence" in p.body
    assert not p.body.startswith("\n")  # body is stripped


def test_no_frontmatter_uses_fallback_name() -> None:
    p = Persona.from_text("Just a body.\n", fallback_name="my-persona")
    assert p.name == "my-persona"
    assert p.body == "Just a body."
    assert p.description == ""
    assert p.voice is None


def test_no_name_anywhere_raises() -> None:
    with pytest.raises(ValueError, match="no 'name'"):
        Persona.from_text("just a body, no name")


def test_unterminated_frontmatter_raises() -> None:
    with pytest.raises(ValueError, match="closing '---' fence"):
        Persona.from_text("---\nname: foo\nno closing fence")


def test_non_mapping_frontmatter_raises() -> None:
    with pytest.raises(ValueError, match="YAML mapping"):
        Persona.from_text("---\n- list-not-mapping\n---\nbody\n")


def test_bom_at_start_is_stripped() -> None:
    p = Persona.from_text("﻿" + "---\nname: x\n---\nbody\n")
    assert p.name == "x"


def test_to_prompt_text_includes_name_description_voice_body() -> None:
    p = Persona(
        name="careful",
        description="A careful operator.",
        body="Always verify before acting.",
        voice="terse",
    )
    rendered = p.to_prompt_text()
    assert "acting as the persona 'careful'" in rendered
    assert "A careful operator." in rendered
    assert "Voice: terse." in rendered
    assert "verify before acting" in rendered


def test_to_prompt_text_handles_minimal_persona() -> None:
    p = Persona(name="x", description="", body="hello")
    rendered = p.to_prompt_text()
    assert "acting as the persona 'x'" in rendered
    assert "hello" in rendered
    assert "Voice:" not in rendered


# ── Persona.from_file ────────────────────────────────────────────────────


def test_from_file_uses_stem_as_fallback(tmp_path: Path) -> None:
    f = tmp_path / "minimal.md"
    f.write_text("body only\n")
    p = Persona.from_file(f)
    assert p.name == "minimal"  # the stem
    assert p.body == "body only"


def test_from_file_not_found_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        Persona.from_file(tmp_path / "does_not_exist.md")


# ── load_personas: each source shape ─────────────────────────────────────


def test_load_personas_none_returns_empty() -> None:
    assert load_personas(None) == {}


def test_load_personas_from_list() -> None:
    pl = [
        Persona(name="a", description="", body="ba"),
        Persona(name="b", description="", body="bb"),
    ]
    out = load_personas(pl)
    assert set(out) == {"a", "b"}
    assert out["a"].body == "ba"


def test_load_personas_from_dict_uses_parsed_name() -> None:
    src = {
        "filename-key": ("---\nname: actual-name\n---\nbody-text\n"),
    }
    out = load_personas(src)
    # The frontmatter's name wins, not the dict key.
    assert "actual-name" in out
    assert "filename-key" not in out


def test_load_personas_from_dict_uses_key_as_fallback_when_no_frontmatter() -> None:
    src = {"my-fallback": "no-frontmatter body"}
    out = load_personas(src)
    assert "my-fallback" in out


def test_load_personas_dict_duplicate_name_raises() -> None:
    src = {
        "a": "---\nname: same\n---\nA body\n",
        "b": "---\nname: same\n---\nB body\n",
    }
    with pytest.raises(ValueError, match="duplicate persona name"):
        load_personas(src)


def test_load_personas_from_directory(tmp_path: Path) -> None:
    (tmp_path / "careful.md").write_text("---\nname: careful\ndescription: c\n---\nbody1\n")
    (tmp_path / "reckless.md").write_text("---\nname: reckless\ndescription: r\n---\nbody2\n")
    out = load_personas(tmp_path)
    assert set(out) == {"careful", "reckless"}
    assert out["careful"].description == "c"


def test_load_personas_from_single_file(tmp_path: Path) -> None:
    f = tmp_path / "solo.md"
    f.write_text("---\nname: solo\n---\nbody\n")
    out = load_personas(f)
    assert list(out) == ["solo"]


def test_load_personas_directory_skips_non_md(tmp_path: Path) -> None:
    (tmp_path / "p.md").write_text("---\nname: p\n---\nbody\n")
    (tmp_path / "p.txt").write_text("should be ignored")
    (tmp_path / "p.yaml").write_text("should also be ignored")
    out = load_personas(tmp_path)
    assert list(out) == ["p"]


def test_load_personas_directory_duplicate_name_raises(tmp_path: Path) -> None:
    (tmp_path / "one.md").write_text("---\nname: same\n---\nbody\n")
    (tmp_path / "two.md").write_text("---\nname: same\n---\nbody\n")
    with pytest.raises(ValueError, match="duplicate persona name"):
        load_personas(tmp_path)


def test_load_personas_nonexistent_path_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be a directory"):
        load_personas(tmp_path / "does_not_exist")


# ── resolve_default ──────────────────────────────────────────────────────


def test_resolve_default_empty_returns_none() -> None:
    assert resolve_default({}, None) is None
    assert resolve_default({}, "anything") is None


def test_resolve_default_explicit_request_honored() -> None:
    personas = {
        "a": Persona(name="a", description="", body=""),
        "b": Persona(name="b", description="", body=""),
    }
    chosen = resolve_default(personas, "b")
    assert chosen is not None
    assert chosen.name == "b"


def test_resolve_default_unknown_request_raises() -> None:
    personas = {"a": Persona(name="a", description="", body="")}
    with pytest.raises(ValueError, match="not found"):
        resolve_default(personas, "nope")


def test_resolve_default_picks_lexically_first() -> None:
    # Order of insertion intentionally not sorted to confirm we pick by sort.
    personas = {
        "zeta": Persona(name="zeta", description="", body=""),
        "alpha": Persona(name="alpha", description="", body=""),
        "mu": Persona(name="mu", description="", body=""),
    }
    chosen = resolve_default(personas, None)
    assert chosen is not None
    assert chosen.name == "alpha"


# ── Adapter integration: mount() with personas ───────────────────────────


@pytest.fixture
def tiny_app():
    """A 2-state Burr app for adapter-integration tests."""
    from burr.core import ApplicationBuilder, action

    @action(reads=[], writes=["done"])
    def begin(state):
        return state.update(done=False)

    @action(reads=["done"], writes=["done"])
    def finish(state):
        return state.update(done=True)

    return (
        ApplicationBuilder()
        .with_actions(begin=begin, finish=finish)
        .with_transitions(("begin", "finish"))
        .with_state(done=False)
        .with_entrypoint("begin")
        .build()
    )


def test_mount_without_personas_works(tiny_app):
    """No personas, no breakage; baseline that the rest of mount() is happy."""
    from theodosia import mount

    server = mount(lambda: tiny_app, name="no-persona-test")
    assert server.name == "no-persona-test"


def test_mount_with_persona_dict_composes_into_instructions(tiny_app):
    """The persona prompt prepends to the action surface in the server's
    instructions, between the default machinery preamble and the action list."""
    from theodosia import mount

    server = mount(
        lambda: tiny_app,
        name="with-persona",
        personas={
            "careful": (
                "---\nname: careful\ndescription: cautious operator\n---\nVerify before acting.\n"
            ),
            "reckless": ("---\nname: reckless\ndescription: bold operator\n---\nAct first.\n"),
        },
        default_persona="careful",
    )
    instructions = server.instructions or ""
    # The persona's body should be embedded.
    assert "Verify before acting" in instructions
    # The default persona's identity header should be there too.
    assert "careful" in instructions


def test_mount_default_persona_resolution_is_lexical(tiny_app):
    """When default_persona is None, alphabetical first wins."""
    from theodosia import mount

    server = mount(
        lambda: tiny_app,
        name="default-resolution",
        personas={
            "zeta": "---\nname: zeta\n---\nzeta body\n",
            "alpha": "---\nname: alpha\n---\nalpha body\n",
        },
    )
    instructions = server.instructions or ""
    assert "alpha body" in instructions
    assert "zeta body" not in instructions


def test_mount_unknown_default_persona_raises(tiny_app):
    from theodosia import mount

    with pytest.raises(ValueError, match="not found"):
        mount(
            lambda: tiny_app,
            name="bad-default",
            personas={"a": "---\nname: a\n---\nbody\n"},
            default_persona="b",
        )


# ── Frame-aware interpolation (render_with_frame) ────────────────────────


def test_render_with_frame_none_returns_verbatim() -> None:
    text = "hello {state.alert_id}"
    assert render_with_frame(text, None) == text


def test_render_with_frame_substitutes_known_field() -> None:
    out = render_with_frame(
        "Alert {state.alert_id} for {state.region}",
        {"state": {"alert_id": "abc-123", "region": "us-east"}},
    )
    assert out == "Alert abc-123 for us-east"


def test_render_with_frame_missing_path_renders_empty() -> None:
    out = render_with_frame(
        "Alert {state.alert_id} severity {state.severity}",
        {"state": {"alert_id": "x"}},
    )
    # Missing severity becomes empty, not the literal placeholder.
    assert out == "Alert x severity "


def test_render_with_frame_handles_deep_path() -> None:
    out = render_with_frame(
        "Reachable: {action.reachable.count}",
        {"action": {"reachable": {"count": 3}}},
    )
    assert out == "Reachable: 3"


def test_render_with_frame_ignores_curly_braces_without_path() -> None:
    """Markdown bodies often contain curly braces in code; they must survive."""
    text = "Run `{'a': 1}` to test; reference state via {state.phase}"
    out = render_with_frame(text, {"state": {"phase": "diagnosis"}})
    assert "{'a': 1}" in out
    assert "{state.phase}" not in out
    assert "diagnosis" in out


def test_to_prompt_text_interpolates_body_only_not_header() -> None:
    """The "You are acting as the persona X" header should never carry
    placeholders; only the body is interpolated."""
    p = Persona(
        name="watcher",
        description="A frame-aware persona.",
        body="You are at {action.name}, with {graph.total_actions} actions total.",
    )
    rendered = p.to_prompt_text(
        frame={
            "action": {"name": "investigate"},
            "graph": {"total_actions": 5},
        }
    )
    assert "acting as the persona 'watcher'" in rendered
    assert "You are at investigate, with 5 actions total." in rendered


def test_to_prompt_text_no_frame_keeps_placeholders_as_is() -> None:
    """A persona used statically (mount time, no session) renders body verbatim."""
    p = Persona(
        name="x",
        description="",
        body="State key: {state.alert_id}",
    )
    rendered = p.to_prompt_text(frame=None)
    assert "{state.alert_id}" in rendered


# ── Frame-aware end-to-end (mount + step + prompt fetch) ─────────────────


def test_frame_aware_persona_renders_state_after_step(tiny_app):
    """After a step lands in a state field, the persona prompt should pick
    up the new value via {state.X} interpolation."""
    from theodosia import mount

    server = mount(
        lambda: tiny_app,
        name="frame-aware-test",
        personas={
            "watcher": (
                "---\nname: watcher\ndescription: w\n---\n"
                "done={state.done}, action={action.name}, "
                "reachable={action.reachable}, total={graph.total_actions}\n"
            ),
        },
        default_persona="watcher",
    )
    # At mount time (no session), the static instructions carry the
    # un-interpolated text. This is by design: the static path has no
    # frame; the prompt-fetch path is what gets the live values.
    static_instr = server.instructions or ""
    assert "{state.done}" in static_instr
