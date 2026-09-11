"""
Tests for the network gate and transport/credential split in check_credentials.

Verifies:
  - wait_for_network returns immediately when the network is already up.
  - wait_for_network retries on backoff and succeeds when the network returns.
  - wait_for_network gives up after exhausting the delay schedule.
  - A DNS failure is classified as a transport failure, not a bad credential.
  - A 401 is classified as a bad credential, not a transport failure.
  - offline() is True only when every failure was a transport failure.

The bug these guard against: on 2026-09-10 the scheduled run fired while the
laptop had no network. Every API check failed DNS resolution, and the run
reported three expired credentials that were in fact healthy — sending the
reader to renew tokens that did not need renewing.

No network or disk access occurs; DNS and HTTP are stubbed throughout.
"""

import socket
import sys
from pathlib import Path
from unittest.mock import patch

import requests

# Make scripts/ importable from the tests/ directory
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import check_credentials
from check_credentials import CheckResult, offline, wait_for_network

# ══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════════

SOURCES = ["jira", "slite"]


def _recorder():
    """Collect sleep durations instead of idling."""
    slept = []
    return slept, slept.append


def _silent(_message):
    """Swallow progress output so test runs stay readable."""


# ══════════════════════════════════════════════════════════════════════════════
#  wait_for_network
# ══════════════════════════════════════════════════════════════════════════════


def test_returns_immediately_when_network_is_up():
    slept, sleep = _recorder()
    with patch.object(check_credentials, "network_is_up", return_value=True):
        assert wait_for_network(SOURCES, delays=(5, 10), sleep=sleep, on_wait=_silent)
    assert slept == [], "should not sleep when the network is already up"


def test_retries_and_succeeds_when_network_returns():
    # Down for the first two probes, up on the third — the laptop-wake case.
    probes = iter([False, False, True])
    slept, sleep = _recorder()
    with patch.object(
        check_credentials, "network_is_up", side_effect=lambda _s: next(probes)
    ):
        assert wait_for_network(
            SOURCES, delays=(5, 10, 20), sleep=sleep, on_wait=_silent
        )
    assert slept == [5, 10], "should back off between probes, then stop on success"


def test_gives_up_after_exhausting_the_schedule():
    slept, sleep = _recorder()
    with patch.object(check_credentials, "network_is_up", return_value=False):
        assert not wait_for_network(
            SOURCES, delays=(5, 10, 20), sleep=sleep, on_wait=_silent
        )
    assert slept == [5, 10, 20], "should exhaust every delay before giving up"


def test_network_is_up_false_when_dns_fails():
    with patch(
        "socket.getaddrinfo",
        side_effect=socket.gaierror(8, "nodename nor servname provided"),
    ):
        assert not check_credentials.network_is_up(SOURCES)


def test_network_is_up_true_when_any_host_resolves():
    with patch("socket.getaddrinfo", return_value=[("fake",)]):
        assert check_credentials.network_is_up(SOURCES)


# ══════════════════════════════════════════════════════════════════════════════
#  Transport vs credential classification
# ══════════════════════════════════════════════════════════════════════════════


def test_dns_failure_is_a_transport_failure_not_a_bad_credential():
    dns_error = requests.exceptions.ConnectionError(
        "Failed to resolve 'bamboohr.atlassian.net' ([Errno 8] nodename nor "
        "servname provided, or not known)"
    )
    with patch.object(
        check_credentials.config, "JIRA_API_TOKEN", "token"
    ), patch.object(
        check_credentials.config, "JIRA_EMAIL", "someone@example.com"
    ), patch(
        "requests.get", side_effect=dns_error
    ):
        result = check_credentials.check_jira()

    assert not result.ok
    assert result.transport_failure, "a DNS failure must not read as a dead token"


def test_401_is_a_bad_credential_not_a_transport_failure():
    class _Response:
        status_code = 401

    with patch.object(
        check_credentials.config, "JIRA_API_TOKEN", "token"
    ), patch.object(
        check_credentials.config, "JIRA_EMAIL", "someone@example.com"
    ), patch(
        "requests.get", return_value=_Response()
    ):
        result = check_credentials.check_jira()

    assert not result.ok
    assert not result.transport_failure
    assert "expired or revoked" in result.detail


# ══════════════════════════════════════════════════════════════════════════════
#  offline()
# ══════════════════════════════════════════════════════════════════════════════


def test_offline_true_when_all_failures_are_transport():
    results = {
        "jira": CheckResult(False, "unreachable", True),
        "slite": CheckResult(False, "unreachable", True),
    }
    assert offline(results)


def test_offline_false_when_any_failure_is_a_real_credential_problem():
    results = {
        "jira": CheckResult(False, "unreachable", True),
        "slite": CheckResult(False, "HTTP 401", False),
    }
    assert not offline(results)


def test_offline_false_when_nothing_failed():
    results = {"jira": CheckResult(True, "authenticated", False)}
    assert not offline(results)
