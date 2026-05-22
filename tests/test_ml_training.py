"""ML training FSM: non-LLM iterative orchestration demo.

Tests cover the happy path through to target_reached, the
checkpoint/pause/resume side flows, FSM enforcement that
``train_epoch`` becomes invalid once the target accuracy or max
epochs has been hit, and the pure-Python math primitives.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastmcp import Client

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "examples"))

from ml_training import (
    _accuracy,
    _make_dataset,
    _one_epoch_sgd,
    _predict_label,
    _predict_prob,
    _sigmoid,
    build_server,
)


async def _step(client, action, **inputs):
    return await client.call_tool("step", {"action": action, "inputs": inputs})


def _payload(result):
    return result.structured_content


# ── math primitives ────────────────────────────────────────────────


def test_sigmoid_endpoints_and_midpoint():
    assert _sigmoid(0.0) == 0.5
    assert _sigmoid(1000.0) > 0.999
    assert _sigmoid(-1000.0) < 0.001


def test_predict_prob_uses_weights_and_bias():
    # Weights (1, 0), bias 0: prob depends only on first feature.
    assert _predict_prob([1.0, 0.0], 0.0, [10.0, 99.0]) > 0.99
    assert _predict_prob([1.0, 0.0], 0.0, [-10.0, 99.0]) < 0.01


def test_predict_label_threshold_at_0_5():
    assert _predict_label([1.0, 0.0], 0.0, [1.0, 0.0]) == 1
    assert _predict_label([1.0, 0.0], 0.0, [-1.0, 0.0]) == 0


def test_make_dataset_returns_balanced_split():
    X_train, y_train, X_val, y_val = _make_dataset(40, 20, seed=42)
    assert len(X_train) == 40
    assert len(y_train) == 40
    assert len(X_val) == 20
    assert len(y_val) == 20
    # Roughly balanced classes.
    train_class_balance = abs(sum(y_train) - len(y_train) / 2)
    assert train_class_balance <= 5  # within 5 of perfect 50/50


def test_one_epoch_sgd_changes_weights_when_loss_is_nonzero():
    X = [[1.0, 0.0]]
    y = [0]  # but with weights (1, 0) the model predicts class 1
    w_before = [1.0, 0.0]
    b_before = 0.0
    w_after, _b_after = _one_epoch_sgd(w_before, b_before, X, y, lr=0.1)
    # Gradient should reduce weight that produced the wrong prediction.
    assert w_after[0] < w_before[0]


def test_accuracy_on_perfectly_predicted_set_is_one():
    X = [[1.0, 1.0], [-1.0, -1.0]]
    y = [1, 0]
    acc = _accuracy([10.0, 10.0], 0.0, X, y)
    assert acc == 1.0


# ── FSM happy path ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_training_run_reaches_target_and_stops_cleanly():
    """configure -> init_model -> train_epoch -> evaluate, looping
    until target_accuracy is reached; then stop_training."""
    server = build_server()
    async with Client(server) as client:
        await _step(client, "configure")
        await _step(client, "init_model")
        target_hit = False
        for _ in range(30):  # bounded loop, safety net
            await _step(client, "train_epoch")
            out = _payload(await _step(client, "evaluate"))
            if "train_epoch" not in out["valid_next_actions"]:
                target_hit = True
                assert "stop_training" in out["valid_next_actions"]
                break
        assert target_hit, "training should have hit target within 30 epochs"
        out = _payload(await _step(client, "stop_training", reason="target_reached"))
        report = out["state"]["final_report"]
        assert report["stop_reason"] == "target_reached"
        assert report["best_val_acc"] >= 0.90
        assert report["epochs_completed"] >= 1
        assert out["state"]["status"] == "stopped"


# ── FSM gating ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_train_epoch_invalid_after_target_reached():
    """Once best_val_acc >= target_accuracy, the continue-training
    transition is gone. checkpoint/pause/stop are the only forward
    moves."""
    server = build_server()
    async with Client(server) as client:
        await _step(client, "configure", target_accuracy=0.5)  # easy target
        await _step(client, "init_model")
        await _step(client, "train_epoch")
        out = _payload(await _step(client, "evaluate"))
        # With target=0.5, first eval should easily exceed; train_epoch
        # should be unreachable from here.
        assert "train_epoch" not in out["valid_next_actions"]
        assert set(out["valid_next_actions"]) <= {
            "checkpoint",
            "pause_training",
            "stop_training",
        }


@pytest.mark.asyncio
async def test_train_epoch_invalid_after_max_epochs_hit():
    """epoch >= max_epochs also closes the continue-training door."""
    server = build_server()
    async with Client(server) as client:
        await _step(client, "configure", max_epochs=2, target_accuracy=0.99)
        await _step(client, "init_model")
        await _step(client, "train_epoch")
        await _step(client, "evaluate")
        await _step(client, "train_epoch")
        out = _payload(await _step(client, "evaluate"))
        # Now epoch == 2 == max_epochs. Continue is closed.
        assert "train_epoch" not in out["valid_next_actions"]


@pytest.mark.asyncio
async def test_pause_then_resume_then_continue():
    """pause_training only allows resume or stop. resume_training
    flows back into train_epoch."""
    server = build_server()
    async with Client(server) as client:
        await _step(client, "configure", target_accuracy=0.99, max_epochs=10)
        await _step(client, "init_model")
        await _step(client, "train_epoch")
        await _step(client, "evaluate")
        out = _payload(await _step(client, "pause_training"))
        assert out["state"]["status"] == "paused"
        assert set(out["valid_next_actions"]) == {"resume_training", "stop_training"}
        out = _payload(await _step(client, "resume_training"))
        assert out["state"]["status"] == "training"
        assert "train_epoch" in out["valid_next_actions"]


@pytest.mark.asyncio
async def test_pause_then_stop_skips_back_into_training():
    """From pause, stop_training is callable directly; the FSM
    doesn't force a resume first."""
    server = build_server()
    async with Client(server) as client:
        await _step(client, "configure")
        await _step(client, "init_model")
        await _step(client, "train_epoch")
        await _step(client, "evaluate")
        await _step(client, "pause_training")
        out = _payload(await _step(client, "stop_training", reason="user_aborted"))
        assert out["state"]["final_report"]["stop_reason"] == "user_aborted"


@pytest.mark.asyncio
async def test_checkpoint_captures_current_weights_and_metrics():
    server = build_server()
    async with Client(server) as client:
        await _step(client, "configure", target_accuracy=0.99, max_epochs=10)
        await _step(client, "init_model")
        await _step(client, "train_epoch")
        await _step(client, "evaluate")
        out = _payload(await _step(client, "checkpoint"))
        cp = out["state"]["last_checkpoint"]
        assert cp["epoch"] == 1
        assert isinstance(cp["weights"], list)
        assert len(cp["weights"]) == 2
        assert "best_val_acc" in cp


# ── validation ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_configure_rejects_invalid_hyperparameters():
    server = build_server()
    async with Client(server) as client:
        out = _payload(await _step(client, "configure", max_epochs=0))
        assert out["error"] == "action_error"
        assert "max_epochs" in out["error_message"]


@pytest.mark.asyncio
async def test_train_history_grows_each_evaluation():
    server = build_server()
    async with Client(server) as client:
        await _step(client, "configure", target_accuracy=0.99, max_epochs=5)
        await _step(client, "init_model")
        for _ in range(3):
            await _step(client, "train_epoch")
            out = _payload(await _step(client, "evaluate"))
        history = out["state"]["train_history"]
        assert len(history) == 3
        assert all("train_acc" in m and "val_acc" in m for m in history)
        assert [m["epoch"] for m in history] == [1, 2, 3]


@pytest.mark.asyncio
async def test_burr_next_includes_all_options_after_first_evaluate():
    """Right after the first evaluate (target not yet hit), every
    forward option is on the table: continue, checkpoint, pause,
    stop."""
    server = build_server()
    async with Client(server) as client:
        await _step(client, "configure", target_accuracy=0.99, max_epochs=10)
        await _step(client, "init_model")
        await _step(client, "train_epoch")
        out = _payload(await _step(client, "evaluate"))
        valid = set(out["valid_next_actions"])
        assert {"train_epoch", "checkpoint", "pause_training", "stop_training"} <= valid
