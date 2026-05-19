"""ML training orchestration as an FSM.

A small two-class linear classifier (logistic regression) trained
via SGD on synthetic 2D data. The whole training loop is broken
into discrete FSM actions instead of a monolithic ``train()`` call:

    configure -> init_model -> train_epoch -> evaluate
                                  ^                |
                                  |  + continue (val_acc < target, epoch < max)
                                  |  + checkpoint -> back to train_epoch
                                  |  + pause_training -> resume_training -> train_epoch
                                  |  + stop_training (terminal)

What the FSM gives the training:

* Every epoch is a separate visible step in ``burr://history`` and
  ``burr://trace``. The training curve is reconstructable from the
  trace without bolting a separate metrics-logger on.
* Stopping criteria are encoded as transition conditions, not as
  ``if epoch >= max_epochs: break`` inside a loop. Reaching the
  target accuracy or max epochs *makes the continue-training
  transition invalid*; the agent (or human) reading
  ``burr://next`` sees only checkpoint / pause / stop as legal
  forward moves and has to choose.
* The pause/resume primitive lets an operator interrupt training,
  inspect state, and decide whether to resume or stop, all without
  killing the session. Maps cleanly to MCP elicitation when that
  becomes ubiquitous.
* Non-LLM. Every other burr-mcp demo wraps an LLM workflow; this
  one shows the FSM gating pattern works just as well for
  classical iterative computations like training loops.

Pure Python: no numpy, no sklearn, no external services. Data is
synthesized in-process from two Gaussian blobs.

Run:

    python examples/ml_training.py

A typical session: configure -> init_model -> train_epoch ->
evaluate -> train_epoch -> ... -> stop_training. The model reaches
~92% val accuracy in 8-12 epochs on the default config.
"""

from __future__ import annotations

import math
import random
from typing import Any

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition
from burr.tracking.client import LocalTrackingClient

from burr_mcp import ServingMode, mount

_TRACKER_PROJECT = "ml-training-demo"


# ── tiny linear-classifier math (pure stdlib) ──────────────────────


def _sigmoid(z: float) -> float:
    """Numerically-stable sigmoid."""
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


def _predict_prob(weights: list[float], bias: float, x: list[float]) -> float:
    z = bias + sum(w * xi for w, xi in zip(weights, x, strict=True))
    return _sigmoid(z)


def _predict_label(weights: list[float], bias: float, x: list[float]) -> int:
    return 1 if _predict_prob(weights, bias, x) >= 0.5 else 0


def _accuracy(weights: list[float], bias: float, X: list[list[float]], y: list[int]) -> float:
    if not X:
        return 0.0
    correct = sum(1 for xi, yi in zip(X, y, strict=True) if _predict_label(weights, bias, xi) == yi)
    return correct / len(X)


def _one_epoch_sgd(
    weights: list[float],
    bias: float,
    X: list[list[float]],
    y: list[int],
    lr: float,
) -> tuple[list[float], float]:
    """One pass over the training set with per-sample SGD updates."""
    new_w = list(weights)
    new_b = bias
    for xi, yi in zip(X, y, strict=True):
        p = _predict_prob(new_w, new_b, xi)
        grad = p - yi
        for j in range(len(new_w)):
            new_w[j] -= lr * grad * xi[j]
        new_b -= lr * grad
    return new_w, new_b


def _make_dataset(
    n_train: int, n_val: int, seed: int
) -> tuple[list[list[float]], list[int], list[list[float]], list[int]]:
    """Two overlapping 2D Gaussian blobs.

    Cluster centres at (-1.0, -1.0) and (1.0, 1.0) with sigma 1.0,
    moderate overlap. A linear classifier takes ~5-8 epochs of SGD
    at lr=0.05 to reach 0.90+ val accuracy on the default split.
    Smaller seeds may vary by a couple epochs either way.
    """
    rng = random.Random(seed)
    X: list[list[float]] = []
    y: list[int] = []
    total = n_train + n_val
    per_class = total // 2
    for _ in range(per_class):
        X.append([rng.gauss(-1.0, 1.0), rng.gauss(-1.0, 1.0)])
        y.append(0)
        X.append([rng.gauss(1.0, 1.0), rng.gauss(1.0, 1.0)])
        y.append(1)
    combined = list(zip(X, y, strict=True))
    rng.shuffle(combined)
    X_shuf, y_shuf = list(zip(*combined, strict=True))
    X_shuf = list(X_shuf)
    y_shuf = list(y_shuf)
    return (
        X_shuf[:n_train],
        list(y_shuf[:n_train]),
        X_shuf[n_train:],
        list(y_shuf[n_train:]),
    )


# ── actions ─────────────────────────────────────────────────────────


@action(
    reads=[],
    writes=[
        "max_epochs",
        "target_accuracy",
        "learning_rate",
        "seed",
        "X_train",
        "y_train",
        "X_val",
        "y_val",
        "data_summary",
        "status",
        "log",
    ],
)
def configure(
    state: State,
    max_epochs: int = 25,
    target_accuracy: float = 0.90,
    learning_rate: float = 0.02,
    n_train: int = 40,
    n_val: int = 20,
    seed: int = 42,
) -> State:
    """Set hyperparameters and load the synthetic dataset.

    Args:
        max_epochs: Hard upper bound on training epochs.
        target_accuracy: Validation accuracy at which the continue-
            training transition becomes invalid (the agent must then
            checkpoint, pause, or stop).
        learning_rate: SGD step size.
        n_train: Training set size (default 20).
        n_val: Validation set size (default 10).
        seed: RNG seed for reproducible data + initialization.
    """
    if max_epochs <= 0:
        raise ValueError("max_epochs must be positive")
    if not (0.0 < target_accuracy <= 1.0):
        raise ValueError("target_accuracy must be in (0, 1]")
    if learning_rate <= 0:
        raise ValueError("learning_rate must be positive")
    X_train, y_train, X_val, y_val = _make_dataset(n_train, n_val, seed)
    return state.update(
        max_epochs=max_epochs,
        target_accuracy=target_accuracy,
        learning_rate=learning_rate,
        seed=seed,
        X_train=X_train,
        y_train=y_train,
        X_val=X_val,
        y_val=y_val,
        data_summary={
            "n_train": len(X_train),
            "n_val": len(X_val),
            "n_features": 2,
            "n_classes": 2,
        },
        status="configured",
        log=[
            f"Configured: max_epochs={max_epochs}, target_accuracy={target_accuracy}, "
            f"learning_rate={learning_rate}, n_train={len(X_train)}, n_val={len(X_val)}"
        ],
    )


@action(reads=["seed", "log"], writes=["weights", "bias", "epoch", "status", "log"])
def init_model(state: State) -> State:
    """Initialize the model: deliberately mis-oriented weights so the
    training run has to rotate the decision boundary across several
    epochs before val accuracy improves. Without this, the synthetic
    dataset is so easy that any near-zero init lands at ~95% val
    accuracy in a single SGD pass and the demo finishes before the
    agent gets to see the FSM mediate multiple epochs.
    """
    rng = random.Random(state["seed"] + 1)
    # Start with weights rotated ~90 degrees from optimal. The
    # optimal direction is roughly (1, 1) for this dataset; we init
    # at (1, -1) plus jitter so SGD has to do real work.
    weights = [1.0 + rng.gauss(0, 0.05), -1.0 + rng.gauss(0, 0.05)]
    return state.update(
        weights=weights,
        bias=0.0,
        epoch=0,
        status="training",
        log=[
            *state["log"],
            f"Initialized model: weights={[round(w, 4) for w in weights]}, bias=0.0",
        ],
    )


@action(
    reads=["weights", "bias", "X_train", "y_train", "learning_rate", "epoch", "log"],
    writes=["weights", "bias", "epoch", "log"],
)
def train_epoch(state: State) -> State:
    """Run one SGD pass over the training set."""
    new_weights, new_bias = _one_epoch_sgd(
        state["weights"],
        state["bias"],
        state["X_train"],
        state["y_train"],
        state["learning_rate"],
    )
    next_epoch = state["epoch"] + 1
    return state.update(
        weights=new_weights,
        bias=new_bias,
        epoch=next_epoch,
        log=[*state["log"], f"Trained epoch {next_epoch}"],
    )


@action(
    reads=[
        "weights",
        "bias",
        "X_train",
        "y_train",
        "X_val",
        "y_val",
        "epoch",
        "train_history",
        "best_val_acc",
        "best_epoch",
        "log",
    ],
    writes=["train_history", "best_val_acc", "best_epoch", "last_metrics", "log"],
)
def evaluate(state: State) -> State:
    """Score on both train and val, record in history, track best epoch."""
    train_acc = _accuracy(state["weights"], state["bias"], state["X_train"], state["y_train"])
    val_acc = _accuracy(state["weights"], state["bias"], state["X_val"], state["y_val"])
    metrics = {
        "epoch": state["epoch"],
        "train_acc": round(train_acc, 4),
        "val_acc": round(val_acc, 4),
    }
    history = [*state.get("train_history", []), metrics]
    best_val_acc = state.get("best_val_acc") or 0.0
    best_epoch = state.get("best_epoch") or 0
    improved = val_acc > best_val_acc
    if improved:
        best_val_acc = val_acc
        best_epoch = state["epoch"]
    log_line = (
        f"Evaluated epoch {state['epoch']}: train_acc={metrics['train_acc']}, "
        f"val_acc={metrics['val_acc']}"
    )
    if improved:
        log_line += f" (new best, was {state.get('best_val_acc', 0.0):.4f})"
    return state.update(
        train_history=history,
        best_val_acc=round(best_val_acc, 4),
        best_epoch=best_epoch,
        last_metrics=metrics,
        log=[*state["log"], log_line],
    )


@action(
    reads=["weights", "bias", "epoch", "best_val_acc", "best_epoch", "log"],
    writes=["last_checkpoint", "log"],
)
def checkpoint(state: State) -> State:
    """Snapshot the current model + metrics as a named checkpoint."""
    snapshot = {
        "epoch": state["epoch"],
        "weights": list(state["weights"]),
        "bias": state["bias"],
        "best_val_acc": state["best_val_acc"],
        "best_epoch": state["best_epoch"],
    }
    return state.update(
        last_checkpoint=snapshot,
        log=[*state["log"], f"Checkpointed at epoch {state['epoch']}"],
    )


@action(reads=["status", "log"], writes=["status", "log"])
def pause_training(state: State) -> State:
    """Pause training. Only resume_training or stop_training are valid next."""
    return state.update(
        status="paused",
        log=[*state["log"], "Training paused. Awaiting resume_training or stop_training."],
    )


@action(reads=["status", "log"], writes=["status", "log"])
def resume_training(state: State) -> State:
    """Resume training after a pause."""
    return state.update(
        status="training",
        log=[*state["log"], "Training resumed."],
    )


@action(
    reads=["epoch", "best_val_acc", "best_epoch", "last_metrics", "log"],
    writes=["status", "stop_reason", "final_report", "log"],
)
def stop_training(state: State, reason: str = "manual") -> State:
    """Terminal: stop training and assemble a final report."""
    final_report: dict[str, Any] = {
        "stop_reason": reason,
        "epochs_completed": state["epoch"],
        "final_metrics": state.get("last_metrics"),
        "best_val_acc": state.get("best_val_acc"),
        "best_epoch": state.get("best_epoch"),
    }
    return state.update(
        status="stopped",
        stop_reason=reason,
        final_report=final_report,
        log=[*state["log"], f"Training stopped: {reason}"],
    )


# ── graph ───────────────────────────────────────────────────────────


_KEEP_TRAINING = Condition.expr(
    "status == 'training' and epoch < max_epochs and best_val_acc < target_accuracy"
)
_IS_TRAINING = Condition.expr("status == 'training'")
_IS_PAUSED = Condition.expr("status == 'paused'")


def build_application():
    return (
        ApplicationBuilder()
        .with_actions(
            configure=configure,
            init_model=init_model,
            train_epoch=train_epoch,
            evaluate=evaluate,
            checkpoint=checkpoint,
            pause_training=pause_training,
            resume_training=resume_training,
            stop_training=stop_training,
        )
        .with_transitions(
            ("configure", "init_model"),
            ("init_model", "train_epoch"),
            ("train_epoch", "evaluate"),
            # After evaluate: continue training only if we haven't hit
            # max_epochs or the target accuracy.
            ("evaluate", "train_epoch", _KEEP_TRAINING),
            ("evaluate", "checkpoint", _IS_TRAINING),
            ("evaluate", "pause_training", _IS_TRAINING),
            ("evaluate", "stop_training", _IS_TRAINING),
            # After checkpoint: back to training (same conditions apply
            # for the continue path).
            ("checkpoint", "train_epoch", _KEEP_TRAINING),
            ("checkpoint", "pause_training", _IS_TRAINING),
            ("checkpoint", "stop_training", _IS_TRAINING),
            # Pause: only resume or stop.
            ("pause_training", "resume_training", _IS_PAUSED),
            ("pause_training", "stop_training", _IS_PAUSED),
            # Resume back into training.
            ("resume_training", "train_epoch", _IS_TRAINING),
            # stop_training is terminal.
        )
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .with_state(
            max_epochs=20,
            target_accuracy=0.92,
            learning_rate=0.05,
            seed=42,
            X_train=[],
            y_train=[],
            X_val=[],
            y_val=[],
            data_summary={},
            weights=[],
            bias=0.0,
            epoch=0,
            train_history=[],
            best_val_acc=0.0,
            best_epoch=0,
            last_metrics=None,
            last_checkpoint=None,
            status="initial",
            stop_reason=None,
            final_report=None,
            log=[],
        )
        .with_entrypoint("configure")
        .build()
    )


def build_server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="ml-training",
        instructions=(
            "FSM-mediated training of a 2-class logistic regression on "
            "synthetic 2D data. Start every session with "
            "configure(max_epochs, target_accuracy, learning_rate, "
            "n_train, n_val, seed) (all optional). Walk: "
            "configure -> init_model -> train_epoch -> evaluate, then "
            "pick from {train_epoch, checkpoint, pause_training, "
            "stop_training} based on burr://next. The continue-training "
            "transition becomes invalid once epoch >= max_epochs OR "
            "best_val_acc >= target_accuracy, so reaching either end "
            "condition forces an explicit stop or pause. Pure-Python "
            "math, no external deps."
        ),
    )


if __name__ == "__main__":
    build_server().run()
