"""
Tests for per-channel max_age_days overrides in slack_collector.

Verifies:
  - Channel with empty override dict uses the function-level default max_age_days.
  - Channel with max_age_days override uses the override value.
  - Unknown override keys are ignored (do not crash).
  - Legacy list-of-strings input is accepted and treated as all-defaults.

These tests stub out the Slack API (requests.post) and config/file loaders so
no network or disk access occurs. They only verify timestamp computation and
argument wiring from collect_slack_messages into the Slack API call.
"""

import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

# Make scripts/ importable from the tests/ directory
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import slack_collector
from slack_collector import collect_slack_messages

# ══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════════


def _fake_tokens():
    return {"xoxc": "xoxc-test", "xoxd": "xoxd-test"}


def _fake_channels_map(names):
    """Build the channels.json-style map that load_slack_channels returns."""
    return {name: {"id": f"C{name.upper()}", "name": name} for name in names}


def _empty_history_response():
    """Slack conversations.history response with no messages, no pagination."""
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"ok": True, "messages": [], "has_more": False})
    return resp


def _run_collector(channels_arg, default_max_age_days=30):
    """
    Invoke collect_slack_messages with all I/O stubbed. Returns the list of
    (channel_name, oldest_float) pairs captured from each Slack API call.
    """
    channel_names = (
        list(channels_arg.keys())
        if isinstance(channels_arg, dict)
        else list(channels_arg)
    )
    fake_channels = _fake_channels_map(channel_names)

    captured_calls = []

    def fake_post(url, headers=None, data=None, **kwargs):
        # data["channel"] is the channel id; data["oldest"] is the unix ts.
        captured_calls.append(
            {
                "channel_id": data["channel"],
                "oldest": float(data["oldest"]),
            }
        )
        return _empty_history_response()

    with patch.object(
        slack_collector, "load_slack_tokens", return_value=_fake_tokens()
    ), patch.object(
        slack_collector, "load_slack_channels", return_value=fake_channels
    ), patch.object(
        slack_collector, "load_user_cache", return_value={}
    ), patch.object(
        slack_collector.requests, "post", side_effect=fake_post
    ):
        collect_slack_messages(
            channel_names=channels_arg,
            token_file="unused",
            channels_file="unused",
            max_age_days=default_max_age_days,
            max_messages_per_channel=200,
        )

    # Map channel_id back to channel_name for readable assertions.
    id_to_name = {info["id"]: name for name, info in fake_channels.items()}
    return [(id_to_name[c["channel_id"]], c["oldest"]) for c in captured_calls]


# A tolerance window (seconds) to account for time.time() drift between the
# assertion's expected value and the collector's internal call.
DRIFT_SECONDS = 5


def _expected_oldest(days):
    return time.time() - (days * 24 * 60 * 60)


# ══════════════════════════════════════════════════════════════════════════════
#  Tests
# ══════════════════════════════════════════════════════════════════════════════


class TestPerChannelMaxAgeOverrides:
    def test_empty_override_uses_default(self):
        """Channel with {} override should use the function-level default."""
        calls = _run_collector({"development": {}}, default_max_age_days=30)
        assert len(calls) == 1
        name, oldest = calls[0]
        assert name == "development"
        assert abs(oldest - _expected_oldest(30)) < DRIFT_SECONDS

    def test_max_age_override_is_applied(self):
        """Channel with max_age_days override should use the override value."""
        calls = _run_collector(
            {"docker-support": {"max_age_days": 180}},
            default_max_age_days=30,
        )
        assert len(calls) == 1
        name, oldest = calls[0]
        assert name == "docker-support"
        # 180-day window should be older than 30-day window.
        assert abs(oldest - _expected_oldest(180)) < DRIFT_SECONDS
        assert oldest < _expected_oldest(30)

    def test_mixed_channels_get_independent_windows(self):
        """Multiple channels each get their own effective max_age_days."""
        calls = _run_collector(
            {
                "development": {},
                "docker-support": {"max_age_days": 180},
            },
            default_max_age_days=30,
        )
        by_name = dict(calls)
        assert set(by_name.keys()) == {"development", "docker-support"}
        assert abs(by_name["development"] - _expected_oldest(30)) < DRIFT_SECONDS
        assert abs(by_name["docker-support"] - _expected_oldest(180)) < DRIFT_SECONDS

    def test_unknown_override_keys_are_ignored(self):
        """Unknown override keys should not crash and should not affect behavior."""
        calls = _run_collector(
            {"development": {"unknown_key": "whatever", "another": 42}},
            default_max_age_days=30,
        )
        assert len(calls) == 1
        name, oldest = calls[0]
        assert name == "development"
        # Falls back to the default since max_age_days wasn't provided.
        assert abs(oldest - _expected_oldest(30)) < DRIFT_SECONDS

    def test_legacy_list_input_treated_as_all_defaults(self):
        """A plain list[str] (legacy shape) should be accepted and use defaults."""
        calls = _run_collector(
            ["development", "skysteelers-team"],
            default_max_age_days=30,
        )
        by_name = dict(calls)
        assert set(by_name.keys()) == {"development", "skysteelers-team"}
        for _, oldest in calls:
            assert abs(oldest - _expected_oldest(30)) < DRIFT_SECONDS
