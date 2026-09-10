"""
Tests for Slite rate-limit retry and degraded-pull reporting.

Verifies:
  - A 429 is retried and succeeds on a later attempt.
  - Retry-After from the server overrides the local backoff step, capped.
  - A 404 is not retried (asking again will not help).
  - Retries are eventually exhausted and the note is reported as failed.
  - A failed children call marks a pruned subtree rather than a childless node.
  - A leaf node with genuinely no children is NOT reported as pruned.
  - TraversalReport.degraded / .summary describe the losses.

The bug these guard against: on 2026-09-10 a full traversal hit 10 rate-limit
errors. Nine notes were dropped and one children call failed, silently pruning
that node's whole subtree — the walk recorded it as childless and moved on, so
nothing in the output revealed how much was missing. The index then wiped and
replaced Slite with the degraded result.

No network or sleeping occurs; requests.get is stubbed and sleep is injected.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import requests

# Make scripts/ importable from the tests/ directory
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import slite_collector
from slite_collector import TraversalReport, collect_slite_docs_with_report

API_KEY = "test-key"


# ══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════════


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(
                f"{self.status_code} Client Error", response=self
            )


def _note(note_id, title="A note", content="body"):
    return FakeResponse(payload={"id": note_id, "title": title, "content": content})


def _children(ids):
    return FakeResponse(payload={"notes": [{"id": i} for i in ids]})


def _recorder():
    slept = []
    return slept, slept.append


# ══════════════════════════════════════════════════════════════════════════════
#  Retry behavior
# ══════════════════════════════════════════════════════════════════════════════


def test_429_is_retried_then_succeeds():
    responses = [FakeResponse(429), FakeResponse(429), _note("n1")]
    slept, sleep = _recorder()
    with patch("requests.get", side_effect=responses):
        result = slite_collector._fetch_note(API_KEY, "n1", sleep=sleep)

    assert result == {"id": "n1", "title": "A note", "content": "body"}
    assert slept == [1, 2], "should back off between attempts"


def test_retry_after_header_overrides_backoff_and_is_capped():
    responses = [FakeResponse(429, headers={"Retry-After": "7"}), _note("n1")]
    slept, sleep = _recorder()
    with patch("requests.get", side_effect=responses):
        slite_collector._fetch_note(API_KEY, "n1", sleep=sleep)
    assert slept == [7], "server's Retry-After wins over the local step"

    responses = [FakeResponse(429, headers={"Retry-After": "9999"}), _note("n1")]
    slept, sleep = _recorder()
    with patch("requests.get", side_effect=responses):
        slite_collector._fetch_note(API_KEY, "n1", sleep=sleep)
    assert slept == [slite_collector.MAX_RETRY_AFTER], "absurd Retry-After is capped"


def test_404_is_not_retried():
    slept, sleep = _recorder()
    with patch("requests.get", return_value=FakeResponse(404)) as mock_get:
        result = slite_collector._fetch_note(API_KEY, "gone", sleep=sleep)

    assert result is None
    assert mock_get.call_count == 1, "a 404 will not improve by asking again"
    assert slept == []


def test_retries_are_eventually_exhausted():
    slept, sleep = _recorder()
    with patch("requests.get", return_value=FakeResponse(429)) as mock_get:
        result = slite_collector._fetch_note(API_KEY, "n1", sleep=sleep)

    assert result is None
    assert mock_get.call_count == len(slite_collector.RETRY_DELAYS) + 1
    assert slept == list(slite_collector.RETRY_DELAYS)


# ══════════════════════════════════════════════════════════════════════════════
#  Subtree pruning
# ══════════════════════════════════════════════════════════════════════════════


def test_failed_children_call_is_reported_as_a_pruned_subtree():
    # root fetches fine; its children call is throttled past every retry.
    def responder(url, **_kwargs):
        if url.endswith("/children"):
            return FakeResponse(429)
        return _note("root")

    _slept, sleep = _recorder()
    with patch("requests.get", side_effect=responder):
        chunks, report = collect_slite_docs_with_report(API_KEY, ["root"], sleep=sleep)

    assert len(chunks) == 1, "the root note itself was readable"
    assert report.pruned_subtrees == ["root"]
    assert report.degraded
    assert "subtree" in report.summary()


def test_genuine_leaf_is_not_reported_as_pruned():
    def responder(url, **_kwargs):
        if url.endswith("/children"):
            return _children([])
        return _note("root")

    _slept, sleep = _recorder()
    with patch("requests.get", side_effect=responder):
        chunks, report = collect_slite_docs_with_report(API_KEY, ["root"], sleep=sleep)

    assert len(chunks) == 1
    assert report.pruned_subtrees == [], "an empty child list is not a failure"
    assert not report.degraded
    assert report.summary() == "1 notes, complete"


def test_unreadable_note_is_counted_but_walk_continues():
    # root has two children; the first is permanently unreadable (404).
    def responder(url, **_kwargs):
        if url.endswith("/root/children"):
            return _children(["bad", "good"])
        if url.endswith("/children"):
            return _children([])
        if url.endswith("/notes/bad"):
            return FakeResponse(404)
        note_id = url.rsplit("/", 1)[-1]
        return _note(note_id)

    _slept, sleep = _recorder()
    with patch("requests.get", side_effect=responder):
        chunks, report = collect_slite_docs_with_report(API_KEY, ["root"], sleep=sleep)

    assert report.failed_notes == ["bad"]
    assert len(chunks) == 2, "root and the good child still made it"
    assert report.degraded
    assert "unreadable" in report.summary()


# ══════════════════════════════════════════════════════════════════════════════
#  Backwards compatibility
# ══════════════════════════════════════════════════════════════════════════════


def test_collect_slite_docs_still_returns_a_plain_list():
    def responder(url, **_kwargs):
        if url.endswith("/children"):
            return _children([])
        return _note("root")

    with patch("requests.get", side_effect=responder):
        chunks = slite_collector.collect_slite_docs(API_KEY, ["root"])

    assert isinstance(chunks, list)
    assert len(chunks) == 1


def test_report_summary_when_clean():
    report = TraversalReport(5, 5, [], [])
    assert not report.degraded
    assert report.summary() == "5 notes, complete"
