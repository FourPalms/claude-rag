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

Run standalone:
    python3 scripts/check_credentials.py          # all sources
    python3 scripts/check_credentials.py jira     # named sources only

Exit code is 0 when every checked credential is valid, 1 when any is not, so it
is usable as a shell guard.

No credential value is ever printed, logged, or included in a returned message.
"""

import sys
from pathlib import Path
from typing import Dict, List, Tuple

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


def check_jira() -> Tuple[bool, str]:
    """Validate the Jira API token via the identity endpoint."""
    if not config.JIRA_API_TOKEN:
        return False, "JIRA_API_TOKEN not set in ~/.secrets.env"
    if not config.JIRA_EMAIL:
        return False, "JIRA_USERNAME / JIRA_EMAIL not set in ~/.secrets.env"

    url = config.JIRA_URL.rstrip("/") + "/rest/api/3/myself"
    try:
        response = requests.get(
            url,
            auth=(config.JIRA_EMAIL, config.JIRA_API_TOKEN),
            timeout=CHECK_TIMEOUT,
        )
    except requests.exceptions.RequestException as request_error:
        return False, f"Jira unreachable: {request_error}"

    if response.status_code == 200:
        return True, f"authenticated as {response.json().get('displayName')}"
    if response.status_code in (401, 403):
        return False, (
            f"HTTP {response.status_code} — token expired or revoked. Mint a new "
            "one at https://id.atlassian.com/manage-profile/security/api-tokens "
            "and update JIRA_API_TOKEN in ~/.secrets.env"
        )
    return False, f"unexpected HTTP {response.status_code}"


def check_slack() -> Tuple[bool, str]:
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
        return False, f"slack_mcp_bridge unavailable: {import_error}"

    try:
        token = _get_access_token()
    except SlackBridgeError as bridge_error:
        return False, (
            f"{bridge_error} -- re-authorize the claude.ai Slack connector; "
            "the bridge credential is stored in the login keychain"
        )
    except Exception as unexpected:  # noqa: BLE001 - a probe must not raise
        return False, f"token retrieval failed: {unexpected}"

    if not token:
        return False, "bridge returned no access token"
    return True, "MCP bridge credential valid"


def check_slite() -> Tuple[bool, str]:
    """Validate the Slite API key by fetching a configured root note."""
    if not config.SLITE_API_KEY:
        return False, "SLITE_API_KEY not set in ~/.secrets.env"
    if not config.SLITE_ROOT_NOTE_IDS:
        return True, "no root notes configured (nothing to check)"

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
        return False, f"Slite unreachable: {request_error}"

    if response.status_code == 200:
        return True, "API key valid"
    if response.status_code in (401, 403):
        return False, (
            f"HTTP {response.status_code} — API key rejected. Update "
            "SLITE_API_KEY in ~/.secrets.env"
        )
    if response.status_code == 429:
        # Rate limiting says nothing about the key's validity, so do not
        # report a good credential as bad.
        return True, "rate-limited (429) — key presumed valid"
    return False, f"unexpected HTTP {response.status_code}"


def check_meetings() -> Tuple[bool, str]:
    """Validate the Google Drive credential by minting an access token."""
    if not getattr(config, "MEETING_DOCS_ENABLED", False):
        return True, "meeting docs disabled (nothing to check)"

    # Imported lazily: drive_collector pulls in the chunker, and a preflight
    # should not pay that cost when meetings are not being indexed.
    from drive_collector import DriveAuthError, _access_token, _list_meeting_docs

    try:
        token = _access_token()
    except DriveAuthError as auth_error:
        return False, str(auth_error)
    except Exception as unexpected:  # noqa: BLE001 - a probe must not raise
        return False, f"token refresh failed: {unexpected}"

    # A token can be valid but lack drive.readonly, since it is shared with the
    # google-docs MCP server and consented for that server's needs. One
    # single-document list is the cheapest way to prove the scope is present.
    try:
        _list_meeting_docs(token, config.MEETING_DOC_QUERY, max_docs=1)
    except DriveAuthError as scope_error:
        return False, str(scope_error)
    except Exception as unexpected:  # noqa: BLE001
        return False, f"Drive unreachable: {unexpected}"

    return True, "Drive token valid, drive.readonly present"


CHECKS = {
    "jira": check_jira,
    "slack": check_slack,
    "slite": check_slite,
    "meetings": check_meetings,
}


def check_sources(sources: List[str]) -> Dict[str, Tuple[bool, str]]:
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


def format_results(results: Dict[str, Tuple[bool, str]]) -> List[str]:
    """Render results as display lines. Returns failures only, for alerting."""
    failures = []
    for source, (ok, detail) in sorted(results.items()):
        marker = "✓" if ok else "✗"
        print(f"  {marker} {source}: {detail}")
        if not ok:
            failures.append(f"{source}: {detail}")
    return failures


def main() -> int:
    requested = [a.lower() for a in sys.argv[1:]] or list(CHECKABLE_SOURCES)
    unknown = [s for s in requested if s not in CHECKS]
    if unknown:
        print(f"Unknown source(s): {', '.join(unknown)}")
        print(f"Checkable: {', '.join(CHECKABLE_SOURCES)}")
        return 2

    print("Checking API credentials...")
    failures = format_results(check_sources(requested))

    if failures:
        print(f"\n{len(failures)} credential(s) need attention.")
        return 1
    print("\nAll checked credentials valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
