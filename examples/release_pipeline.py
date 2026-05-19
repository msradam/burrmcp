"""Release pipeline FSM: tests -> canary -> observe -> promote.

The whole point is that an MCP-connected agent literally cannot
promote a change to prod without walking through the gates. Try to
prompt your agent with "just promote this hotfix to prod" and watch
the server return ``invalid_transition`` with the actual required
next step.

Stages:

    submit_change -> run_tests -> deploy_canary
                                       |
                                       v
                                 observe_canary  (callable repeatedly)
                                       |
                          +------------+------------+
                          v                         v
                    promote_to_prod            rollback
                          |                         |
                          v                         v
                       close_change <----- close_change

If tests fail, ``run_tests`` writes ``tests_passed=False`` and the FSM
loops you back to ``submit_change`` rather than letting you continue
to canary. If a canary observation comes back ``degraded`` the only
valid next move is ``rollback``.

Run:

    python examples/release_pipeline.py
"""

from __future__ import annotations

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition
from burr.tracking.client import LocalTrackingClient

from burr_mcp import ServingMode, mount

_MIN_OBSERVATIONS_BEFORE_PROMOTE = 2
_TRACKER_PROJECT = "release-pipeline-demo"


# ── actions ─────────────────────────────────────────────────────────


@action(
    reads=[],
    writes=[
        "stage",
        "summary",
        "risk",
        "tests_passed",
        "canary_deployed",
        "canary_observations",
        "canary_health",
        "promoted",
        "rolled_back",
        "log",
    ],
)
def submit_change(state: State, summary: str, risk: str = "low") -> State:
    """Open a change request. Resets pipeline state.

    Args:
        summary: One-line description of what this change is.
        risk: "low", "medium", or "high". Informational only.
    """
    if risk not in {"low", "medium", "high"}:
        raise ValueError(f"risk must be low|medium|high; got {risk!r}")
    return state.update(
        stage="tests_pending",
        summary=summary,
        risk=risk,
        tests_passed=False,
        canary_deployed=False,
        canary_observations=[],
        canary_health="pending",
        promoted=False,
        rolled_back=False,
        log=[f"Change submitted: {summary} (risk={risk})."],
    )


@action(reads=["stage", "log"], writes=["stage", "tests_passed", "log"])
def run_tests(state: State, result: str = "passed") -> State:
    """Run the full test suite.

    Args:
        result: "passed" or "failed". If failed, the pipeline rewinds
            and you must re-submit (after presumably fixing the
            change).
    """
    if result not in {"passed", "failed"}:
        raise ValueError(f"result must be passed|failed; got {result!r}")
    passed = result == "passed"
    new_stage = "canary_pending" if passed else "tests_pending"
    log = [*state.get("log", []), f"Tests run: {result}."]
    return state.update(stage=new_stage, tests_passed=passed, log=log)


@action(
    reads=["stage", "log"],
    writes=["stage", "canary_deployed", "canary_health", "log"],
)
def deploy_canary(state: State) -> State:
    """Roll the change out to one canary host. Requires tests passed."""
    log = [*state.get("log", []), "Canary deployed to canary-host-01."]
    return state.update(
        stage="canary_observing",
        canary_deployed=True,
        canary_health="pending",
        log=log,
    )


@action(
    reads=["stage", "canary_observations", "log"],
    writes=["stage", "canary_observations", "canary_health", "log"],
)
def observe_canary(state: State, status: str = "healthy") -> State:
    """Record a canary observation.

    Args:
        status: "healthy" or "degraded". One ``degraded`` observation
            forces a rollback; otherwise enough healthy observations
            unlock promotion.
    """
    if status not in {"healthy", "degraded"}:
        raise ValueError(f"status must be healthy|degraded; got {status!r}")
    obs = [*state.get("canary_observations", []), status]
    if "degraded" in obs:
        health = "degraded"
        new_stage = "canary_rollback_ready"
    elif len(obs) >= _MIN_OBSERVATIONS_BEFORE_PROMOTE:
        health = "healthy"
        new_stage = "canary_promote_ready"
    else:
        health = "pending"
        new_stage = "canary_observing"
    log = [*state.get("log", []), f"Canary observation: {status} ({len(obs)})."]
    return state.update(
        stage=new_stage,
        canary_observations=obs,
        canary_health=health,
        log=log,
    )


@action(reads=["stage", "log"], writes=["stage", "promoted", "log"])
def promote_to_prod(state: State) -> State:
    """Promote the canary rollout to the full prod fleet."""
    log = [*state.get("log", []), "Promoted to prod fleet."]
    return state.update(stage="closing", promoted=True, log=log)


@action(reads=["stage", "log"], writes=["stage", "rolled_back", "log"])
def rollback(state: State) -> State:
    """Roll the canary back. Used when an observation comes in degraded."""
    log = [*state.get("log", []), "Canary rolled back."]
    return state.update(stage="closing", rolled_back=True, log=log)


@action(
    reads=["stage", "promoted", "rolled_back", "log"],
    writes=["stage", "log"],
)
def close_change(state: State) -> State:
    """Close the change request. Terminal."""
    outcome = (
        "promoted"
        if state["promoted"]
        else ("rolled_back" if state["rolled_back"] else "abandoned")
    )
    log = [*state.get("log", []), f"Change closed: {outcome}."]
    return state.update(stage="closed", log=log)


def build_application():
    return (
        ApplicationBuilder()
        .with_actions(
            submit_change=submit_change,
            run_tests=run_tests,
            deploy_canary=deploy_canary,
            observe_canary=observe_canary,
            promote_to_prod=promote_to_prod,
            rollback=rollback,
            close_change=close_change,
        )
        .with_transitions(
            # submit -> tests
            (
                "submit_change",
                "run_tests",
                Condition.expr("stage == 'tests_pending'"),
            ),
            # tests fail -> can resubmit; tests pass -> canary
            (
                "run_tests",
                "submit_change",
                Condition.expr("tests_passed == False"),
            ),
            (
                "run_tests",
                "deploy_canary",
                Condition.expr("tests_passed == True"),
            ),
            # canary deployed -> observe
            (
                "deploy_canary",
                "observe_canary",
                Condition.expr("canary_deployed == True"),
            ),
            # observe again, then either promote or rollback
            (
                "observe_canary",
                "observe_canary",
                Condition.expr("canary_health == 'pending'"),
            ),
            (
                "observe_canary",
                "promote_to_prod",
                Condition.expr("canary_health == 'healthy'"),
            ),
            (
                "observe_canary",
                "rollback",
                Condition.expr("canary_health == 'degraded'"),
            ),
            # close after promote or rollback
            (
                "promote_to_prod",
                "close_change",
                Condition.expr("promoted == True"),
            ),
            (
                "rollback",
                "close_change",
                Condition.expr("rolled_back == True"),
            ),
        )
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .with_state(
            stage="draft",
            summary="",
            risk="low",
            tests_passed=False,
            canary_deployed=False,
            canary_observations=[],
            canary_health="pending",
            promoted=False,
            rolled_back=False,
            log=[],
        )
        .with_entrypoint("submit_change")
        .build()
    )


def build_server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="release-pipeline",
        instructions=(
            "A release pipeline FSM: submit_change -> run_tests -> "
            "deploy_canary -> observe_canary -> promote_to_prod or "
            "rollback -> close_change. The FSM strictly enforces these "
            "gates, so promote_to_prod and rollback are not callable "
            "until the prerequisites are met. If a user asks you to "
            "'just promote' a change, walk through the steps in order "
            "instead. Read burr://next for the currently-valid action; "
            "read burr://state for the full pipeline status, including "
            "test result and canary observations."
        ),
    )


if __name__ == "__main__":
    build_server().run()
