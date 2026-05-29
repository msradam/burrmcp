"""Tests for classified upstream responses (chaos handling).

The pattern lifted from Leavitt's chaos_handler.py: an action body uses
``safe_upstream`` instead of ``call_upstream`` directly, and gets back a
:class:`SourceResult` it can branch on without raising. Combined with
:class:`theodosia.testing.FakeUpstream`, every chaos mode (timeout,
malformed, error string, dict-with-error) is testable hermetically.
"""

from __future__ import annotations

import pytest

import theodosia
from theodosia import (
    ERROR,
    MALFORMED,
    OK,
    SourceResult,
    classify_payload,
    confidence_label,
    coverage,
    safe_upstream,
)
from theodosia.testing import FakeUpstream
from theodosia.upstream import bind_upstream, reset_upstream


def test_source_result_usable_true_only_for_ok() -> None:
    assert SourceResult("x", OK, data=1).usable is True
    assert SourceResult("x", ERROR, detail="bad").usable is False
    assert SourceResult("x", MALFORMED, data="?").usable is False


def test_source_result_unknown_status_rejected() -> None:
    with pytest.raises(ValueError, match="status must be one of"):
        SourceResult("x", "yolo")


def test_source_result_to_dict_round_trip() -> None:
    r = SourceResult("metrics", OK, data=[1, 2], detail="", meta={"src": "g"})
    d = r.to_dict()
    assert d == {
        "name": "metrics",
        "status": OK,
        "data": [1, 2],
        "detail": "",
        "meta": {"src": "g"},
    }


def test_classify_none_is_error() -> None:
    r = classify_payload("x", None)
    assert r.status == ERROR
    assert "empty" in r.detail


def test_classify_string_with_error_word_is_error() -> None:
    r = classify_payload("x", "internal server error: traceback ...")
    assert r.status == ERROR


def test_classify_plain_string_is_malformed() -> None:
    r = classify_payload("x", "just a label")
    assert r.status == MALFORMED


def test_classify_dict_with_error_key_is_error() -> None:
    r = classify_payload("x", {"error": "Grafana 500"})
    assert r.status == ERROR
    assert "Grafana 500" in r.detail


def test_classify_dict_without_error_key_is_ok_by_default() -> None:
    r = classify_payload("x", {"value": 42})
    assert r.status == OK


def test_classify_list_payload_is_ok_when_expect_list() -> None:
    r = classify_payload("x", [1, 2, 3], expect="list")
    assert r.status == OK


def test_classify_dict_passes_expect_list() -> None:
    """A dict satisfies expect='list' because list-of-records often round-trips
    through MCP as a dict with the list under a key."""
    r = classify_payload("x", {"items": [1, 2]}, expect="list")
    assert r.status == OK


def test_classify_int_fails_expect_list() -> None:
    r = classify_payload("x", 7, expect="list")
    assert r.status == MALFORMED


def test_classify_list_fails_expect_dict() -> None:
    r = classify_payload("x", [1, 2], expect="dict")
    assert r.status == MALFORMED


def test_classify_long_detail_is_truncated() -> None:
    big = "error " + ("X" * 1000)
    r = classify_payload("x", big)
    assert r.status == ERROR
    assert len(r.detail) <= 300


@pytest.mark.asyncio
async def test_safe_upstream_returns_ok_for_clean_response() -> None:
    fake = FakeUpstream({"grafana": {"q": {"value": 42}}})
    tok = bind_upstream(fake)
    try:
        r = await safe_upstream("metrics", "grafana", "q", {})
    finally:
        reset_upstream(tok)
    assert r.usable is True
    assert r.status == OK
    assert r.data == {"value": 42}


@pytest.mark.asyncio
async def test_safe_upstream_classifies_dict_error_as_error() -> None:
    fake = FakeUpstream({"grafana": {"q": {"error": "rate limit"}}})
    tok = bind_upstream(fake)
    try:
        r = await safe_upstream("metrics", "grafana", "q", {})
    finally:
        reset_upstream(tok)
    assert r.status == ERROR
    assert "rate limit" in r.detail


@pytest.mark.asyncio
async def test_safe_upstream_catches_arbitrary_exception_from_callable() -> None:
    """An upstream tool that raises (chaos) should surface as ERROR, not crash."""

    def boom(args):
        raise RuntimeError("upstream blew up")

    fake = FakeUpstream({"grafana": {"q": boom}})
    tok = bind_upstream(fake)
    try:
        r = await safe_upstream("metrics", "grafana", "q", {})
    finally:
        reset_upstream(tok)
    assert r.status == ERROR
    assert "RuntimeError" in r.detail


@pytest.mark.asyncio
async def test_safe_upstream_catches_unknown_server_as_error() -> None:
    fake = FakeUpstream({"known": {"x": "ok"}})
    tok = bind_upstream(fake)
    try:
        r = await safe_upstream("metrics", "unknown", "x", {})
    finally:
        reset_upstream(tok)
    assert r.status == ERROR
    assert "upstream unavailable" in r.detail


@pytest.mark.asyncio
async def test_safe_upstream_with_no_manager_bound_returns_error() -> None:
    """No bind_upstream call; safe_upstream must not raise, just report ERROR."""
    r = await safe_upstream("metrics", "grafana", "q", {})
    assert r.status == ERROR


@pytest.mark.asyncio
async def test_safe_upstream_expect_list_classifies_int_as_malformed() -> None:
    fake = FakeUpstream({"grafana": {"q": 7}})
    tok = bind_upstream(fake)
    try:
        r = await safe_upstream("metrics", "grafana", "q", {}, expect="list")
    finally:
        reset_upstream(tok)
    assert r.status == MALFORMED


def test_coverage_counts_usable_only() -> None:
    results = [
        SourceResult("a", OK, data=1),
        SourceResult("b", ERROR, detail="x"),
        SourceResult("c", MALFORMED, data="?"),
        SourceResult("d", OK, data=2),
    ]
    assert coverage(results) == (2, 4)


def test_coverage_empty_list_is_zero_zero() -> None:
    assert coverage([]) == (0, 0)


@pytest.mark.parametrize(
    "usable,total,expected",
    [
        (0, 0, "none"),
        (0, 5, "none"),
        (3, 5, "degraded"),
        (5, 5, "full"),
        (1, 1, "full"),
    ],
)
def test_confidence_label_classification(usable: int, total: int, expected: str) -> None:
    assert confidence_label(usable, total) == expected


@pytest.mark.asyncio
async def test_action_pattern_one_flaky_source_does_not_poison_others() -> None:
    """The full pattern: query several upstreams in parallel, classify each,
    summarize via coverage + confidence_label. Even when one source is broken,
    the others survive and the FSM gets a degraded but useful result."""

    fake = FakeUpstream(
        {
            "grafana": {"metrics": {"value": 42}},
            "loki": {"logs": {"error": "loki down"}},
            "k8s": {"pods": [{"name": "web-1"}]},
        }
    )
    tok = bind_upstream(fake)
    try:
        results = [
            await safe_upstream("metrics", "grafana", "metrics", {}),
            await safe_upstream("logs", "loki", "logs", {}),
            await safe_upstream("pods", "k8s", "pods", {}, expect="list"),
        ]
    finally:
        reset_upstream(tok)
    assert [r.status for r in results] == [OK, ERROR, OK]
    usable, total = coverage(results)
    assert (usable, total) == (2, 3)
    assert confidence_label(usable, total) == "degraded"


def test_chaos_symbols_exported_at_top_level() -> None:
    """All chaos-handling symbols are importable from ``theodosia`` directly."""
    for name in (
        "OK",
        "ERROR",
        "MALFORMED",
        "SourceResult",
        "classify_payload",
        "safe_upstream",
        "coverage",
        "confidence_label",
    ):
        assert hasattr(theodosia, name), f"missing public export: {name}"
        assert name in theodosia.__all__, f"missing __all__: {name}"
