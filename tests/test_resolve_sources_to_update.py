"""
Tests for resolve_sources_to_update in unified_indexer.

This is the guard that prevents the delete-then-readd indexer from destroying a
source's chunks when its collector fails or returns empty. Indexing wipes every
existing chunk for a source before adding the freshly collected ones, so the
wipe list MUST be derived from what was actually collected — never from the
request flags. A source that collected 0 chunks (missing config, expired auth,
rate-limit, transient MCP hiccup) must be left untouched, not wiped.

Regression context: the Slack index was silently zeroed when its config file
went missing — collection returned 0, but "slack" was still wiped because the
wipe keyed off the request flag instead of the collected output.
"""

import sys
from pathlib import Path

# Make scripts/ importable from the tests/ directory
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from unified_indexer import resolve_sources_to_update


def _meta(source):
    return {"source": source, "document": "x"}


def test_only_collected_sources_are_wiped():
    """A requested source with 0 collected chunks is NOT in the wipe list."""
    # slack was requested but collected nothing; jira/code came back with data.
    all_metadatas = [_meta("jira"), _meta("jira"), _meta("code")]
    requested = ["jira", "code", "slack"]

    updated, skipped = resolve_sources_to_update(all_metadatas, requested)

    assert updated == ["code", "jira"]  # sorted, slack absent → not wiped
    assert skipped == ["slack"]


def test_empty_source_is_reported_as_skipped():
    """Every requested-but-empty source is surfaced in skipped_sources."""
    all_metadatas = [_meta("code")]
    requested = ["code", "slack", "slite"]

    updated, skipped = resolve_sources_to_update(all_metadatas, requested)

    assert updated == ["code"]
    assert skipped == ["slack", "slite"]  # preserves requested order


def test_all_sources_healthy():
    """When every requested source has data, all are wiped-and-replaced."""
    all_metadatas = [_meta("slack"), _meta("jira"), _meta("code")]
    requested = ["slack", "jira", "code"]

    updated, skipped = resolve_sources_to_update(all_metadatas, requested)

    assert updated == ["code", "jira", "slack"]
    assert skipped == []


def test_total_collection_failure_wipes_nothing():
    """If the whole run collected 0 chunks, nothing is wiped — index preserved."""
    updated, skipped = resolve_sources_to_update([], ["slack", "jira", "code"])

    assert updated == []  # no source wiped
    assert skipped == ["slack", "jira", "code"]


def test_none_source_values_are_ignored():
    """Metadata missing a 'source' key never produces a bogus wipe target."""
    all_metadatas = [{"document": "x"}, _meta("slack")]
    requested = ["slack"]

    updated, skipped = resolve_sources_to_update(all_metadatas, requested)

    assert updated == ["slack"]  # None filtered out, no crash
    assert skipped == []


def test_unrequested_but_collected_source_still_wiped():
    """
    The wipe list is driven by collected data, not the request list — a source
    present in the collected chunks is wiped even if not in requested_sources
    (keeps the wipe and the add perfectly aligned on the same chunk set).
    """
    all_metadatas = [_meta("slack")]
    requested = []  # nothing explicitly requested, but slack chunks exist

    updated, skipped = resolve_sources_to_update(all_metadatas, requested)

    assert updated == ["slack"]
    assert skipped == []
