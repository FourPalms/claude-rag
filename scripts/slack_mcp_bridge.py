"""
Slack MCP bridge for the RAG collector.

Why this exists
---------------
BambooHR moved to Slack Enterprise, which killed the old stealth-token
(`xoxc`/`xoxd`) transport: every direct slack.com API call now triggers an
immediate forced logout. The sanctioned replacement is the official
``claude_ai_Slack`` connector (claude.ai-managed OAuth).

Transport
---------
Two transports produce the SAME validated output (selected by env
``SLACK_BRIDGE_TRANSPORT``):

  - ``direct`` (DEFAULT): a standalone Python MCP client talks JSON-RPC over
    Streamable HTTP directly to Anthropic's claude.ai MCP proxy, reusing the
    same claude.ai OAuth token Claude Code itself stores in the macOS Keychain.
    NO LLM is in the loop: it calls ``slack_read_channel`` / ``slack_read_thread``
    and parses the connector's human-formatted text blocks into per-message
    records deterministically. ~seconds/channel, $0 LLM cost.

  - ``claude_p``: the legacy fallback. Shells out to a headless ``claude -p``
    invocation (which carries the connector) and has it dump a JSON array to a
    temp file. ~minutes/channel, ~$0.64/channel in LLM tokens. Kept only as an
    emergency path; do not use unless the direct path is broken.

This module ONLY fetches and normalizes. It does NOT reshape into the RAG
chunk contract -- that lives in ``slack_collector.reshape_bridge_messages`` so
the collector keeps full ownership of its output shape.

The shipped JSON schema (one object per message, parents and replies flattened
into a single array, newest-first preserved from the connector):

    {
      "ts":          "<slack ts string, e.g. 1782252215.158459>",
      "user_id":     "<U... or '' if unknown>",
      "user_display":"<inline display name from the connector, e.g. 'Jacob Peterson'>",
      "text":        "<message text, Slack mrkdwn, mentions left raw>",
      "thread_ts":   "<parent ts if this message is part of a thread, else null>",
      "is_parent":   true|false,   # true for a thread root that has replies
      "is_reply":    true|false,   # true for a reply under a parent
      "reply_count": <int>         # only meaningful on a parent; 0 otherwise
    }

Empirically confirmed (2026-06-24) against the live connector:
- ``slack_read_channel`` accepts ``channel_id`` (required), ``oldest``,
  ``latest``, ``limit`` (1-100, default 100), ``cursor`` (cursor-based
  pagination; the cursor is carried in a ``pagination_info`` text line), and
  returns ``result.content[0].text`` as a JSON string whose ``messages`` field
  is a human-formatted text block (one ``=== Message from ... ===`` stanza per
  message, with ``Message TS:`` and an optional ``Thread: N replies`` line).
- ``slack_read_thread`` takes ``channel_id`` + ``message_ts`` (the parent ts;
  ``thread_ts`` is rejected) and returns a formatted block: a
  ``=== THREAD PARENT MESSAGE ===`` stanza then ``--- Reply N of M ---`` stanzas.
- The connector returns the author display name INLINE on every message, so no
  separate users.info / profile fan-out is needed for author resolution.
  (In-text ``<@USERID>`` mentions are still raw mrkdwn; the collector handles
  those.)
"""

import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import httpx
except ImportError:  # pragma: no cover - httpx is a hard dep of the direct path
    httpx = None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Transport selector: "direct" (no-LLM Python MCP client, default) or
# "claude_p" (legacy headless claude -p fallback).
BRIDGE_TRANSPORT = os.environ.get("SLACK_BRIDGE_TRANSPORT", "direct").strip().lower()

# claude.ai MCP proxy endpoint for the Slack connector. The mcpsrv id is a
# stable identifier captured from Claude Code's own connector resolution
# (see slack-connector-direct-mcp-20260624.md). Overridable via env in case it
# ever rotates.
SLACK_MCPSRV_ID = os.environ.get(
    "SLACK_MCPSRV_ID", "mcpsrv_01SN3YmAELmqeENxYoc2igYQ"
)
MCP_PROXY_BASE = "https://mcp-proxy.anthropic.com/v1/mcp"

# macOS Keychain entry Claude Code stores its claude.ai OAuth credential in.
KEYCHAIN_SERVICE = "Claude Code-credentials"

# claude.ai OAuth token endpoint + public Claude Code client id, used ONLY for
# the headless refresh-token exchange when the cached access token is expired.
OAUTH_TOKEN_URL = os.environ.get(
    "CLAUDE_OAUTH_TOKEN_URL", "https://console.anthropic.com/v1/oauth/token"
)
OAUTH_CLIENT_ID = os.environ.get(
    "CLAUDE_OAUTH_CLIENT_ID", "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
)

# Where a refreshed token is cached so we never write back to (and risk
# desyncing) Claude Code's own Keychain credential. See _get_access_token.
REFRESH_CACHE_PATH = Path(
    os.environ.get(
        "SLACK_MCP_TOKEN_CACHE",
        os.path.expanduser("~/.claude/state/slack-tools/mcp-token-cache.json"),
    )
)

# HTTP timeout per MCP call (seconds). The connector itself is sub-second; this
# is generous headroom for a slow proxy hop.
MCP_HTTP_TIMEOUT = int(os.environ.get("SLACK_MCP_HTTP_TIMEOUT", "60"))

# Page size for slack_read_channel (server caps at 100).
CHANNEL_PAGE_LIMIT = 100
# Page size for slack_read_thread (server allows up to 1000).
THREAD_PAGE_LIMIT = 1000

# --- legacy claude -p fallback config (unchanged behavior) ---
BRIDGE_MODEL = os.environ.get("SLACK_BRIDGE_MODEL", "sonnet")
ALLOWED_TOOLS = [
    "mcp__claude_ai_Slack__slack_read_channel",
    "mcp__claude_ai_Slack__slack_read_thread",
    "mcp__claude_ai_Slack__slack_read_user_profile",
    "Write",
]
BRIDGE_TIMEOUT_SECONDS = int(os.environ.get("SLACK_BRIDGE_TIMEOUT", "600"))


class SlackBridgeError(RuntimeError):
    """Raised when a bridge transport fails to produce a valid result."""


# ---------------------------------------------------------------------------
# Token handling (Keychain read + headless refresh, NO write-back)
# ---------------------------------------------------------------------------

def _read_keychain_credential() -> Dict:
    """Read and parse the Claude Code OAuth credential from the macOS Keychain.

    Returns the full parsed JSON dict (the ``claudeAiOauth`` block lives under
    the ``claudeAiOauth`` key). We never log any token value.
    """
    try:
        raw = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except subprocess.CalledProcessError as e:
        raise SlackBridgeError(
            "Could not read the Claude Code credential from the macOS Keychain "
            f"(service '{KEYCHAIN_SERVICE}'). Is Claude Code logged in? "
            f"security exit {e.returncode}."
        ) from e
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise SlackBridgeError(
            "Keychain credential is not valid JSON."
        ) from e


def _read_cached_token() -> Optional[Dict]:
    """Read a previously-refreshed token from our private side cache, if any.

    Returns a dict with at least ``accessToken`` and ``expiresAt`` (ms epoch),
    or None if no usable cache exists.
    """
    try:
        with open(REFRESH_CACHE_PATH, "r") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict) or not data.get("accessToken"):
        return None
    return data


def _write_cached_token(access_token: str, refresh_token: str, expires_at_ms: int) -> None:
    """Persist a refreshed token to our private side cache (0600).

    CRITICAL: we deliberately do NOT write back to the macOS Keychain. Claude
    code's stored credential is the single source of truth for its own login,
    and the claude.ai refresh flow rotates the refresh token; writing a value
    back could desync or corrupt Claude Code's login. Caching the refreshed
    token in our own file keeps the Keychain untouched.
    """
    try:
        REFRESH_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = REFRESH_CACHE_PATH.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(
                {
                    "accessToken": access_token,
                    "refreshToken": refresh_token,
                    "expiresAt": expires_at_ms,
                },
                f,
            )
        os.chmod(tmp, 0o600)
        os.replace(tmp, REFRESH_CACHE_PATH)
    except OSError:
        # Caching is best-effort; if it fails we simply refresh again next time.
        pass


def _is_expired(expires_at_ms: Optional[int], skew_seconds: int = 120) -> bool:
    """True if the token is expired or within ``skew_seconds`` of expiry."""
    if not expires_at_ms:
        return True
    return (expires_at_ms / 1000.0) <= (time.time() + skew_seconds)


def _refresh_access_token(refresh_token: str) -> Tuple[str, str, int]:
    """Exchange a refresh token for a fresh access token via claude.ai OAuth.

    Returns (access_token, new_refresh_token, expires_at_ms).

    This is the standard OAuth2 refresh_token grant against the claude.ai token
    endpoint with the public Claude Code client id. The response rotates the
    refresh token, which is exactly why we keep the result in our own side cache
    and never touch the Keychain (see _write_cached_token).
    """
    if httpx is None:
        raise SlackBridgeError("httpx is required for the direct MCP transport.")
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": OAUTH_CLIENT_ID,
    }
    try:
        r = httpx.post(
            OAUTH_TOKEN_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=MCP_HTTP_TIMEOUT,
        )
    except httpx.HTTPError as e:
        raise SlackBridgeError(f"OAuth refresh request failed: {e}") from e
    if r.status_code != 200:
        # Do not echo the body verbatim (could carry token material); summarize.
        raise SlackBridgeError(
            f"OAuth refresh returned HTTP {r.status_code}. The refresh token may "
            "be stale or rotated. Re-run an interactive `claude` to re-auth."
        )
    try:
        body = r.json()
    except ValueError as e:
        raise SlackBridgeError("OAuth refresh response was not JSON.") from e
    access = body.get("access_token")
    if not access:
        raise SlackBridgeError("OAuth refresh response missing access_token.")
    new_refresh = body.get("refresh_token", refresh_token)
    expires_in = int(body.get("expires_in", 3600))
    expires_at_ms = int((time.time() + expires_in) * 1000)
    return access, new_refresh, expires_at_ms


def _get_access_token(force_refresh: bool = False) -> str:
    """Return a usable claude.ai access token, refreshing headlessly if needed.

    Resolution order:
      1. If our side cache holds a non-expired access token, use it.
      2. Else read the Keychain credential. If its access token is still valid
         and we are not force-refreshing, use it directly (the common case --
         the collector usually runs while Claude Code's token is fresh).
      3. Else (no valid token anywhere) raise cleanly. We deliberately do NOT
         perform an independent OAuth refresh: the claude.ai grant rotates the
         refresh token, and refreshing from our side would leave Claude Code's
         Keychain holding a superseded token that reuse-detection could reject,
         logging Claude Code out. Protecting its login wins over a stale-token
         Slack run. The Keychain is never written.
    """
    cached = _read_cached_token()
    if (
        not force_refresh
        and cached
        and not _is_expired(cached.get("expiresAt"))
    ):
        return cached["accessToken"]

    cred = _read_keychain_credential()
    oauth = cred.get("claudeAiOauth") or {}
    kc_access = oauth.get("accessToken")
    kc_expires = oauth.get("expiresAt")

    if not force_refresh and kc_access and not _is_expired(kc_expires):
        return kc_access

    # Token expired and nothing valid cached. Do NOT independently refresh: the
    # claude.ai refresh grant ROTATES the refresh token, so refreshing here would
    # strand Claude Code's Keychain on a superseded token. With refresh-token
    # reuse detection (standard OAuth hardening), Claude Code's next refresh from
    # that stale token could be rejected and log it out. We will not gamble the
    # user's Claude Code login to index Slack on one stale-token run, so we fail
    # cleanly. The Keychain self-heals the moment `claude` is used interactively.
    # (_refresh_access_token / _write_cached_token are retained for a future
    # write-back-to-Keychain design that keeps a single, shared refresh chain.)
    raise SlackBridgeError(
        "claude.ai access token is expired and no valid cached token is "
        "available; skipping Slack this run rather than forking Claude Code's "
        "OAuth refresh chain (which could log it out). It succeeds on the next "
        "run after any interactive `claude` use refreshes the Keychain."
    )


# ---------------------------------------------------------------------------
# Direct MCP client (Streamable HTTP / JSON-RPC)
# ---------------------------------------------------------------------------

class _SlackMcpClient:
    """Minimal direct MCP client for the claude_ai_Slack connector.

    Opens one Streamable HTTP session (initialize + notifications/initialized)
    and exposes tools/call. No LLM. On a 401 it transparently refreshes the
    access token once and reopens the session.
    """

    def __init__(self, access_token: str):
        if httpx is None:
            raise SlackBridgeError("httpx is required for the direct MCP transport.")
        self._url = f"{MCP_PROXY_BASE}/{SLACK_MCPSRV_ID}"
        self._client_sid = str(uuid.uuid4())
        self._client = httpx.Client(timeout=MCP_HTTP_TIMEOUT)
        self._access_token = access_token
        self._mcp_session_id: Optional[str] = None
        self._rpc_id = 0
        self._initialize()

    def _headers(self) -> Dict[str, str]:
        h = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "X-Mcp-Client-Session-Id": self._client_sid,
        }
        if self._mcp_session_id:
            h["Mcp-Session-Id"] = self._mcp_session_id
        return h

    @staticmethod
    def _parse_sse(text: str) -> Dict:
        """Extract the JSON-RPC object from a Streamable HTTP / SSE response."""
        for line in text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[len("data:"):].strip())
        return json.loads(text)

    def _next_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    def _initialize(self) -> None:
        r = self._client.post(
            self._url,
            headers=self._headers(),
            json={
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "rag-slack-direct", "version": "1.0"},
                },
            },
        )
        if r.status_code == 401:
            raise _Unauthorized()
        r.raise_for_status()
        self._mcp_session_id = r.headers.get("mcp-session-id")
        self._parse_sse(r.text)  # validate it parses; serverInfo unused
        # notifications/initialized (notification, no id)
        self._client.post(
            self._url,
            headers=self._headers(),
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )

    def call_tool(self, name: str, arguments: Dict) -> Dict:
        r = self._client.post(
            self._url,
            headers=self._headers(),
            json={
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        if r.status_code == 401:
            raise _Unauthorized()
        r.raise_for_status()
        parsed = self._parse_sse(r.text)
        if "error" in parsed:
            raise SlackBridgeError(
                f"MCP tool '{name}' error: {parsed['error']}"
            )
        return parsed.get("result", {})

    def tool_text(self, name: str, arguments: Dict) -> str:
        """Call a tool and return the concatenated text content blocks."""
        result = self.call_tool(name, arguments)
        blocks = result.get("content", [])
        return "\n".join(
            b.get("text", "") for b in blocks if b.get("type") == "text"
        )

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass


class _Unauthorized(Exception):
    """Internal sentinel: the proxy returned 401 (token expired/invalid)."""


# ---------------------------------------------------------------------------
# Parsers for the connector's human-formatted text blocks
# ---------------------------------------------------------------------------

# slack_read_channel stanza header, e.g.:
#   === Message from Breven Joyner <bjoyner@bamboohr.com> (U07SAMVGS22) at 2026-06-23 12:58:39 EDT ===
_CHANNEL_HEADER_RE = re.compile(
    r"^=== Message from (?P<name>.*?) <(?P<email>[^>]*)> \((?P<uid>[^)]*)\) at .*? ===\s*$"
)
# slack_read_thread reply stanza, e.g.:  --- Reply 1 of 4 ---
_THREAD_REPLY_SEP_RE = re.compile(r"^--- Reply \d+ of \d+ ---\s*$")
_THREAD_PARENT_SEP_RE = re.compile(r"^=== THREAD PARENT MESSAGE ===\s*$")
_THREAD_REPLIES_HEADER_RE = re.compile(r"^=== THREAD REPLIES.*===\s*$")
# Common metadata lines inside any stanza.
_MSG_TS_RE = re.compile(r"^Message TS:\s*(?P<ts>\S+)\s*$")
_FROM_RE = re.compile(r"^From:\s*(?P<name>.*?) <(?P<email>[^>]*)> \((?P<uid>[^)]*)\)\s*$")
_THREAD_LINE_RE = re.compile(r"^Thread:\s*(?P<count>\d+)\s+repl", re.IGNORECASE)
# Lines that are stanza metadata, not body text.
_META_PREFIXES = ("Message TS:", "Reactions:", "Files:", "Time:", "From:")
# Pagination cursor inside pagination_info.
_CURSOR_RE = re.compile(r"cursor:\s*`(?P<cursor>[^`]+)`")


def _split_tool_payload(tool_text: str) -> Tuple[str, str]:
    """Return (messages_block, pagination_info) from a tool's text result.

    The connector wraps both in a JSON string ({"messages": "...",
    "pagination_info": "..."}). Falls back to treating the whole thing as the
    messages block if it is not JSON-wrapped.
    """
    try:
        obj = json.loads(tool_text)
        if isinstance(obj, dict):
            return obj.get("messages", "") or "", obj.get("pagination_info", "") or ""
    except (json.JSONDecodeError, TypeError):
        pass
    return tool_text, ""


def _extract_cursor(pagination_info: str) -> Optional[str]:
    """Pull the next-page cursor out of a pagination_info string, if present."""
    if not pagination_info:
        return None
    m = _CURSOR_RE.search(pagination_info)
    return m.group("cursor") if m else None


def _clean_body(body_lines: List[str]) -> str:
    """Join a stanza's body lines, trimming trailing blank lines."""
    while body_lines and not body_lines[-1].strip():
        body_lines.pop()
    while body_lines and not body_lines[0].strip():
        body_lines.pop(0)
    return "\n".join(body_lines)


def parse_channel_block(messages_block: str) -> List[Dict]:
    """Parse a slack_read_channel ``messages`` text block into raw records.

    Each record carries: ts, user_id, user_display, text, and a derived
    ``reply_count`` (from a ``Thread: N replies`` line, else 0). thread/parent
    linkage is finalized later in fetch (after thread expansion).
    """
    records: List[Dict] = []
    lines = messages_block.splitlines()
    cur: Optional[Dict] = None
    body: List[str] = []

    def _flush():
        nonlocal cur, body
        if cur is not None:
            cur["text"] = _clean_body(body)
            records.append(cur)
        cur, body = None, []

    for line in lines:
        m = _CHANNEL_HEADER_RE.match(line)
        if m:
            _flush()
            cur = {
                "ts": "",
                "user_id": m.group("uid").strip(),
                "user_display": m.group("name").strip() or "Unknown",
                "text": "",
                "reply_count": 0,
            }
            body = []
            continue
        if cur is None:
            # Lines before the first stanza header (e.g. "Channel: #..."): skip.
            continue
        ts_m = _MSG_TS_RE.match(line)
        if ts_m:
            cur["ts"] = ts_m.group("ts").strip()
            continue
        thr_m = _THREAD_LINE_RE.match(line)
        if thr_m:
            cur["reply_count"] = int(thr_m.group("count"))
            continue
        if line.startswith(_META_PREFIXES):
            # Reactions/Files/etc: not part of the indexed body text.
            continue
        body.append(line)

    _flush()
    return records


def parse_thread_block(messages_block: str, parent_ts: str) -> List[Dict]:
    """Parse a slack_read_thread block into (parent + replies) raw records.

    Returns records in document order: the parent first, then each reply. Every
    record carries ts, user_id, user_display, text. Parent/reply roles are
    assigned by position (first stanza = parent).
    """
    records: List[Dict] = []
    lines = messages_block.splitlines()
    cur: Optional[Dict] = None
    body: List[str] = []
    seen_first = False

    def _flush():
        nonlocal cur, body
        if cur is not None:
            cur["text"] = _clean_body(body)
            records.append(cur)
        cur, body = None, []

    def _start():
        nonlocal cur, body
        _flush()
        cur = {"ts": "", "user_id": "", "user_display": "Unknown", "text": ""}
        body = []

    for line in lines:
        if _THREAD_PARENT_SEP_RE.match(line):
            _start()
            seen_first = True
            continue
        if _THREAD_REPLIES_HEADER_RE.match(line):
            # Section divider between parent and replies; not its own stanza.
            continue
        if _THREAD_REPLY_SEP_RE.match(line):
            _start()
            seen_first = True
            continue
        if not seen_first:
            continue
        from_m = _FROM_RE.match(line)
        if from_m:
            cur["user_display"] = from_m.group("name").strip() or "Unknown"
            cur["user_id"] = from_m.group("uid").strip()
            continue
        ts_m = _MSG_TS_RE.match(line)
        if ts_m:
            cur["ts"] = ts_m.group("ts").strip()
            continue
        if line.startswith(_META_PREFIXES):
            continue
        body.append(line)

    _flush()
    return records


# ---------------------------------------------------------------------------
# Direct transport: fetch one channel (with thread expansion + pagination)
# ---------------------------------------------------------------------------

def _fetch_channel_direct(
    channel_id: str,
    oldest_ts: float,
    max_messages: int,
) -> List[Dict]:
    """Fetch one channel via the direct (no-LLM) MCP client.

    Mirrors the old claude -p prompt's semantics:
      - window by ``oldest`` (server-side), paging via cursor until exhausted or
        the top-level cap is reached;
      - cap top-level messages at ``max_messages`` (newest kept);
      - expand any message with replies via slack_read_thread, including ALL
        replies regardless of age;
      - flatten parents/replies/standalone into one list with parent/reply flags.
    """
    access = _get_access_token()
    try:
        client = _SlackMcpClient(access)
    except _Unauthorized:
        # Token rejected at init: force a refresh and retry once.
        access = _get_access_token(force_refresh=True)
        client = _SlackMcpClient(access)

    def _call_with_reauth(name: str, args: Dict) -> str:
        nonlocal client
        try:
            return client.tool_text(name, args)
        except _Unauthorized:
            client.close()
            new_access = _get_access_token(force_refresh=True)
            client = _SlackMcpClient(new_access)
            return client.tool_text(name, args)

    try:
        # ---- Step 1: page the channel within the window, up to the cap. ----
        top_level: List[Dict] = []
        cursor: Optional[str] = None
        oldest_str = f"{oldest_ts:.6f}"
        while True:
            args: Dict = {
                "channel_id": channel_id,
                "oldest": oldest_str,
                "limit": CHANNEL_PAGE_LIMIT,
            }
            if cursor:
                args["cursor"] = cursor
            text = _call_with_reauth("slack_read_channel", args)
            messages_block, pagination_info = _split_tool_payload(text)
            page_records = parse_channel_block(messages_block)
            top_level.extend(page_records)
            cursor = _extract_cursor(pagination_info)
            if not cursor or len(top_level) >= max_messages:
                break

        # Enforce the cap: keep the newest ``max_messages`` top-level messages.
        # The connector returns newest-first, so the first N are the newest.
        if len(top_level) > max_messages:
            top_level = top_level[:max_messages]

        # ---- Step 2 & 3: expand threads, flatten with parent/reply flags. ----
        out: List[Dict] = []
        for rec in top_level:
            ts = rec.get("ts", "")
            if not ts:
                continue
            reply_count = int(rec.get("reply_count") or 0)
            if reply_count <= 0:
                # Standalone message.
                out.append(
                    {
                        "ts": ts,
                        "user_id": rec.get("user_id", ""),
                        "user_display": rec.get("user_display", "Unknown"),
                        "text": rec.get("text", ""),
                        "thread_ts": None,
                        "is_parent": False,
                        "is_reply": False,
                        "reply_count": 0,
                    }
                )
                continue

            # Thread: read parent + all replies.
            thread_text = _call_with_reauth(
                "slack_read_thread",
                {
                    "channel_id": channel_id,
                    "message_ts": ts,
                    "limit": THREAD_PAGE_LIMIT,
                },
            )
            thread_block, _ = _split_tool_payload(thread_text)
            thread_records = parse_thread_block(thread_block, ts)

            if not thread_records:
                # Thread read returned nothing usable: degrade to the channel
                # record as a standalone parent so the message is not lost.
                out.append(
                    {
                        "ts": ts,
                        "user_id": rec.get("user_id", ""),
                        "user_display": rec.get("user_display", "Unknown"),
                        "text": rec.get("text", ""),
                        "thread_ts": ts,
                        "is_parent": True,
                        "is_reply": False,
                        "reply_count": reply_count,
                    }
                )
                continue

            # First record = parent; the rest = replies.
            parent = thread_records[0]
            replies = thread_records[1:]
            parent_ts = parent.get("ts") or ts
            out.append(
                {
                    "ts": parent_ts,
                    "user_id": parent.get("user_id", ""),
                    "user_display": parent.get("user_display", "Unknown"),
                    "text": parent.get("text", ""),
                    "thread_ts": parent_ts,
                    "is_parent": True,
                    "is_reply": False,
                    "reply_count": len(replies),
                }
            )
            for rep in replies:
                rep_ts = rep.get("ts")
                if not rep_ts:
                    continue
                out.append(
                    {
                        "ts": rep_ts,
                        "user_id": rep.get("user_id", ""),
                        "user_display": rep.get("user_display", "Unknown"),
                        "text": rep.get("text", ""),
                        "thread_ts": parent_ts,
                        "is_parent": False,
                        "is_reply": True,
                        "reply_count": 0,
                    }
                )
        return out
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Legacy claude -p transport (fallback)
# ---------------------------------------------------------------------------

def _build_prompt(channel_id: str, oldest_ts: float, max_messages: int, out_path: str) -> str:
    """Construct the strict fetch-and-dump prompt for one channel (claude -p)."""
    return f"""You are a mechanical Slack data extractor. Do exactly what is described, nothing more. Do not summarize, editorialize, or skip anything.

GOAL: Read recent messages from one Slack channel (including thread replies) and write them as a strict JSON array to a file. Then stop.

CHANNEL: {channel_id}
WINDOW: only include messages with a Slack ts (unix epoch seconds) >= {oldest_ts:.6f}. Ignore anything older.
CAP: include at most {max_messages} top-level messages (a thread parent or a standalone message counts as one; its replies do not count toward this cap). If there are more than the cap within the window, keep the {max_messages} most recent and drop the rest.

STEP 1 - Read the channel.
Call mcp__claude_ai_Slack__slack_read_channel with channel_id="{channel_id}", oldest="{oldest_ts:.6f}", limit=100. If the result indicates more pages (a next_cursor), call it again with the same oldest and the cursor, repeating until there are no more pages OR you have collected enough top-level messages to cover the cap within the window. Collect every message returned within the window.

STEP 2 - Expand threads.
For every message that indicates it has replies (e.g. a "thread" field like "N replies ..." or a reply_count > 0), call mcp__claude_ai_Slack__slack_read_thread with that channel_id and the parent's message_ts to retrieve the parent and all of its replies. Read every reply, regardless of age (a thread can have recent replies on an older parent - include them all).

STEP 3 - Emit JSON.
Build a single JSON array. Flatten parents, replies, and standalone messages into one list. For EACH message produce exactly this object:
{{
  "ts": "<the Slack ts string for THIS message, e.g. 1782252215.158459>",
  "user_id": "<the Slack user id, e.g. U08JKE31KPV, or empty string if unknown>",
  "user_display": "<the author's display name exactly as the connector reported it inline, e.g. Jacob Peterson>",
  "text": "<the message text VERBATIM, including any <@U...> mentions, <#C...|name> channel refs, and <url|label> links left exactly as-is. Do NOT resolve or rewrite them.>",
  "thread_ts": "<the parent thread ts string if this message belongs to a thread, otherwise null>",
  "is_parent": <true if this is a thread root that has at least one reply, else false>,
  "is_reply": <true if this is a reply beneath a parent, else false>,
  "reply_count": <integer number of replies if this is a thread parent, else 0>
}}

Rules:
- A standalone message (no replies): is_parent=false, is_reply=false, thread_ts=null, reply_count=0.
- A thread parent: is_parent=true, is_reply=false, thread_ts equals its own ts, reply_count=the number of replies.
- A reply: is_parent=false, is_reply=true, thread_ts equals the PARENT's ts, reply_count=0.
- Use the connector's inline display name for user_display; only call mcp__claude_ai_Slack__slack_read_user_profile if a message has a user_id but the connector gave you no name at all.
- ts must be the raw Slack ts string (the dotted unix value), NOT a human date. If the connector only gave you a human timestamp for a message, still use the raw ts string it provided alongside it.

STEP 4 - Write the file.
Use the Write tool to write the JSON array (and nothing else - no markdown fences, no prose) to this exact path:
{out_path}

STEP 5 - Finish.
After the file is written, reply with exactly DONE and nothing else. If the channel had zero messages in the window, write an empty array [] to the file and still reply DONE.
"""


def _fetch_channel_claude_p(
    channel_id: str,
    oldest_ts: float,
    max_messages: int,
    model: Optional[str],
    timeout: Optional[int],
) -> List[Dict]:
    """Legacy fallback: fetch one channel via a headless ``claude -p`` worker."""
    model = model or BRIDGE_MODEL
    timeout = timeout or BRIDGE_TIMEOUT_SECONDS

    fd, out_path = tempfile.mkstemp(prefix="slack_bridge_", suffix=".json")
    os.close(fd)
    try:
        os.unlink(out_path)
    except FileNotFoundError:
        pass

    prompt = _build_prompt(channel_id, oldest_ts, max_messages, out_path)
    cmd = [
        "claude", "-p", prompt,
        "--allowedTools", *ALLOWED_TOOLS,
        "--model", model,
        "--output-format", "json",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise SlackBridgeError(
            f"claude -p timed out after {timeout}s for channel {channel_id}"
        ) from e

    if proc.returncode != 0:
        raise SlackBridgeError(
            f"claude -p exited {proc.returncode} for channel {channel_id}. "
            f"stderr: {proc.stderr[-2000:]}"
        )

    out_file = Path(out_path)
    if not out_file.exists():
        raise SlackBridgeError(
            f"claude -p did not write the result file for channel {channel_id}. "
            f"stdout tail: {proc.stdout[-1000:]}"
        )
    try:
        with open(out_file, "r") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise SlackBridgeError(
            f"Could not parse bridge result file for channel {channel_id}: {e}"
        ) from e
    finally:
        try:
            out_file.unlink()
        except FileNotFoundError:
            pass
    return _validate_messages(raw)


# ---------------------------------------------------------------------------
# Validation (shared) + public entry point
# ---------------------------------------------------------------------------

def _validate_messages(raw: object) -> List[Dict]:
    """Validate and lightly normalize a list of bridge message records."""
    if not isinstance(raw, list):
        raise SlackBridgeError(
            f"Bridge output is not a JSON array (got {type(raw).__name__})"
        )

    required = {"ts", "user_id", "user_display", "text", "thread_ts",
                "is_parent", "is_reply", "reply_count"}
    cleaned: List[Dict] = []
    for i, m in enumerate(raw):
        if not isinstance(m, dict):
            raise SlackBridgeError(f"Bridge message {i} is not an object: {m!r}")
        missing = required - set(m.keys())
        if missing:
            raise SlackBridgeError(
                f"Bridge message {i} missing keys {sorted(missing)}: {m!r}"
            )
        ts = str(m.get("ts") or "").strip()
        if not ts:
            continue
        try:
            reply_count = int(m.get("reply_count") or 0)
        except (TypeError, ValueError):
            reply_count = 0
        cleaned.append(
            {
                "ts": ts,
                "user_id": str(m.get("user_id") or "").strip(),
                "user_display": str(m.get("user_display") or "Unknown").strip()
                or "Unknown",
                "text": m.get("text") or "",
                "thread_ts": (str(m["thread_ts"]).strip()
                              if m.get("thread_ts") else None),
                "is_parent": bool(m.get("is_parent")),
                "is_reply": bool(m.get("is_reply")),
                "reply_count": reply_count,
            }
        )
    return cleaned


def fetch_channel_via_mcp(
    channel_id: str,
    oldest_ts: float,
    max_messages: int = 2000,
    model: Optional[str] = None,
    timeout: Optional[int] = None,
) -> List[Dict]:
    """
    Fetch one channel's recent messages (with threads) through the official
    ``claude_ai_Slack`` connector.

    Transport is chosen by env ``SLACK_BRIDGE_TRANSPORT``:
      - "direct" (default): standalone Python MCP client, no LLM, ~seconds, $0.
      - "claude_p": legacy headless ``claude -p`` worker (~minutes, paid LLM).

    Args:
        channel_id: Slack channel ID (e.g. "C08GM20DP29").
        oldest_ts: Unix epoch seconds; only messages with ts >= this are kept.
        max_messages: Cap on top-level messages (parents + standalone).
        model: claude -p model override (claude_p transport only).
        timeout: claude -p per-channel wall-clock ceiling (claude_p only).

    Returns:
        List of validated message dicts (see module docstring schema). The shape
        is identical across both transports, so the collector seam,
        reshape_bridge_messages, the SLACK_BACKEND toggle, and the tests are all
        contract-stable.

    Raises:
        SlackBridgeError: on any transport / auth / parse failure.
    """
    if BRIDGE_TRANSPORT == "claude_p":
        return _fetch_channel_claude_p(
            channel_id, oldest_ts, max_messages, model, timeout
        )
    # Default: direct, no-LLM transport.
    raw = _fetch_channel_direct(channel_id, oldest_ts, max_messages)
    return _validate_messages(raw)


def extract_cost_usd(claude_p_stdout: str) -> Optional[float]:
    """Pull total_cost_usd out of a `claude -p --output-format json` blob, if present."""
    try:
        data = json.loads(claude_p_stdout)
        return data.get("total_cost_usd")
    except (json.JSONDecodeError, AttributeError):
        return None


if __name__ == "__main__":
    # Manual smoke test: python3 slack_mcp_bridge.py C08GM20DP29 7
    import sys
    import time as _time

    cid = sys.argv[1] if len(sys.argv) > 1 else "C08GM20DP29"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    oldest = _time.time() - days * 86400
    t0 = _time.time()
    msgs = fetch_channel_via_mcp(cid, oldest, max_messages=2000)
    dt = _time.time() - t0
    print(f"fetched {len(msgs)} messages from {cid} (last {days}d) in {dt:.1f}s "
          f"via transport={BRIDGE_TRANSPORT}")
    print(json.dumps(msgs[:3], indent=2))
