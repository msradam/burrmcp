"""Skill-to-FSM: webapp testing methodology as caller-LLM prompts.

Decomposes Anthropic's webapp-testing SKILL into a Burr FSM. The
SKILL teaches a Reconnaissance-Then-Action pattern with one
load-bearing rule: do not inspect the DOM before ``networkidle`` has
fired. The FSM enforces that rule at the protocol layer by gating
``reconnaissance`` behind ``wait_for_load``.

Source SKILL: ``examples/skills/webapp-testing/SKILL.md``. The
SKILL's core pattern (verbatim from the "Reconnaissance-Then-Action
Pattern" section) is three steps:

    1. Inspect rendered DOM (screenshot + page.content + locators)
    2. Identify selectors from inspection results
    3. Execute actions using discovered selectors

The FSM wraps that with ``navigate`` + ``wait_for_load`` upstream and
``verify`` + ``finalize_test`` downstream so the test outcome lands in
the audit trail. ``navigate`` / ``wait_for_load`` come from the
SKILL's Decision Tree ("Navigate and wait for networkidle") and the
Common Pitfall ("Don't inspect the DOM before waiting for
networkidle"); ``verify`` and ``finalize_test`` are FSM conventions
added on top of the SKILL so the verdict is captured.

The FSM does not call Playwright. The caller LLM drives Playwright
(via its own MCP-attached browser or a Playwright runtime), reports
back observations, and the FSM records them. This keeps the demo
runnable without browser binaries while still demonstrating the
gated workflow that the SKILL is built around.

Pre-req for a real run: a Playwright-capable client. For the
shipped tests, the FSM is exercised purely through state mutations
since there is no actual browser involved.

A typical session:

    start_test(target_url="http://localhost:5173", app_kind="dynamic")
    navigate()
    wait_for_load()
    reconnaissance(observations={...})
    identify_selectors(selectors={...})
    execute_actions(actions=[...])
    verify(assertions=[...])
    finalize_test(verdict="passed")
"""

from __future__ import annotations

from typing import Any, Literal

from burr.core import ApplicationBuilder, State, action
from burr.tracking.client import LocalTrackingClient

from theodosia import ServingMode, mount

_TRACKER_PROJECT = "webapp-testing-demo"
_VALID_VERDICTS = {"passed", "failed", "inconclusive"}
_VALID_APP_KINDS = {"static", "dynamic"}


# == prompt templates ==============================================


_PROMPT_NAVIGATE = """\
PHASE 1 of 7: NAVIGATE.

Target: `{target_url}` (kind: `{app_kind}`).

Use your Playwright-capable client to launch a browser (chromium
headless per the SKILL) and navigate to the target. Capture any
console errors, network failures, or HTTP status visible at this
point. Then call `navigate()` so the FSM advances to the wait-for-
load phase.

The SKILL forbids inspecting the DOM before the page has settled;
the FSM enforces that by gating `reconnaissance` behind
`wait_for_load`.
"""


_PROMPT_WAIT_FOR_LOAD = """\
PHASE 2 of 7: WAIT FOR LOAD.

The SKILL's load-bearing rule: do not inspect the DOM until the
page has reached the `networkidle` state. Run
`page.wait_for_load_state('networkidle')` (or the async
equivalent), then call `wait_for_load(loaded=True, notes='...')`.

If the page does not settle (e.g., long-polling endpoints, animated
loaders) call `wait_for_load(loaded=False, notes='...')`. The FSM
will refuse `reconnaissance` until `loaded` is True; you'll need to
adjust the wait strategy and call again.
"""


_PROMPT_RECONNAISSANCE = """\
PHASE 3 of 7: RECONNAISSANCE.

Now that the page has settled, inspect what's actually rendered.
Capture:
- screenshot_path: where you saved the full-page screenshot
- buttons: text or aria-label of every visible button
- inputs: name / placeholder of every input
- links: visible link text
- console_warnings_or_errors: any messages worth flagging
- raw_dom_excerpt: a small slice of HTML around the parts you'll
  interact with (do not paste the whole page)

Call `reconnaissance(observations={{...}})`. The agent will use
these observations in the next phase to identify the selectors it
will use.
"""


_PROMPT_IDENTIFY_SELECTORS = """\
PHASE 4 of 7: IDENTIFY SELECTORS.

From the observations recorded in the previous phase, pick the
selectors you will interact with. Prefer (in this order, per the
SKILL's "Best Practices"): `text=`, `role=`, CSS selectors, then
IDs. Avoid selectors that depend on layout or class hashing.

For each action you plan, record a selector:
- per_action_selector: {{"action_name": "selector string", ...}}

Call `identify_selectors(selectors={{...}})`.
"""


_PROMPT_EXECUTE_ACTIONS = """\
PHASE 5 of 7: EXECUTE ACTIONS.

Drive the planned actions through Playwright (clicks, fills, key
presses, etc.) using the selectors you identified. For each action
record:
- name: short label ("click_submit", "fill_email", etc.)
- selector: the selector used
- input: any keyboard / form input (omit for clicks)
- result: "ok" | "selector_not_found" | "timeout" | "error"
- notes: anything notable (e.g., page navigated after click)

Call `execute_actions(actions=[{{...}}, ...])`.
"""


_PROMPT_VERIFY = """\
PHASE 6 of 7: VERIFY.

For each test assertion, evaluate against the rendered state:
- name: short label ("login_succeeded", "error_message_shown", ...)
- expected: what should be true
- actual: what you observed
- passed: bool

The full list of assertions is the load-bearing artefact of the
test run; it's what an operator or CI step reads off.

Call `verify(assertions=[{{...}}, ...])`.
"""


_PROMPT_FINALIZE = """\
PHASE 7 of 7: FINALIZE.

Reflect on the verify results and call `finalize_test(verdict=...,
notes='...')` with one of:
- `passed`: every assertion passed
- `failed`: at least one assertion failed
- `inconclusive`: the test could not be evaluated end-to-end (server
  refused, timeout, blocked by infra)

The FSM stores the verdict + a small summary so the audit trail in
theodosia://history is complete.
"""


# == actions =======================================================


@action(
    reads=[],
    writes=[
        "target_url",
        "app_kind",
        "loaded",
        "observations",
        "selectors",
        "actions",
        "assertions",
        "verdict",
        "summary",
        "current_prompt",
        "log",
    ],
)
def start_test(
    state: State,
    target_url: str,
    app_kind: Literal["static", "dynamic"] = "dynamic",
) -> State:
    """Open a webapp-testing session.

    Args:
        target_url: URL of the application under test. Use file://
            URLs for static HTML per the SKILL.
        app_kind: "dynamic" (server-rendered JS app) or "static"
            (plain HTML file). The SKILL's decision tree branches on
            this; the FSM just records it for the prompt content.
    """
    if not target_url.strip():
        raise ValueError("target_url must not be empty")
    if app_kind not in _VALID_APP_KINDS:
        raise ValueError(f"app_kind must be one of {sorted(_VALID_APP_KINDS)}; got {app_kind!r}")
    return state.update(
        target_url=target_url,
        app_kind=app_kind,
        loaded=False,
        observations={},
        selectors={},
        actions=[],
        assertions=[],
        verdict=None,
        summary=None,
        current_prompt=_PROMPT_NAVIGATE.format(target_url=target_url, app_kind=app_kind),
        log=[f"Test started: target_url={target_url!r}, app_kind={app_kind}"],
    )


@action(reads=["log"], writes=["current_prompt", "log"])
def navigate(state: State) -> State:
    """Marker that the agent has navigated; emit wait-for-load prompt."""
    return state.update(
        current_prompt=_PROMPT_WAIT_FOR_LOAD,
        log=[*state["log"], "Navigated"],
    )


@action(reads=["log"], writes=["loaded", "current_prompt", "log"])
def wait_for_load(state: State, loaded: bool = True, notes: str = "") -> State:
    """Record whether the page reached networkidle.

    Loaded must be True for the FSM to advance into reconnaissance;
    False loops back so the agent can revise the wait strategy.
    """
    if loaded:
        next_prompt = _PROMPT_RECONNAISSANCE
        log_msg = "Load confirmed (networkidle)"
    else:
        next_prompt = (
            "wait_for_load returned loaded=False. The FSM will not "
            "advance until the page settles; refine the wait strategy "
            "and call wait_for_load(loaded=True) again. Notes: " + (notes or "")
        )
        log_msg = f"Load NOT confirmed: {notes or '(no notes)'}"
    return state.update(
        loaded=loaded,
        current_prompt=next_prompt,
        log=[*state["log"], log_msg],
    )


@action(
    reads=["loaded", "log"],
    writes=["observations", "current_prompt", "log"],
)
def reconnaissance(state: State, observations: dict[str, Any]) -> State:
    """Stash DOM-inspection observations.

    The SKILL's load-bearing pre-condition: the page must have
    settled. The FSM refuses if ``state.loaded`` is False, matching
    the SKILL's "Don't inspect before networkidle" rule.
    """
    if not state["loaded"]:
        raise ValueError(
            "reconnaissance refused: state.loaded is False. Call "
            "wait_for_load(loaded=True) first; the SKILL forbids "
            "DOM inspection before networkidle has fired."
        )
    return state.update(
        observations=observations,
        current_prompt=_PROMPT_IDENTIFY_SELECTORS,
        log=[*state["log"], "Reconnaissance recorded"],
    )


@action(reads=["log"], writes=["selectors", "current_prompt", "log"])
def identify_selectors(state: State, selectors: dict[str, str]) -> State:
    """Stash the per-action selectors the agent picked."""
    return state.update(
        selectors=selectors,
        current_prompt=_PROMPT_EXECUTE_ACTIONS,
        log=[*state["log"], f"Selectors recorded for {len(selectors)} action(s)"],
    )


@action(reads=["log"], writes=["actions", "current_prompt", "log"])
def execute_actions(state: State, actions: list[dict[str, Any]]) -> State:
    """Stash the executed actions and emit verify prompt."""
    return state.update(
        actions=list(actions or []),
        current_prompt=_PROMPT_VERIFY,
        log=[*state["log"], f"Executed {len(actions or [])} action(s)"],
    )


@action(reads=["log"], writes=["assertions", "current_prompt", "log"])
def verify(state: State, assertions: list[dict[str, Any]]) -> State:
    """Stash assertion results and emit finalize prompt."""
    return state.update(
        assertions=list(assertions or []),
        current_prompt=_PROMPT_FINALIZE,
        log=[*state["log"], f"Verified {len(assertions or [])} assertion(s)"],
    )


@action(
    reads=["assertions", "actions", "log"],
    writes=["verdict", "summary", "current_prompt", "log"],
)
def finalize_test(
    state: State,
    verdict: Literal["passed", "failed", "inconclusive"],
    notes: str = "",
) -> State:
    """Terminal: record the verdict and a per-assertion summary."""
    if verdict not in _VALID_VERDICTS:
        raise ValueError(f"verdict must be one of {sorted(_VALID_VERDICTS)}; got {verdict!r}")
    assertions = state["assertions"]
    passed = sum(1 for a in assertions if a.get("passed"))
    failed = len(assertions) - passed
    summary = {
        "verdict": verdict,
        "actions_executed": len(state["actions"]),
        "assertions_total": len(assertions),
        "assertions_passed": passed,
        "assertions_failed": failed,
        "notes": notes,
    }
    return state.update(
        verdict=verdict,
        summary=summary,
        current_prompt=f"Test complete: {verdict}. See state.summary.",
        log=[*state["log"], f"Verdict: {verdict}"],
    )


# == graph =========================================================


def build_application():
    return (
        ApplicationBuilder()
        .with_actions(
            start_test=start_test,
            navigate=navigate,
            wait_for_load=wait_for_load,
            reconnaissance=reconnaissance,
            identify_selectors=identify_selectors,
            execute_actions=execute_actions,
            verify=verify,
            finalize_test=finalize_test,
        )
        .with_transitions(
            ("start_test", "navigate"),
            ("navigate", "wait_for_load"),
            ("wait_for_load", "reconnaissance"),
            ("reconnaissance", "identify_selectors"),
            ("identify_selectors", "execute_actions"),
            ("execute_actions", "verify"),
            ("verify", "finalize_test"),
        )
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .with_state(
            target_url="",
            app_kind="dynamic",
            loaded=False,
            observations={},
            selectors={},
            actions=[],
            assertions=[],
            verdict=None,
            summary=None,
            current_prompt="",
            log=[],
        )
        .with_entrypoint("start_test")
        .build()
    )


def build_server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="webapp-testing",
        instructions=(
            "Webapp-testing SKILL as an FSM. The caller LLM drives "
            "Playwright (via its own browser-capable MCP or a local "
            "Playwright runtime); this FSM enforces the testing "
            "workflow: start_test(target_url, app_kind) -> navigate "
            "-> wait_for_load (must report loaded=True before "
            "reconnaissance is reachable; the SKILL forbids DOM "
            "inspection before networkidle) -> reconnaissance -> "
            "identify_selectors -> execute_actions -> verify -> "
            "finalize_test. Read state.current_prompt after each "
            "step for the next phase's instructions. Source SKILL "
            "at examples/skills/webapp-testing/."
        ),
    )


if __name__ == "__main__":
    build_server().run()
