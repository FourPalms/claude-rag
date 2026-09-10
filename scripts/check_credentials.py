#!/usr/bin/env python3
"""
Preflight credential check for the RAG indexer.

Every API-backed source (Jira, Slite, meeting docs) authenticates with a
credential that expires: Atlassian API tokens now carry a hard expiry, Slite
API keys can be revoked, and the Google refresh token behind meeting docs is
shared with the google-docs MCP server, so a re-consent there invalidates it
here. When one lapses, the affected source quietly collects zero documents. The indexer
preserves the stale chunks rather than purging them (correct — better stale
than empty), which means a search still returns plausible-looking results and
nothing announces that the data stopped moving.

On 2026-08-10 both the Jira token and the Slack tokens turned out to be dead,
discovered only incidentally while fixing an unrelated outage.

This module answers one question per source — "would this credential work right
now?" — using the cheapest authenticated endpoint each API offers, so it can run
as a fast preflight before the expensive collection work begins.

A check can fail for two unrelated reasons, and conflating them produces a bad
alert. On 2026-09-10 the 8am run fired while the laptop was in a car with no
network: DNS could not resolve anything, all three API checks failed on
transport, and the run reported "3 credential(s) need attention" — pointing at
tokens that were perfectly healthy. So every result carries a
`transport_failure` flag, and callers word the alert accordingly.

Run standalone:
    python3 scripts/check_credentials.py          # all sources
    python3 scripts/check_credentials.py jira     # named sources only

Exit code is 0 when every checked credential is valid, 1 when any is not, so it
is usable as a shell guard.

No credential value is ever printed, logged, or included in a returned message.
"""

import socket
import sys
import time
from pathlib import Path
from typing import Dict, List, NamedTuple, Sequence
from urllib.parse import urlparse

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

# Preflight checks must not become the reason a run is slow, and a credential
# that needs more than a few seconds to validate is indistinguishable from one
# that is broken for our purposes.
CHECK_TIMEOUT = (5, 10)

# Sources this module knows how to validate. Sources without a credential
# (code, sessions, sanctum — all local filesystem reads) are intentionally
# absent: there is nothing to expire.
CHECKABLE_SOURCES = ("jira", "slack", "slite", "meetings")

# Backoff schedule for waiting out a missing network, in seconds between
# attempts. The case this is sized for is a laptop that woke, or left the
# house, moments before the run fired — the interface usually associates within
# a minute. Six attempts spread over ~3.5 minutes covers that without eating a
# meaningful share of the wrapper's 2-hour cap when the machine is genuinely
# offline.
NETWORK_RETRY_DELAYS = (5, 10, 20, 40, 60, 90)


class CheckResult(NamedTuple):
    """
    Outcome of one credential check.

    `transport_failure` separates "we could not reach the API" from "the API
    told us this credential is no good". Only the latter is something a human
    can act on.
    """

    ok: bool
    detail: str
    transport_failure: bool = False


def _ok(detail: str) -> CheckResult:
    return CheckResult(True, detail, False)


def _bad_credential(detail: str) -> CheckResult:
    return CheckResult(False, detail, False)


def _unreachable(detail: str) -> CheckResult:
    return CheckResult(False, detail, True)


def _api_hosts(sources: Sequence[str]) -> List[str]:
    """
    Hostnames the given sources need to resolve.

    Only hosts we can name without a credential are listed; the point is to
    prove a working resolver exists, not to prove any particular API is up.
    """
    hosts = []
    for source in sources:
        if source == "jira" and config.JIRA_URL:
            host = urlparse(config.JIRA_URL).hostname
            if host:
                hosts.append(host)
        elif source == "slite":
            hosts.append("api.slite.com")
        elif source == "meetings":
            hosts.append("www.googleapis.com")
        elif source == "slack":
            hosts.append("slack.com")
    return hosts


def network_is_up(sources: Sequence[str]) -> bool:
    """
    True when at least one relevant hostname resolves.

    Deliberately DNS-only: resolution is the step that fails when a machine has
    no route out, it costs milliseconds, and it does not care whether a given
    API is currently healthy. One host resolving is enough — a single API being
    down is the per-source checks' problem, not this gate's.
    """
    for host in _api_hosts(sources) or ["api.slite.com"]:
        try:
            socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
            return True
        except socket.gaierror:
            continue
        except OSError:
            continue
    return False


def wait_for_network(
    sources: Sequence[str],
    delays: Sequence[int] = NETWORK_RETRY_DELAYS,
    sleep=time.sleep,
    on_wait=print,
) -> bool:
    """
    Wait out a transient network outage before declaring the API sources dead.

    The run is scheduled by launchd at a fixed hour, so it regularly fires at
    whatever moment the laptop happens to be in — mid-wake, mid-commute,
    between an office network and a tethered phone. On 2026-09-10 it fired in a
    car and every API check failed DNS resolution. Interfaces normally
    associate within a minute of that, so a short backoff converts a lost day
    of indexing into a slightly later run.

    Returns True as soon as the network answers, False once the schedule is
    exhausted. `sleep` and `on_wait` are injectable so tests need not idle.
    """
    if network_is_up(sources):
        return True

    for attempt, delay in enumerate(delays, start=1):
        on_wait(
            f"  no network — retrying in {delay}s " f"(attempt {attempt}/{len(delays)})"
        )
        sleep(delay)
        if network_is_up(sources):
            on_wait(
                f"  network back after {attempt} retr"
                f"{'y' if attempt == 1 else 'ies'}"
            )
            return True
    return False


def check_jira() -> CheckResult:
    """Validate the Jira API token via the identity endpoint."""
    if not config.JIRA_API_TOKEN:
        return _bad_credential("JIRA_API_TOKEN not set in ~/.secrets.env")
    if not config.JIRA_EMAIL:
        return _bad_credential("JIRA_USERNAME / JIRA_EMAIL not set in ~/.secrets.env")

    url = config.JIRA_URL.rstrip("/") + "/rest/api/3/myself"
    try:
        response = requests.get(
            url,
            auth=(config.JIRA_EMAIL, config.JIRA_API_TOKEN),
            timeout=CHECK_TIMEOUT,
        )
    except requests.exceptions.RequestException as request_error:
        return _unreachable(f"Jira unreachable: {request_error}")

    if response.status_code == 200:
        return _ok(f"authenticated as {response.json().get('displayName')}")
    if response.status_code in (401, 403):
        return _bad_credential(
            f"HTTP {response.status_code} — token expired or revoked. Mint a new "
            "one at https://id.atlassian.com/manage-profile/security/api-tokens "
            "and update JIRA_API_TOKEN in ~/.secrets.env"
        )
    return _bad_credential(f"unexpected HTTP {response.status_code}")


def check_slack() -> CheckResult:
    """
    Validate the Slack credential behind the MCP bridge.

    Probes the path ingestion actually uses. The previous version validated
    xoxc/xoxd browser tokens, which Slack Enterprise force-logs-out and which
    slack_mcp_bridge.py never reads -- so it reported a failure while Slack was
    indexing fine.

    Depth: this proves the OAuth credential can produce an access token, which
    is the failure mode that bites (a revoked or expired refresh token). It
    does NOT open an MCP session or fetch a channel, because that costs a full
    client handshake and a preflight should stay cheap. A pass here means the
    credential is alive, not that every channel is reachable.
    """
    try:
        from slack_mcp_bridge import SlackBridgeError, _get_access_token
    except ImportError as import_error:
        return _bad_credential(f"slack_mcp_bridge unavailable: {import_error}")

    try:
        token = _get_access_token()
    except SlackBridgeError as bridge_error:
        return _bad_credential(
            f"{bridge_error} -- re-authorize the claude.ai Slack connector; "
            "the bridge credential is stored in the login keychain"
        )
    except requests.exceptions.RequestException as request_error:
        # The bridge refreshes over the network, so it fails the same way the
        # HTTP checks do when there is no route out.
        return _unreachable(f"Slack unreachable: {request_error}")
    except Exception as unexpected:  # noqa: BLE001 - a probe must not raise
        return _bad_credential(f"token retrieval failed: {unexpected}")

    if not token:
        return _bad_credential("bridge returned no access token")
    return _ok("MCP bridge credential valid")


def check_slite() -> CheckResult:
    """Validate the Slite API key by fetching a configured root note."""
    if not config.SLITE_API_KEY:
        return _bad_credential("SLITE_API_KEY not set in ~/.secrets.env")
    if not config.SLITE_ROOT_NOTE_IDS:
        return _ok("no root notes configured (nothing to check)")

    note_id = config.SLITE_ROOT_NOTE_IDS[0]
    url = f"https://api.slite.com/v1/notes/{note_id}"
    try:
        response = requests.get(
            url,
            headers={
                "x-slite-api-key": config.SLITE_API_KEY,
                "Accept": "application/json",
            },
            timeout=CHECK_TIMEOUT,
        )
    except requests.exceptions.RequestException as request_error:
        return _unreachable(f"Slite unreachable: {request_error}")

    if response.status_code == 200:
        return _ok("API key valid")
    if response.status_code in (401, 403):
        return _bad_credential(
            f"HTTP {response.status_code} — API key rejected. Update "
            "SLITE_API_KEY in ~/.secrets.env"
        )
    if response.status_code == 429:
        # Rate limiting says nothing about the key's validity, so do not
        # report a good credential as bad.
        return _ok("rate-limited (429) — key presumed valid")
    return _bad_credential(f"unexpected HTTP {response.status_code}")


def check_meetings() -> CheckResult:
    """Validate the Google Drive credential by minting an access token."""
    if not getattr(config, "MEETING_DOCS_ENABLED", False):
        return _ok("meeting docs disabled (nothing to check)")

    # Imported lazily: drive_collector pulls in the chunker, and a preflight
    # should not pay that cost when meetings are not being indexed.
    from drive_collector import DriveAuthError, _access_token, _list_meeting_docs

    try:
        token = _access_token()
    except DriveAuthError as auth_error:
        return _bad_credential(str(auth_error))
    except requests.exceptions.RequestException as request_error:
        return _unreachable(f"Drive unreachable: {request_error}")
    except Exception as unexpected:  # noqa: BLE001 - a probe must not raise
        return _bad_credential(f"token refresh failed: {unexpected}")

    # A token can be valid but lack drive.readonly, since it is shared with the
    # google-docs MCP server and consented for that server's needs. One
    # single-document list is the cheapest way to prove the scope is present.
    try:
        _list_meeting_docs(token, config.MEETING_DOC_QUERY, max_docs=1)
    except DriveAuthError as scope_error:
        return _bad_credential(str(scope_error))
    except requests.exceptions.RequestException as request_error:
        return _unreachable(f"Drive unreachable: {request_error}")
    except Exception as unexpected:  # noqa: BLE001
        return _bad_credential(f"Drive unreachable: {unexpected}")

    return _ok("Drive token valid, drive.readonly present")


CHECKS = {
    "jira": check_jira,
    "slack": check_slack,
    "slite": check_slite,
    "meetings": check_meetings,
}


def check_sources(sources: List[str]) -> Dict[str, CheckResult]:
    """
    Validate the credential for each named source.

    Sources with no credential to check are skipped rather than reported, so
    callers can pass their full source list without filtering first.
    """
    results = {}
    for source in sources:
        if source in CHECKS:
            results[source] = CHECKS[source]()
    return results


def format_results(results: Dict[str, CheckResult]) -> List[str]:
    """Render results as display lines. Returns failures only, for alerting."""
    failures = []
    for source, result in sorted(results.items()):
        marker = "✓" if result.ok else "✗"
        print(f"  {marker} {source}: {result.detail}")
        if not result.ok:
            failures.append(f"{source}: {result.detail}")
    return failures


def offline(results: Dict[str, CheckResult]) -> bool:
    """
    True when every failure was a transport failure and at least one occurred.

    Distinguishes "this machine has no network" from "a token needs renewing",
    which is the difference between an alert worth waking up to and noise.
    """
    failures = [r for r in results.values() if not r.ok]
    return bool(failures) and all(r.transport_failure for r in failures)


def main() -> int:
    requested = [a.lower() for a in sys.argv[1:]] or list(CHECKABLE_SOURCES)
    unknown = [s for s in requested if s not in CHECKS]
    if unknown:
        print(f"Unknown source(s): {', '.join(unknown)}")
        print(f"Checkable: {', '.join(CHECKABLE_SOURCES)}")
        return 2

    if not wait_for_network(requested):
        print("No network — cannot validate credentials.")
        return 1

    print("Checking API credentials...")
    results = check_sources(requested)
    failures = format_results(results)

    if failures:
        if offline(results):
            print(
                f"\n{len(failures)} source(s) unreachable — network problem, "
                "not a credential problem."
            )
        else:
            print(f"\n{len(failures)} credential(s) need attention.")
        return 1
    print("\nAll checked credentials valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
