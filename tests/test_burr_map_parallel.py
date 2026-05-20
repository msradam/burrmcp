"""Tests for the MapStates-based multi-temperature fan-out demo.

Coverage:

* Pure scoring primitives (parabolic envelope, tie-breaking).
* MapAndReduceWithStatus reducer: picks max score, handles ties,
  flips status.
* configure validates the temperature grid.
* Transition gates: prepare_inputs requires status=='configured',
  finalize requires status=='reduced'.
* End-to-end MCP walk to ``finalize``, asserting every fan-out task
  shows up in ``state["samples"]``.
* MapStates sub-runs land on the parent tracker's project dir (the
  burr-side audit trail), even though they do not surface under
  ``burr://subruns``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "examples"))

from burr_map_parallel import (
    _DEFAULT_TEMPERATURES,
    MapAndReduceWithStatus,
    _score_candidate,
    _synthesize_candidate,
    build_application,
    build_server,
)


async def _step(client, action, **inputs):
    return await client.call_tool("step", {"action": action, "inputs": inputs})


def _payload(result):
    return json.loads(result.content[0].text)


# -- scoring primitives -----------------------------------------------


def test_synthesize_candidate_is_deterministic_per_inputs():
    a = _synthesize_candidate("prompt", 0.5)
    b = _synthesize_candidate("prompt", 0.5)
    c = _synthesize_candidate("prompt", 0.6)
    assert a == b
    assert a != c
    assert a.startswith("candidate(t=0.5):")


def test_score_candidate_peaks_near_half():
    """Parabolic envelope is highest at t=0.5, lowest at t in {0, 1}."""
    prompt = "rank these bug reports"
    candidates = {t: _synthesize_candidate(prompt, t) for t in (0.0, 0.5, 1.0)}
    s0 = _score_candidate(prompt, 0.0, candidates[0.0])
    sm = _score_candidate(prompt, 0.5, candidates[0.5])
    s1 = _score_candidate(prompt, 1.0, candidates[1.0])
    assert sm > s0
    assert sm > s1


# -- reducer unit tests ----------------------------------------------


def _make_state_objects(rows):
    """Build a list of pseudo-state dicts that the reducer can iterate."""
    # The reducer only indexes sub_state[key], so plain dicts work.
    return list(rows)


def test_reducer_picks_highest_score_no_tie():
    reducer = MapAndReduceWithStatus()
    from burr.core import State

    base = State({"prompt": "p", "temperatures": [0.1, 0.5, 0.9]})
    rows = [
        {"prompt": "p", "temperature": 0.1, "candidate": "c1", "score": 0.2},
        {"prompt": "p", "temperature": 0.5, "candidate": "c2", "score": 0.9},
        {"prompt": "p", "temperature": 0.9, "candidate": "c3", "score": 0.4},
    ]
    new_state = reducer.reduce(base, iter(rows))
    assert new_state["best_temperature"] == 0.5
    assert new_state["best_candidate"] == "c2"
    assert new_state["best_score"] == 0.9
    assert new_state["status"] == "reduced"
    # Sorted by descending score.
    scores = [s["score"] for s in new_state["samples"]]
    assert scores == sorted(scores, reverse=True)


def test_reducer_breaks_tied_scores_by_lower_temperature():
    """When scores tie, the lower temperature wins. Keeps the reduce
    output stable across runs and platforms."""
    reducer = MapAndReduceWithStatus()
    from burr.core import State

    base = State({"prompt": "p", "temperatures": [0.2, 0.4]})
    rows = [
        {"prompt": "p", "temperature": 0.4, "candidate": "c2", "score": 0.7},
        {"prompt": "p", "temperature": 0.2, "candidate": "c1", "score": 0.7},
    ]
    new_state = reducer.reduce(base, iter(rows))
    assert new_state["best_temperature"] == 0.2
    assert new_state["best_candidate"] == "c1"


def test_reducer_empty_input_does_not_crash():
    reducer = MapAndReduceWithStatus()
    from burr.core import State

    base = State({"prompt": "p", "temperatures": []})
    new_state = reducer.reduce(base, iter([]))
    assert new_state["samples"] == []
    assert new_state["best_temperature"] is None
    assert new_state["best_candidate"] is None
    assert new_state["best_score"] is None
    assert new_state["status"] == "reduced"


# -- configure validation --------------------------------------------


@pytest.mark.asyncio
async def test_configure_rejects_empty_prompt():
    server = build_server()
    async with Client(server) as client:
        out = _payload(await _step(client, "configure", prompt=""))
        assert out["error"] == "action_error"
        assert "prompt" in out["error_message"]


@pytest.mark.asyncio
async def test_configure_rejects_out_of_range_temperature():
    server = build_server()
    async with Client(server) as client:
        out = _payload(await _step(client, "configure", prompt="x", temperatures=[0.1, 1.5]))
        assert out["error"] == "action_error"
        assert "0.0, 1.0" in out["error_message"] or "[0.0, 1.0]" in out["error_message"]


@pytest.mark.asyncio
async def test_configure_rejects_duplicate_temperatures():
    server = build_server()
    async with Client(server) as client:
        out = _payload(await _step(client, "configure", prompt="x", temperatures=[0.3, 0.3, 0.5]))
        assert out["error"] == "action_error"
        assert "unique" in out["error_message"]


@pytest.mark.asyncio
async def test_configure_accepts_default_temperature_grid():
    """No temperatures kwarg uses the built-in default."""
    server = build_server()
    async with Client(server) as client:
        out = _payload(await _step(client, "configure", prompt="x"))
        assert out["state"]["temperatures"] == list(_DEFAULT_TEMPERATURES)
        assert out["state"]["status"] == "configured"


# -- transition gates ------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_unreachable_before_map_and_reduce():
    """After configure, only prepare_inputs is a legal next step;
    finalize must not show up until status=='reduced'."""
    server = build_server()
    async with Client(server) as client:
        out = _payload(await _step(client, "configure", prompt="x"))
        assert "finalize" not in out["valid_next_actions"]
        # Even after prepare_inputs, finalize is still gated.
        out = _payload(await _step(client, "prepare_inputs"))
        assert out["valid_next_actions"] == ["map_and_reduce"]


# -- end-to-end fan-out ----------------------------------------------


@pytest.mark.asyncio
async def test_full_walk_to_finalize_records_every_sample():
    """All N temperatures should produce one row in state["samples"]
    and one of them should be picked as the best."""
    temps = [0.0, 0.25, 0.5, 0.75, 1.0]
    server = build_server()
    async with Client(server) as client:
        await _step(client, "configure", prompt="five-way fanout", temperatures=temps)
        await _step(client, "prepare_inputs")
        await _step(client, "map_and_reduce")
        out = _payload(await _step(client, "finalize"))
        report = out["state"]["final_report"]
        assert report["n_samples"] == len(temps)
        sampled_temps = {s["temperature"] for s in report["samples"]}
        assert sampled_temps == set(temps)
        assert report["best_temperature"] in sampled_temps
        # The parabola peaks at 0.5; that or one of its immediate
        # neighbours should win for this grid.
        assert report["best_temperature"] in {0.25, 0.5, 0.75}


@pytest.mark.asyncio
async def test_fanout_plan_records_intended_parallel_tasks(tmp_path):
    """prepare_inputs writes the per-task plan into burr://history so
    a client can see what was about to be fanned out before
    map_and_reduce runs."""
    server = build_server()
    async with Client(server) as client:
        await _step(
            client,
            "configure",
            prompt="plan visibility",
            temperatures=[0.1, 0.9],
        )
        out = _payload(await _step(client, "prepare_inputs"))
        plan = out["state"]["fanout_plan"]
        assert plan == [
            {"prompt": "plan visibility", "temperature": 0.1},
            {"prompt": "plan visibility", "temperature": 0.9},
        ]


@pytest.mark.asyncio
async def test_map_states_subruns_land_on_parent_tracker_project(tmp_path, monkeypatch):
    """MapStates spawns sub-Applications via the parent's tracker
    (``"cascade"`` behaviour). Burrmcp's spawn_subapp resource does
    not see those sub-runs, but the tracker writes one child app
    directory per task under the same project. This test asserts
    the audit trail is on disk so anyone debugging the demo can find
    the per-task data even though ``burr://subruns`` is empty."""
    server = build_server()
    async with Client(server) as client:
        await _step(client, "configure", prompt="audit trail", temperatures=[0.2, 0.8])
        await _step(client, "prepare_inputs")
        await _step(client, "map_and_reduce")
        # MapStates does not register sub-runs under spawn_subapp's
        # resource, so this should be empty even though three apps
        # ran (parent + 2 children).
        subruns = json.loads((await client.read_resource("burr://subruns"))[0].text)
        assert subruns == []
    # The conftest autouse fixture redirected HOME to tmp_path, so
    # the tracker project landed under tmp_path/.burr/<project>/.
    project_dir = tmp_path / ".burr" / "burr-map-parallel-demo"
    assert project_dir.exists(), f"expected tracker dir at {project_dir}"
    app_dirs = [p for p in project_dir.iterdir() if p.is_dir()]
    # One parent run + one child app per fanned-out temperature.
    assert len(app_dirs) >= 3, (
        f"expected at least 3 app dirs (parent + 2 children), got {len(app_dirs)}: "
        f"{[p.name for p in app_dirs]}"
    )


@pytest.mark.asyncio
async def test_parent_history_records_map_and_reduce_as_single_entry():
    """From the parent FSM's point of view, MapStates is one action.
    The fan-out happens *inside* that action; the parent only sees
    one history entry for the whole map_and_reduce step."""
    server = build_server()
    async with Client(server) as client:
        await _step(client, "configure", prompt="single entry")
        await _step(client, "prepare_inputs")
        await _step(client, "map_and_reduce")
        await _step(client, "finalize")
        history = json.loads((await client.read_resource("burr://history"))[0].text)
        actions = [h["action"] for h in history]
        assert actions == [
            "configure",
            "prepare_inputs",
            "map_and_reduce",
            "finalize",
        ]


def test_build_application_constructs_clean():
    """Smoke check that the factory builds without raising and the
    graph has the four expected actions."""
    app = build_application()
    names = {a.name for a in app.graph.actions}
    assert names == {"configure", "prepare_inputs", "map_and_reduce", "finalize"}
