"""
Golden-sample shape test for the MCP-backed Slack collector.

This is the primary guard against silent metadata drift after migrating the
collector's transport from the dead stealth (xoxc/xoxd) path to the official
claude_ai_Slack connector via the claude -p bridge. It asserts that:

  - reshape_bridge_messages produces the EXACT chunk dicts the collector has
    always returned (every documented metadata key present, correct chunk_type
    values, verbatim markdown headers), for all three chunk types.
  - collect_slack_messages, with the bridge mocked out, returns that same shape
    end-to-end through the default (mcp) backend, and that <@USERID> mentions in
    text are resolved using inline display names with no API fan-out.

No network, no Slack API, no claude -p process is invoked: the bridge function
is patched to return a fixed synthetic payload.
"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

# Make scripts/ importable from the tests/ directory.
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import slack_collector
from slack_collector import collect_slack_messages, reshape_bridge_messages
import slack_mcp_bridge

CHANNEL_NAME = "skysteelers"
CHANNEL_ID = "C08GM20DP29"


# Real connector output samples (captured 2026-06-24 from the live
# claude_ai_Slack connector via the direct MCP client). The connector wraps the
# formatted block in a JSON string with "messages" + "pagination_info".
CHANNEL_PAYLOAD = json.dumps(
    {
        "messages": (
            "Channel: #skysteelers (C08GM20DP29)\n\n"
            "=== Message from Breven Joyner <bjoyner@bamboohr.com> (U07SAMVGS22) "
            "at 2026-06-23 12:58:39 EDT === \n"
            "Message TS: 1782233919.007999\n"
            "Moving this <https://x|conversation> here.\n"
            "Thread: 4 replies (latest: 2026-06-23 13:25:25 EDT)\n\n"
            "=== Message from Tracie Segura <tsegura@bamboohr.com> (U02PEBNFSP6) "
            "at 2026-06-22 13:49:17 EDT === \n"
            "Message TS: 1782150557.786409\n"
            "Hi Skysteelers :slightly_smiling_face: I have a pr for you please\n"
            "Reactions: eyes (1)\n"
        ),
        "pagination_info": (
            "There are more messages available. To view the next page, use "
            "cursor: `bmV4dF90czoxNzgyMjM0MzYyNjgzMTk5`\n"
        ),
    }
)

THREAD_PAYLOAD = json.dumps(
    {
        "messages": (
            "=== THREAD PARENT MESSAGE ===\n"
            "From: Breven Joyner <bjoyner@bamboohr.com> (U07SAMVGS22)\n"
            "Time: 2026-06-23 12:58:39 EDT\n"
            "Message TS: 1782233919.007999\n"
            "Moving this here.\n\n"
            "=== THREAD REPLIES (2 total) ===\n\n"
            "--- Reply 1 of 2 ---\n"
            "From: Carla Gouveia <cgouveia@bamboohr.com> (U044HJ3GJRZ)\n"
            "Time: 2026-06-23 13:14:11 EDT\n"
            "Message TS: 1782234851.477379\n"
            "Not on my side.\n\n"
            "--- Reply 2 of 2 ---\n"
            "From: Claire Thomas <cthomas@bamboohr.com> (U09365460G3)\n"
            "Time: 2026-06-23 13:15:21 EDT\n"
            "Message TS: 1782234921.850399\n"
            "No concerns!\n"
            "Reactions: green_heart (1)\n"
        ),
        "pagination_info": "There are no more messages in this thread.\n",
    }
)


class TestChannelParser:
    """The deterministic parser for slack_read_channel text blocks."""

    def setup_method(self):
        block, self.pagination = slack_mcp_bridge._split_tool_payload(
            CHANNEL_PAYLOAD
        )
        self.records = slack_mcp_bridge.parse_channel_block(block)

    def test_parses_two_messages(self):
        assert len(self.records) == 2

    def test_first_is_thread_parent_with_reply_count(self):
        r = self.records[0]
        assert r["ts"] == "1782233919.007999"
        assert r["user_id"] == "U07SAMVGS22"
        assert r["user_display"] == "Breven Joyner"
        assert r["reply_count"] == 4
        # Thread:/Reactions: lines are metadata, not body text.
        assert "Thread:" not in r["text"]
        assert "Moving this" in r["text"]

    def test_second_is_standalone(self):
        r = self.records[1]
        assert r["ts"] == "1782150557.786409"
        assert r["reply_count"] == 0
        assert "Reactions:" not in r["text"]

    def test_cursor_extracted(self):
        assert (
            slack_mcp_bridge._extract_cursor(self.pagination)
            == "bmV4dF90czoxNzgyMjM0MzYyNjgzMTk5"
        )

    def test_no_cursor_when_absent(self):
        assert slack_mcp_bridge._extract_cursor("There are no more messages.") is None


class TestThreadParser:
    """The deterministic parser for slack_read_thread text blocks."""

    def setup_method(self):
        block, _ = slack_mcp_bridge._split_tool_payload(THREAD_PAYLOAD)
        self.records = slack_mcp_bridge.parse_thread_block(
            block, "1782233919.007999"
        )

    def test_parent_plus_replies(self):
        assert len(self.records) == 3  # 1 parent + 2 replies

    def test_parent_first(self):
        p = self.records[0]
        assert p["ts"] == "1782233919.007999"
        assert p["user_id"] == "U07SAMVGS22"
        assert p["user_display"] == "Breven Joyner"

    def test_replies_in_order(self):
        r1, r2 = self.records[1], self.records[2]
        assert r1["ts"] == "1782234851.477379"
        assert r1["user_display"] == "Carla Gouveia"
        assert r2["ts"] == "1782234921.850399"
        assert r2["user_display"] == "Claire Thomas"
        assert "Reactions:" not in r2["text"]


class TestTokenHandling:
    """Token resolution + headless refresh, with the Keychain NEVER written.

    These guard the safety-critical invariants:
      - a still-valid token (cache or Keychain) is used as-is (no needless work);
      - on expiry we do NOT independently refresh (that would rotate the refresh
        token and could log Claude Code out); we raise cleanly instead;
      - the Keychain is never written.
    """

    def test_valid_keychain_token_used_without_refresh(self):
        future = int((time.time() + 3600) * 1000)
        cred = {"claudeAiOauth": {
            "accessToken": "AT_VALID", "refreshToken": "RT", "expiresAt": future}}
        with patch.object(slack_mcp_bridge, "_read_cached_token", return_value=None), \
             patch.object(slack_mcp_bridge, "_read_keychain_credential", return_value=cred), \
             patch.object(slack_mcp_bridge, "_refresh_access_token") as mock_ref, \
             patch.object(slack_mcp_bridge, "_write_cached_token") as mock_write:
            assert slack_mcp_bridge._get_access_token() == "AT_VALID"
            assert mock_ref.call_count == 0
            assert mock_write.call_count == 0

    def test_expired_token_raises_without_forking_refresh_chain(self):
        """Expired Keychain token + no cache: must NOT independently refresh
        (that would rotate Claude Code's refresh token and risk logging it out).
        Raise cleanly, touch neither the refresh endpoint nor any cache write."""
        past = int((time.time() - 10) * 1000)
        cred = {"claudeAiOauth": {
            "accessToken": "AT_OLD", "refreshToken": "RT_OLD", "expiresAt": past}}
        with patch.object(slack_mcp_bridge, "_read_cached_token", return_value=None), \
             patch.object(slack_mcp_bridge, "_read_keychain_credential", return_value=cred), \
             patch.object(slack_mcp_bridge, "_refresh_access_token") as mock_ref, \
             patch.object(slack_mcp_bridge, "_write_cached_token") as mock_write:
            raised = False
            try:
                slack_mcp_bridge._get_access_token()
            except slack_mcp_bridge.SlackBridgeError:
                raised = True
            assert raised, "expected SlackBridgeError on expired token"
            assert mock_ref.call_count == 0
            assert mock_write.call_count == 0

    def test_fresh_side_cache_short_circuits(self):
        future = int((time.time() + 3600) * 1000)
        cached = {"accessToken": "AT_CACHED", "refreshToken": "RT", "expiresAt": future}
        with patch.object(slack_mcp_bridge, "_read_cached_token", return_value=cached), \
             patch.object(slack_mcp_bridge, "_read_keychain_credential") as mock_kc, \
             patch.object(slack_mcp_bridge, "_refresh_access_token") as mock_ref:
            assert slack_mcp_bridge._get_access_token() == "AT_CACHED"
            assert mock_kc.call_count == 0
            assert mock_ref.call_count == 0

    def test_module_never_writes_the_keychain(self):
        import inspect
        src = inspect.getsource(slack_mcp_bridge)
        # The only `security` invocation is the read; no add/update of the entry.
        assert "add-generic-password" not in src
        assert src.count("find-generic-password") == 1


class TestValidateMessages:
    """The shared validator must accept direct-transport output unchanged."""

    def test_validator_passes_direct_records(self):
        records = [
            {
                "ts": "1782233919.007999", "user_id": "U07SAMVGS22",
                "user_display": "Breven Joyner", "text": "hi",
                "thread_ts": "1782233919.007999", "is_parent": True,
                "is_reply": False, "reply_count": 2,
            },
            {
                "ts": "1782234851.477379", "user_id": "U044HJ3GJRZ",
                "user_display": "Carla Gouveia", "text": "reply",
                "thread_ts": "1782233919.007999", "is_parent": False,
                "is_reply": True, "reply_count": 0,
            },
        ]
        cleaned = slack_mcp_bridge._validate_messages(records)
        assert len(cleaned) == 2
        assert cleaned[0]["is_parent"] is True
        assert cleaned[1]["thread_ts"] == "1782233919.007999"


# A synthetic bridge payload covering all three chunk types: a thread parent
# with one reply, and a standalone message. Includes a <@USERID> mention to
# verify inline mention resolution.
def _bridge_payload():
    return [
        {
            "ts": "1782234362.683199",
            "user_id": "U03KEFP6JMQ",
            "user_display": "Scott Clayton",
            "text": "Hey <@U044HJ3GJRZ>, a question for you.",
            "thread_ts": "1782234362.683199",
            "is_parent": True,
            "is_reply": False,
            "reply_count": 1,
        },
        {
            "ts": "1782234793.845099",
            "user_id": "U044HJ3GJRZ",
            "user_display": "Carla Gouveia",
            "text": "Here is the answer.",
            "thread_ts": "1782234362.683199",
            "is_parent": False,
            "is_reply": True,
            "reply_count": 0,
        },
        {
            "ts": "1782252215.158459",
            "user_id": "U08JKE31KPV",
            "user_display": "Jacob Peterson",
            "text": "Standalone message, please CR my PR.",
            "thread_ts": None,
            "is_parent": False,
            "is_reply": False,
            "reply_count": 0,
        },
    ]


# The exact metadata key sets the downstream pipeline relies on, per the frozen
# contract documented in SLACK_MCP_MIGRATION.md.
PARENT_KEYS = {
    "channel_name", "channel_id", "message_ts", "thread_ts",
    "author_username", "author_display", "date", "timestamp",
    "is_thread_parent", "reply_count", "chunk_type", "filename", "filepath",
}
REPLY_KEYS = {
    "channel_name", "channel_id", "message_ts", "thread_ts",
    "thread_parent_author", "author_username", "author_display", "date",
    "timestamp", "is_thread_reply", "chunk_type", "filename", "filepath",
}
MESSAGE_KEYS = {
    "channel_name", "channel_id", "message_ts", "author_username",
    "author_display", "date", "timestamp", "is_thread", "chunk_type",
    "filename", "filepath",
}


class TestReshapeShape:
    def setup_method(self):
        self.chunks = reshape_bridge_messages(
            _bridge_payload(), CHANNEL_NAME, CHANNEL_ID
        )

    def test_produces_three_chunks(self):
        assert len(self.chunks) == 3

    def test_every_chunk_has_content_and_metadata(self):
        for c in self.chunks:
            assert set(c.keys()) == {"content", "metadata"}
            assert isinstance(c["content"], str) and c["content"]
            assert isinstance(c["metadata"], dict)

    def test_chunk_types_present(self):
        types = [c["metadata"]["chunk_type"] for c in self.chunks]
        assert types == ["thread_parent", "thread_reply", "message"]

    def test_thread_parent_metadata_keys_exact(self):
        parent = self.chunks[0]["metadata"]
        assert set(parent.keys()) == PARENT_KEYS
        assert parent["chunk_type"] == "thread_parent"
        assert parent["is_thread_parent"] is True
        assert parent["reply_count"] == 1
        assert parent["message_ts"] == "1782234362.683199"
        assert parent["thread_ts"] == "1782234362.683199"
        assert parent["channel_name"] == CHANNEL_NAME
        assert parent["channel_id"] == CHANNEL_ID
        assert parent["filename"] == "skysteelers_1782234362.683199.slack"
        assert parent["filepath"] == "slack://skysteelers/1782234362.683199"

    def test_thread_reply_metadata_keys_exact(self):
        reply = self.chunks[1]["metadata"]
        assert set(reply.keys()) == REPLY_KEYS
        assert reply["chunk_type"] == "thread_reply"
        assert reply["is_thread_reply"] is True
        # Reply links back to the parent ts, and names the parent author.
        assert reply["thread_ts"] == "1782234362.683199"
        assert reply["thread_parent_author"] == "Scott Clayton"
        assert reply["message_ts"] == "1782234793.845099"

    def test_standalone_message_metadata_keys_exact(self):
        msg = self.chunks[2]["metadata"]
        assert set(msg.keys()) == MESSAGE_KEYS
        assert msg["chunk_type"] == "message"
        assert msg["is_thread"] is False
        assert msg["message_ts"] == "1782252215.158459"

    def test_markdown_headers_verbatim(self):
        parent, reply, msg = self.chunks
        assert parent["content"].startswith(
            "# Thread started by @Scott Clayton in #skysteelers\n"
        )
        assert "**Replies:** 1" in parent["content"]
        assert reply["content"].startswith(
            "# Reply from @Carla Gouveia in #skysteelers thread\n"
        )
        assert "**Thread started by:** @Scott Clayton" in reply["content"]
        assert msg["content"].startswith(
            "# Message from @Jacob Peterson in #skysteelers\n"
        )

    def test_mentions_resolved_from_inline_names(self):
        # <@U044HJ3GJRZ> in the parent text should become @Carla Gouveia,
        # resolved purely from inline author names (no API call).
        parent_content = self.chunks[0]["content"]
        assert "@Carla Gouveia" in parent_content
        assert "<@U044HJ3GJRZ>" not in parent_content


class TestCollectEndToEndWithBridge:
    """collect_slack_messages through the default mcp backend, bridge mocked."""

    def test_end_to_end_shape(self):
        fake_channels = {CHANNEL_NAME: {"id": CHANNEL_ID, "name": CHANNEL_NAME}}

        with patch.object(
            slack_collector, "load_slack_channels", return_value=fake_channels
        ), patch.object(
            slack_collector, "fetch_channel_via_mcp",
            return_value=_bridge_payload(),
        ) as mock_fetch:
            chunks = collect_slack_messages(
                channel_names={CHANNEL_NAME: {}},
                token_file="unused",
                channels_file="unused",
                max_age_days=30,
                max_messages_per_channel=2000,
            )

        # Bridge was called once for the one channel, with the right channel id.
        assert mock_fetch.call_count == 1
        _, kwargs = mock_fetch.call_args
        assert kwargs["channel_id"] == CHANNEL_ID
        assert kwargs["max_messages"] == 2000

        # Output shape matches the contract.
        assert len(chunks) == 3
        types = [c["metadata"]["chunk_type"] for c in chunks]
        assert types == ["thread_parent", "thread_reply", "message"]
        assert set(chunks[0]["metadata"].keys()) == PARENT_KEYS
        assert set(chunks[1]["metadata"].keys()) == REPLY_KEYS
        assert set(chunks[2]["metadata"].keys()) == MESSAGE_KEYS
