"""On-call alert triage with a real Granite call inside the graph.

A free-form alert text comes in. The FSM walks it through:

    report_alert -> classify_severity -> extract_service
                 -> suggest_runbook -> format_response

where ``classify_severity`` and ``extract_service`` are real Granite
calls via Ollama, and ``suggest_runbook`` is a deterministic lookup
against the runbook corpus shipped under
``examples/data/parallel_research/runbooks/``.

What the FSM gives the LLM:

* Both LLM steps retry up to three times if Granite returns
  malformed output (severity not in ``{P0, P1, P2, P3}``, service
  not in the known list). After three strikes the FSM routes to a
  ``route_to_human`` terminal action with the raw attempts captured
  in state so an operator can see what Granite was saying.
* The corpus lookup runs without involving Granite at all, so
  expensive LLM tokens go to the part of the workflow where the
  free-form input lives, not to retrieval where deterministic
  search suffices.
* The retry loop is encoded as transitions, not as a Python
  ``while`` loop inside the action. Every retry is a separate
  visible step in ``burr://history`` and ``burr://trace``, and
  every Granite response is captured in
  ``severity_attempts`` / ``service_attempts`` for audit.

Requires Ollama running with a Granite model pulled:

    ollama serve &
    ollama pull granite4.1:3b
    python examples/granite_oncall.py

Override the model or endpoint via env vars:

    BURR_MCP_GRANITE_MODEL=granite4:1b
    BURR_MCP_GRANITE_OLLAMA_BASE=http://other-host:11434
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition
from burr.tracking.client import LocalTrackingClient

from burrmcp import ServingMode, mount

_TRACKER_PROJECT = "granite-oncall-demo"
_DEFAULT_MODEL = os.environ.get("BURR_MCP_GRANITE_MODEL", "granite4.1:3b")
_OLLAMA_BASE = os.environ.get("BURR_MCP_GRANITE_OLLAMA_BASE", "http://localhost:11434")
_GRANITE_TIMEOUT = float(os.environ.get("BURR_MCP_GRANITE_TIMEOUT", "30.0"))
_MAX_RETRIES = 3

_CORPUS_DIR = Path(__file__).parent / "data" / "parallel_research"
_KNOWN_SERVICES = ["auth", "billing", "deploy"]
_VALID_SEVERITIES = {"P0", "P1", "P2", "P3"}


class GraniteUnavailable(RuntimeError):
    """Raised when Ollama isn't reachable or the model isn't pulled.

    Bubbles up as an MCP ``action_error`` with a clear hint about
    starting Ollama. Distinct from malformed-output retries because
    "Ollama is down" needs operator intervention, not another LLM
    attempt.
    """


async def _call_granite(
    prompt: str,
    *,
    system: str | None = None,
    model: str = _DEFAULT_MODEL,
) -> str:
    """Call Granite via Ollama's ``/api/chat`` endpoint and return
    the assistant message content, stripped.

    Defined as a module-level function so tests can monkey-patch it
    without touching httpx or standing up a fake Ollama server.
    """
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    try:
        async with httpx.AsyncClient(timeout=_GRANITE_TIMEOUT) as client:
            r = await client.post(
                f"{_OLLAMA_BASE}/api/chat",
                json={
                    "model": model,
                    "messages": messages,
                    "stream": False,
                    "options": {"temperature": 0.0},
                },
            )
        r.raise_for_status()
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
        raise GraniteUnavailable(
            f"Ollama not reachable at {_OLLAMA_BASE}. "
            f"Start with `ollama serve` and pull the model: `ollama pull {model}`."
        ) from e
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise GraniteUnavailable(
                f"Model {model!r} not found in Ollama. Pull it with `ollama pull {model}`."
            ) from e
        raise
    return r.json()["message"]["content"].strip()


# ── prompts ─────────────────────────────────────────────────────────


_CLASSIFIER_SYSTEM = (
    "You are a strict classifier. Reply with exactly the requested format. "
    "No other text, no explanations, no preamble."
)

_SEVERITY_PROMPT = """Classify this alert by severity. Reply with exactly one of: P0, P1, P2, P3.

P0 = complete outage, all users affected
P1 = significant degradation, more than 10% of users affected
P2 = partial degradation or non-critical service down
P3 = minor issue, fixable in business hours

Alert: {alert}

Reply with ONLY the severity code (P0, P1, P2, or P3). No other words."""


_SERVICE_PROMPT = """Which service does this alert involve? Choose from: {services}.

Alert: {alert}

Reply with ONLY the service name. No other words."""


# ── actions ─────────────────────────────────────────────────────────


@action(
    reads=[],
    writes=["alert_text", "severity_attempts", "service_attempts", "log"],
)
async def report_alert(state: State, text: str) -> State:
    """Receive a free-form alert text. Resets retry counters."""
    if not text.strip():
        raise ValueError("alert text must not be empty")
    return state.update(
        alert_text=text,
        severity_attempts=[],
        service_attempts=[],
        log=[f"Alert reported: {text[:80]!r}"],
    )


@action(
    reads=["alert_text", "severity_attempts", "log"],
    writes=["severity", "severity_attempts", "log"],
)
async def classify_severity(state: State) -> State:
    """Granite call: classify the alert as P0, P1, P2, or P3.

    Records every Granite response in ``severity_attempts`` so the
    transition layer can decide retry vs. escalate. On a valid
    response, sets ``state.severity`` and the FSM advances; on an
    invalid response, leaves ``severity`` None and the retry
    transition fires.
    """
    prompt = _SEVERITY_PROMPT.format(alert=state["alert_text"])
    raw = await _call_granite(prompt, system=_CLASSIFIER_SYSTEM)
    candidate = raw.strip().upper().split()[0] if raw.strip() else ""
    candidate = candidate.rstrip(".,!?:;")
    attempts = [*state.get("severity_attempts", []), raw]
    log_line = f"Granite severity attempt {len(attempts)}: raw={raw[:60]!r}, parsed={candidate!r}"
    if candidate in _VALID_SEVERITIES:
        return state.update(
            severity=candidate,
            severity_attempts=attempts,
            log=[*state.get("log", []), log_line + " -> accepted"],
        )
    return state.update(
        severity=None,
        severity_attempts=attempts,
        log=[*state.get("log", []), log_line + " -> rejected"],
    )


@action(
    reads=["alert_text", "service_attempts", "log"],
    writes=["service", "service_attempts", "log"],
)
async def extract_service(state: State) -> State:
    """Granite call: extract which service this alert is about.

    Validates against ``_KNOWN_SERVICES``. Same retry-or-escalate
    pattern as ``classify_severity``.
    """
    prompt = _SERVICE_PROMPT.format(
        alert=state["alert_text"],
        services=", ".join(_KNOWN_SERVICES),
    )
    raw = await _call_granite(prompt, system=_CLASSIFIER_SYSTEM)
    candidate = raw.strip().lower().split()[0] if raw.strip() else ""
    candidate = candidate.rstrip(".,!?:;")
    attempts = [*state.get("service_attempts", []), raw]
    log_line = f"Granite service attempt {len(attempts)}: raw={raw[:60]!r}, parsed={candidate!r}"
    if candidate in _KNOWN_SERVICES:
        return state.update(
            service=candidate,
            service_attempts=attempts,
            log=[*state.get("log", []), log_line + " -> accepted"],
        )
    return state.update(
        service=None,
        service_attempts=attempts,
        log=[*state.get("log", []), log_line + " -> rejected"],
    )


def _find_runbook(service: str) -> dict:
    """Return the best-matching runbook for ``service`` from the
    shipped corpus. Falls back to ``incident-response.md`` when no
    runbook mentions the service, so an operator always has
    something to read."""
    runbooks_dir = _CORPUS_DIR / "runbooks"
    if not runbooks_dir.exists():
        return {"name": "(no runbooks)", "score": 0, "first_line": ""}
    candidates = []
    needle = service.lower()
    for path in sorted(runbooks_dir.glob("*.md")):
        content = path.read_text()
        score = content.lower().count(needle)
        candidates.append(
            {
                "name": path.name,
                "score": score,
                "first_line": content.splitlines()[0] if content else "",
            }
        )
    if not candidates:
        return {"name": "(none)", "score": 0, "first_line": ""}
    candidates.sort(key=lambda c: -c["score"])
    if candidates[0]["score"] > 0:
        return candidates[0]
    for c in candidates:
        if "incident-response" in c["name"]:
            return c
    return candidates[0]


@action(reads=["service", "log"], writes=["runbook", "log"])
async def suggest_runbook(state: State) -> State:
    """Deterministic lookup: match the affected service against
    runbook content. No LLM call."""
    runbook = _find_runbook(state["service"])
    return state.update(
        runbook=runbook,
        log=[*state.get("log", []), f"Matched runbook: {runbook['name']}"],
    )


@action(
    reads=["alert_text", "severity", "service", "runbook", "log"],
    writes=["final_response", "log"],
)
async def format_response(state: State) -> State:
    """Terminal: build the structured triage response."""
    response = {
        "status": "triaged",
        "alert": state["alert_text"],
        "severity": state["severity"],
        "service": state["service"],
        "runbook": state["runbook"],
    }
    return state.update(
        final_response=response,
        log=[
            *state.get("log", []),
            f"Triage complete: {state['severity']} / {state['service']}",
        ],
    )


@action(
    reads=["alert_text", "severity_attempts", "service_attempts", "log"],
    writes=["final_response", "log"],
)
async def route_to_human(state: State) -> State:
    """Terminal: LLM retries exhausted, hand off to a human."""
    response = {
        "status": "needs_human",
        "alert": state["alert_text"],
        "reason": (
            "automated triage failed after retries; "
            "review the granite attempts in severity_attempts / service_attempts"
        ),
        "severity_attempts": state.get("severity_attempts", []),
        "service_attempts": state.get("service_attempts", []),
    }
    return state.update(
        final_response=response,
        log=[*state.get("log", []), "Routed to human triage."],
    )


# ── graph ───────────────────────────────────────────────────────────


def build_application():
    return (
        ApplicationBuilder()
        .with_actions(
            report_alert=report_alert,
            classify_severity=classify_severity,
            extract_service=extract_service,
            suggest_runbook=suggest_runbook,
            format_response=format_response,
            route_to_human=route_to_human,
        )
        .with_transitions(
            ("report_alert", "classify_severity"),
            # classify_severity outcomes
            (
                "classify_severity",
                "extract_service",
                Condition.expr("severity is not None"),
            ),
            (
                "classify_severity",
                "classify_severity",
                Condition.expr(f"severity is None and len(severity_attempts) < {_MAX_RETRIES}"),
            ),
            (
                "classify_severity",
                "route_to_human",
                Condition.expr(f"severity is None and len(severity_attempts) >= {_MAX_RETRIES}"),
            ),
            # extract_service outcomes
            (
                "extract_service",
                "suggest_runbook",
                Condition.expr("service is not None"),
            ),
            (
                "extract_service",
                "extract_service",
                Condition.expr(f"service is None and len(service_attempts) < {_MAX_RETRIES}"),
            ),
            (
                "extract_service",
                "route_to_human",
                Condition.expr(f"service is None and len(service_attempts) >= {_MAX_RETRIES}"),
            ),
            ("suggest_runbook", "format_response"),
        )
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .with_state(
            alert_text="",
            severity=None,
            service=None,
            runbook=None,
            severity_attempts=[],
            service_attempts=[],
            final_response=None,
            log=[],
        )
        .with_entrypoint("report_alert")
        .build()
    )


def build_server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="granite-oncall",
        instructions=(
            "On-call alert triage with a real Granite-via-Ollama call "
            "inside the FSM. Start every session with "
            "report_alert(text=...). Then call classify_severity, "
            "extract_service, suggest_runbook, and format_response in "
            "order; the server walks each transition as it becomes "
            f"valid. classify_severity and extract_service hit Granite "
            f"({_DEFAULT_MODEL} at {_OLLAMA_BASE}); they auto-retry up "
            f"to {_MAX_RETRIES} times on malformed output and then "
            "route_to_human if Granite still can't comply. The "
            "runbook lookup is deterministic over the corpus at "
            "examples/data/parallel_research/runbooks/. Known services: "
            f"{', '.join(_KNOWN_SERVICES)}."
        ),
    )


if __name__ == "__main__":
    build_server().run()
