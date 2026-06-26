"""
Slack Message Collector for RAG - Fetch messages via Slack API.

Fetches Slack messages from configured channels and returns them as searchable chunks.
Thread-based chunking: threads with replies are combined into one chunk.
"""

import json
import os
import re
import time
import requests
from typing import List, Dict, Optional, Union
from pathlib import Path
from datetime import datetime

# MCP bridge backend (default). The stealth xoxc/xoxd path below is dead under
# Slack Enterprise (forced logout on every call) and is retained ONLY as an
# emergency fallback behind SLACK_BACKEND=stealth. See slack_mcp_bridge.py.
try:
    from slack_mcp_bridge import fetch_channel_via_mcp
except ImportError:  # pragma: no cover - import shim when run as a module
    from scripts.slack_mcp_bridge import fetch_channel_via_mcp


def load_slack_tokens(token_file: str) -> Dict[str, str]:
    """
    Load Slack tokens from config file.

    Args:
        token_file: Path to tokens.json file

    Returns:
        Dict with 'xoxc' and 'xoxd' tokens
    """
    token_path = Path(token_file).expanduser()

    if not token_path.exists():
        raise FileNotFoundError(f"Slack token file not found: {token_path}")

    with open(token_path, "r") as f:
        tokens = json.load(f)

    if "xoxc" not in tokens or "xoxd" not in tokens:
        raise ValueError("Token file missing required 'xoxc' or 'xoxd' tokens")

    return tokens


def load_slack_channels(channels_file: str) -> Dict[str, Dict]:
    """
    Load Slack channel mappings from config file.

    Args:
        channels_file: Path to channels.json file

    Returns:
        Dict mapping channel names to channel info (id, name, etc.)
    """
    channels_path = Path(channels_file).expanduser()

    if not channels_path.exists():
        raise FileNotFoundError(f"Slack channels file not found: {channels_path}")

    with open(channels_path, "r") as f:
        channels = json.load(f)

    return channels


def load_user_cache(cache_file: str) -> Dict[str, Dict]:
    """
    Load user cache from disk.

    Handles both formats:
    - Bash skill format: {"USERID": "@username (Display Name)"}
    - Python format: {"USERID": {"username": "...", "display_name": "..."}}

    Args:
        cache_file: Path to user cache JSON file

    Returns:
        Dict mapping user IDs to user info dicts
    """
    cache_path = Path(cache_file).expanduser()

    if cache_path.exists():
        try:
            with open(cache_path, "r") as f:
                raw_cache = json.load(f)

            # Convert bash format to Python format if needed
            converted_cache = {}
            for user_id, value in raw_cache.items():
                if isinstance(value, str):
                    # Bash format: "@username (Display Name)"
                    # Parse it into dict format
                    match = re.match(r"@(\S+) \((.+)\)", value)
                    if match:
                        converted_cache[user_id] = {
                            "username": match.group(1),
                            "display_name": match.group(2),
                        }
                    else:
                        # Fallback if format doesn't match
                        converted_cache[user_id] = {
                            "username": "unknown",
                            "display_name": value,
                        }
                elif isinstance(value, dict):
                    # Already in Python format
                    converted_cache[user_id] = value

            return converted_cache
        except Exception as e:
            print(f"  ⚠️  Error loading user cache: {e}")
            return {}

    return {}


def save_user_cache(cache_file: str, user_cache: Dict[str, Dict]) -> None:
    """
    Save user cache to disk.

    Args:
        cache_file: Path to user cache JSON file
        user_cache: Dict mapping user IDs to user info dicts
    """
    cache_path = Path(cache_file).expanduser()
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with open(cache_path, "w") as f:
            json.dump(user_cache, f, indent=2)
    except Exception as e:
        print(f"  ⚠️  Error saving user cache: {e}")


def fetch_user_info(
    user_id: str, tokens: Dict[str, str], user_cache: Dict[str, Dict]
) -> Dict[str, str]:
    """
    Fetch user information from Slack API (with caching).

    Args:
        user_id: Slack user ID
        tokens: Dict with xoxc and xoxd tokens
        user_cache: Cache dict to store user info

    Returns:
        Dict with 'username' and 'display_name'
    """
    # Check cache first
    if user_id in user_cache:
        return user_cache[user_id]

    # Fetch from API
    headers = {
        "cookie": f'd={tokens["xoxd"]}',
        "content-type": "application/x-www-form-urlencoded",
    }

    data = {"token": tokens["xoxc"], "user": user_id}

    try:
        response = requests.post(
            "https://slack.com/api/users.info", headers=headers, data=data
        )
        response.raise_for_status()
        result = response.json()

        if result.get("ok"):
            user = result.get("user", {})
            user_info = {
                "username": user.get("name", "unknown"),
                "display_name": user.get("profile", {}).get("display_name")
                or user.get("real_name", "Unknown"),
            }
            user_cache[user_id] = user_info
            return user_info
        else:
            print(
                f"  ⚠️  Failed to fetch user {user_id}: {result.get('error', 'unknown error')}"
            )
            return {"username": "unknown", "display_name": "Unknown"}

    except Exception as e:
        print(f"  ⚠️  Error fetching user {user_id}: {e}")
        return {"username": "unknown", "display_name": "Unknown"}


def resolve_mentions_in_text(
    text: str, tokens: Dict[str, str], user_cache: Dict[str, Dict]
) -> str:
    """
    Resolve user mentions in message text from <@USERID> to @username (Display Name).

    Args:
        text: Message text with potential <@USERID> mentions
        tokens: Dict with xoxc and xoxd tokens
        user_cache: Cache dict to store user info

    Returns:
        Text with resolved mentions (e.g., "@darrell (Darrell Hunt)")
    """
    if not text:
        return text

    # Find all <@USERID> patterns in the text
    mention_pattern = r"<@([A-Z0-9]+)>"
    mentions = re.findall(mention_pattern, text)

    # Resolve each unique user ID
    for user_id in set(mentions):  # Use set to avoid duplicate lookups
        user_info = fetch_user_info(user_id, tokens, user_cache)
        # Replace <@USERID> with @username (Display Name)
        mention_text = f"@{user_info['username']} ({user_info['display_name']})"
        text = text.replace(f"<@{user_id}>", mention_text)

    return text


def fetch_thread_replies(
    channel_id: str, thread_ts: str, tokens: Dict[str, str]
) -> List[Dict]:
    """
    Fetch all replies in a thread.

    Args:
        channel_id: Slack channel ID
        thread_ts: Thread timestamp
        tokens: Dict with xoxc and xoxd tokens

    Returns:
        List of reply messages (excluding the parent)
    """
    headers = {
        "cookie": f'd={tokens["xoxd"]}',
        "content-type": "application/x-www-form-urlencoded",
    }

    data = {"token": tokens["xoxc"], "channel": channel_id, "ts": thread_ts}

    # Retry with backoff on 429. conversations.replies is a Tier 3 endpoint
    # (~50 req/min), which a busy reindex burns through fast — especially on
    # channels like #development with lots of long reply tails. Slack returns
    # Retry-After when rate-limited; we honor it. On transient errors we
    # back off exponentially before giving up.
    MAX_ATTEMPTS = 5
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = requests.post(
                "https://slack.com/api/conversations.replies",
                headers=headers,
                data=data,
            )

            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 30))
                print(
                    f"  ⚠️  replies rate-limited, waiting {retry_after}s "
                    f"(attempt {attempt + 1}/{MAX_ATTEMPTS})"
                )
                time.sleep(retry_after)
                continue

            response.raise_for_status()
            result = response.json()

            if result.get("ok"):
                messages = result.get("messages", [])
                # Drop the parent; only replies flow downstream.
                return messages[1:] if len(messages) > 1 else []
            print(
                f"  ⚠️  Failed to fetch thread replies: "
                f"{result.get('error', 'unknown error')}"
            )
            return []

        except Exception as e:
            if attempt < MAX_ATTEMPTS - 1:
                backoff = 2**attempt
                print(
                    f"  ⚠️  replies error: {e}, retrying in {backoff}s "
                    f"(attempt {attempt + 1}/{MAX_ATTEMPTS})"
                )
                time.sleep(backoff)
                continue
            print(f"  ⚠️  Error fetching thread replies (gave up): {e}")
            return []

    print(f"  ⚠️  replies: exhausted {MAX_ATTEMPTS} attempts, giving up")
    return []


def format_timestamp(ts: str) -> str:
    """
    Format Slack timestamp to readable datetime.

    Args:
        ts: Slack timestamp (e.g., "1774289284.648239")

    Returns:
        Formatted datetime string (e.g., "2026-04-03 12:47:37")
    """
    try:
        timestamp = float(ts)
        dt = datetime.fromtimestamp(timestamp)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except:
        return ts


def resolve_mentions_static(text: str, id_to_name: Dict[str, str]) -> str:
    """
    Resolve <@USERID> mentions using an in-memory id->display-name map (no API).

    Used by the MCP backend, which already carries display names inline for
    message authors. We build the map from those inline names and use it to
    rewrite in-text mentions to "@Display Name". Unknown IDs are left as-is so
    nothing is silently dropped.

    Args:
        text: Message text with potential <@USERID> mentions.
        id_to_name: Map of Slack user id -> display name harvested from the
            bridge's inline author names.

    Returns:
        Text with resolvable mentions rewritten to "@Display Name".
    """
    if not text:
        return text

    def _sub(match: "re.Match") -> str:
        uid = match.group(1)
        name = id_to_name.get(uid)
        return f"@{name}" if name else match.group(0)

    return re.sub(r"<@([A-Z0-9]+)>", _sub, text)


def reshape_bridge_messages(
    messages: List[Dict],
    channel_name: str,
    channel_id: str,
) -> List[Dict]:
    """
    Convert bridge JSON message records into the exact chunk dicts that
    collect_slack_messages has always returned. This is the contract firewall:
    the metadata keys, chunk_type values, and markdown headers here MUST match
    the stealth path byte-for-byte so the rest of the RAG pipeline is untouched.

    Bridge record schema (see slack_mcp_bridge.py):
        ts, user_id, user_display, text, thread_ts, is_parent, is_reply,
        reply_count

    Produces the three chunk_types: "thread_parent", "thread_reply", "message".

    Args:
        messages: Validated bridge records for one channel.
        channel_name: Channel name without leading '#'.
        channel_id: Slack channel id.

    Returns:
        List of {"content": <markdown>, "metadata": {...}} chunks.
    """
    chunks: List[Dict] = []

    # Build an id->display-name map from inline author names so we can resolve
    # in-text <@USERID> mentions without any API fan-out. Also index parents so
    # replies can name their thread starter.
    id_to_name: Dict[str, str] = {}
    parent_author_by_ts: Dict[str, str] = {}
    for m in messages:
        if m.get("user_id") and m.get("user_display"):
            id_to_name.setdefault(m["user_id"], m["user_display"])
        if m.get("is_parent"):
            parent_author_by_ts[m["ts"]] = m.get("user_display", "Unknown")

    for m in messages:
        msg_ts = m["ts"]
        author_display = m.get("user_display", "Unknown")
        # The stealth path stored a short username plus a display name. The MCP
        # connector only exposes the display name inline, so we use it for both
        # author_username and author_display to keep every key populated.
        author_username = author_display
        formatted_ts = format_timestamp(msg_ts)
        resolved_text = resolve_mentions_static(m.get("text", ""), id_to_name)

        if m.get("is_reply"):
            thread_ts = m.get("thread_ts") or msg_ts
            parent_author = parent_author_by_ts.get(thread_ts, "Unknown")
            content = f"""# Reply from @{author_username} in #{channel_name} thread
**Date:** {formatted_ts}
**Thread started by:** @{parent_author}

{resolved_text}
"""
            metadata = {
                "channel_name": channel_name,
                "channel_id": channel_id,
                "message_ts": msg_ts,
                "thread_ts": thread_ts,
                "thread_parent_author": parent_author,
                "author_username": author_username,
                "author_display": author_display,
                "date": formatted_ts.split(" ")[0],
                "timestamp": formatted_ts,
                "is_thread_reply": True,
                "chunk_type": "thread_reply",
                "filename": f"{channel_name}_{msg_ts}.slack",
                "filepath": f"slack://{channel_name}/{msg_ts}",
            }
            chunks.append({"content": content, "metadata": metadata})

        elif m.get("is_parent"):
            reply_count = m.get("reply_count", 0)
            content = f"""# Thread started by @{author_username} in #{channel_name}
**Date:** {formatted_ts}
**Replies:** {reply_count}

{resolved_text}
"""
            metadata = {
                "channel_name": channel_name,
                "channel_id": channel_id,
                "message_ts": msg_ts,
                "thread_ts": msg_ts,  # Parent's thread_ts is same as message_ts
                "author_username": author_username,
                "author_display": author_display,
                "date": formatted_ts.split(" ")[0],
                "timestamp": formatted_ts,
                "is_thread_parent": True,
                "reply_count": reply_count,
                "chunk_type": "thread_parent",
                "filename": f"{channel_name}_{msg_ts}.slack",
                "filepath": f"slack://{channel_name}/{msg_ts}",
            }
            chunks.append({"content": content, "metadata": metadata})

        else:
            content = f"""# Message from @{author_username} in #{channel_name}
**Date:** {formatted_ts}

{resolved_text}
"""
            metadata = {
                "channel_name": channel_name,
                "channel_id": channel_id,
                "message_ts": msg_ts,
                "author_username": author_username,
                "author_display": author_display,
                "date": formatted_ts.split(" ")[0],
                "timestamp": formatted_ts,
                "is_thread": False,
                "chunk_type": "message",
                "filename": f"{channel_name}_{msg_ts}.slack",
                "filepath": f"slack://{channel_name}/{msg_ts}",
            }
            chunks.append({"content": content, "metadata": metadata})

    return chunks


def _collect_slack_messages_mcp(
    channels_config: Dict[str, Dict],
    channels_file: str,
    max_age_days: int,
    max_messages_per_channel: int,
) -> List[Dict]:
    """
    MCP-backed implementation of the collector. Pulls each channel through the
    official claude_ai_Slack connector via the claude -p bridge, then reshapes
    to the frozen chunk contract. No xoxc/xoxd, no slack.com calls.
    """
    chunks: List[Dict] = []

    try:
        channels = load_slack_channels(channels_file)
    except Exception as e:
        print(f"  ⚠️  Error loading Slack channels config: {e}")
        return []

    for channel_name, channel_overrides in channels_config.items():
        if channel_name not in channels:
            print(f"  ⚠️  Channel '{channel_name}' not found in channels.json")
            continue

        channel_id = channels[channel_name]["id"]
        channel_max_age_days = channel_overrides.get("max_age_days", max_age_days)
        oldest_timestamp = time.time() - (channel_max_age_days * 24 * 60 * 60)
        oldest_date = datetime.fromtimestamp(oldest_timestamp).strftime("%Y-%m-%d")

        print(
            f"  Fetching messages from #{channel_name} via MCP bridge "
            f"since {oldest_date} ({channel_max_age_days} days ago)..."
        )
        fetch_start = time.time()

        try:
            bridge_messages = fetch_channel_via_mcp(
                channel_id=channel_id,
                oldest_ts=oldest_timestamp,
                max_messages=max_messages_per_channel,
            )
        except Exception as e:
            print(f"  ⚠️  MCP bridge failed for #{channel_name}: {e}")
            continue

        fetch_time = time.time() - fetch_start

        if not bridge_messages:
            print(f"  ⚠️  No messages returned for #{channel_name}")
            continue

        channel_chunks = reshape_bridge_messages(
            bridge_messages, channel_name, channel_id
        )
        chunks.extend(channel_chunks)
        print(
            f"  ✓ #{channel_name}: {len(bridge_messages)} messages "
            f"→ {len(channel_chunks)} chunks in {fetch_time:.1f}s"
        )

    return chunks


def collect_slack_messages(
    channel_names: Union[List[str], Dict[str, Dict]],
    token_file: str,
    channels_file: str,
    max_age_days: int = 30,
    max_messages_per_channel: int = 2000,
) -> List[Dict]:
    """
    Fetch Slack messages from configured channels using time-based indexing.

    Backend is selected by the SLACK_BACKEND env var:
      - "mcp" (default): official claude_ai_Slack connector via the claude -p
        bridge. Sanctioned OAuth, no forced logout.
      - "stealth": the legacy xoxc/xoxd HTTP path. DEAD under Slack Enterprise
        (triggers a forced logout on every call). Retained only as an emergency
        toggle; do not use it.

    Args:
        channel_names: Either a dict mapping channel name to an overrides dict
            (e.g. {"docker-support": {"max_age_days": 180}, "development": {}}),
            or a plain list of channel names (legacy). Supported override keys:
              - max_age_days: int, overrides the default max_age_days for that channel.
            Unknown override keys are ignored.
        token_file: Path to tokens.json (only used by the stealth backend).
        channels_file: Path to channels.json
        max_age_days: Default fetch window (days) for channels that don't
            override it (default: 30)
        max_messages_per_channel: Safety cap per channel (default: 2000)

    Returns:
        List of chunks, each with:
        - content: Formatted message or thread text
        - metadata: channel, author, date, thread info, etc.
    """
    # Normalize legacy list-of-strings input to the dict shape.
    if isinstance(channel_names, list):
        channels_config: Dict[str, Dict] = {name: {} for name in channel_names}
    else:
        channels_config = channel_names

    backend = os.environ.get("SLACK_BACKEND", "mcp").strip().lower()
    if backend == "stealth":
        print("  ⚠️  SLACK_BACKEND=stealth — using the DEAD xoxc/xoxd path.")
        return _collect_slack_messages_stealth(
            channels_config,
            token_file,
            channels_file,
            max_age_days,
            max_messages_per_channel,
        )

    return _collect_slack_messages_mcp(
        channels_config,
        channels_file,
        max_age_days,
        max_messages_per_channel,
    )


def _collect_slack_messages_stealth(
    channels_config: Dict[str, Dict],
    token_file: str,
    channels_file: str,
    max_age_days: int = 30,
    max_messages_per_channel: int = 2000,
) -> List[Dict]:
    """
    DEAD legacy backend: xoxc/xoxd browser-session tokens hitting slack.com.

    Under Slack Enterprise this triggers an immediate forced logout on every
    call. Kept intact only so SLACK_BACKEND=stealth can flip back during
    cutover if the MCP path ever needs to be bypassed. DO NOT run this casually.
    """
    chunks = []

    # Load tokens and channel mappings
    try:
        tokens = load_slack_tokens(token_file)
        channels = load_slack_channels(channels_file)
    except Exception as e:
        print(f"  ⚠️  Error loading Slack configuration: {e}")
        return []

    # Load persistent user cache (shared location with bash skill)
    cache_file = "~/.claude/state/slack-tools/user-cache.json"
    user_cache = load_user_cache(cache_file)
    initial_cache_size = len(user_cache)

    # Fetch messages from each channel
    for channel_name, channel_overrides in channels_config.items():
        if channel_name not in channels:
            print(f"  ⚠️  Channel '{channel_name}' not found in channels.json")
            continue

        channel_info = channels[channel_name]
        channel_id = channel_info["id"]

        # Resolve effective max_age_days for this channel (per-channel override
        # wins over the function-level default). Unknown override keys are ignored.
        channel_max_age_days = channel_overrides.get("max_age_days", max_age_days)
        oldest_timestamp = time.time() - (channel_max_age_days * 24 * 60 * 60)
        oldest_date = datetime.fromtimestamp(oldest_timestamp).strftime("%Y-%m-%d")

        print(
            f"  Fetching messages from #{channel_name} "
            f"since {oldest_date} ({channel_max_age_days} days ago)..."
        )
        fetch_start = time.time()

        # Fetch messages from channel with pagination
        headers = {
            "cookie": f'd={tokens["xoxd"]}',
            "content-type": "application/x-www-form-urlencoded",
        }

        all_messages = []
        cursor = None
        page_count = 0

        while len(all_messages) < max_messages_per_channel:
            data = {
                "token": tokens["xoxc"],
                "channel": channel_id,
                "limit": 200,  # Fetch more per request for efficiency
                "oldest": str(oldest_timestamp),
            }

            if cursor:
                data["cursor"] = cursor

            try:
                response = requests.post(
                    "https://slack.com/api/conversations.history",
                    headers=headers,
                    data=data,
                )

                # Rate limit handling
                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 60))
                    print(f"  ⚠️  Rate limited. Waiting {retry_after} seconds...")
                    time.sleep(retry_after)
                    continue

                response.raise_for_status()
                result = response.json()

                if not result.get("ok"):
                    print(
                        f"  ⚠️  Failed to fetch messages: {result.get('error', 'unknown error')}"
                    )
                    break

                messages = result.get("messages", [])

                if not messages:
                    break

                all_messages.extend(messages)
                page_count += 1

                # Check for more pages
                has_more = result.get("has_more", False)
                cursor = result.get("response_metadata", {}).get("next_cursor")

                if not has_more or not cursor:
                    break

            except requests.exceptions.RequestException as e:
                print(f"  ⚠️  Error fetching messages from {channel_name}: {e}")
                break

        fetch_time = time.time() - fetch_start

        if not all_messages:
            print(f"  ⚠️  No messages found in #{channel_name}")
            continue

        # Apply safety cap
        if len(all_messages) > max_messages_per_channel:
            print(
                f"  ⚠️  Hit safety cap: {len(all_messages)} messages, using first {max_messages_per_channel}"
            )
            all_messages = all_messages[:max_messages_per_channel]

        # Determine actual date range of messages
        timestamps = [float(msg.get("ts", 0)) for msg in all_messages if msg.get("ts")]
        if timestamps:
            oldest_msg_date = datetime.fromtimestamp(min(timestamps)).strftime(
                "%Y-%m-%d"
            )
            newest_msg_date = datetime.fromtimestamp(max(timestamps)).strftime(
                "%Y-%m-%d"
            )
            print(
                f"  Fetched {len(all_messages)} messages ({page_count} pages) in {fetch_time:.2f}s"
            )
            print(f"  Date range: {oldest_msg_date} to {newest_msg_date}")
        else:
            print(
                f"  Fetched {len(all_messages)} messages ({page_count} pages) in {fetch_time:.2f}s"
            )

        # Track which messages are part of threads (to avoid duplicates)
        processed_thread_parents = set()
        process_start = time.time()

        # Process messages (newest first from API)
        for msg in all_messages:
            msg_ts = msg.get("ts", "")
            msg_user = msg.get("user", "")
            msg_text = msg.get("text", "")
            msg_thread_ts = msg.get("thread_ts")

            # Skip if this is a thread reply (not a parent)
            if msg_thread_ts and msg_thread_ts != msg_ts:
                continue

            # Get user info
            user_info = fetch_user_info(msg_user, tokens, user_cache)

            # Check if this is a thread parent
            reply_count = msg.get("reply_count", 0)
            is_thread = reply_count > 0

            if is_thread:
                # Fetch thread replies
                replies = fetch_thread_replies(channel_id, msg_ts, tokens)

                # Resolve mentions in parent message text
                resolved_msg_text = resolve_mentions_in_text(
                    msg_text, tokens, user_cache
                )

                # Create chunk for parent message
                formatted_ts = format_timestamp(msg_ts)
                parent_content = f"""# Thread started by @{user_info['username']} in #{channel_name}
**Date:** {formatted_ts}
**Replies:** {reply_count}

{resolved_msg_text}
"""

                parent_metadata = {
                    "channel_name": channel_name,
                    "channel_id": channel_id,
                    "message_ts": msg_ts,
                    "thread_ts": msg_ts,  # Parent's thread_ts is same as message_ts
                    "author_username": user_info["username"],
                    "author_display": user_info["display_name"],
                    "date": formatted_ts.split(" ")[0],
                    "timestamp": formatted_ts,
                    "is_thread_parent": True,
                    "reply_count": reply_count,
                    "chunk_type": "thread_parent",
                    "filename": f"{channel_name}_{msg_ts}.slack",
                    "filepath": f"slack://{channel_name}/{msg_ts}",
                }

                chunks.append({"content": parent_content, "metadata": parent_metadata})

                # Create separate chunk for each reply
                for reply in replies:
                    reply_user = reply.get("user", "")
                    reply_text = reply.get("text", "")
                    reply_ts = reply.get("ts", "")

                    reply_user_info = fetch_user_info(reply_user, tokens, user_cache)
                    reply_formatted_ts = format_timestamp(reply_ts)

                    # Resolve mentions in reply text
                    resolved_reply_text = resolve_mentions_in_text(
                        reply_text, tokens, user_cache
                    )

                    reply_content = f"""# Reply from @{reply_user_info['username']} in #{channel_name} thread
**Date:** {reply_formatted_ts}
**Thread started by:** @{user_info['username']}

{resolved_reply_text}
"""

                    reply_metadata = {
                        "channel_name": channel_name,
                        "channel_id": channel_id,
                        "message_ts": reply_ts,
                        "thread_ts": msg_ts,  # Links reply to parent
                        "thread_parent_author": user_info["username"],
                        "author_username": reply_user_info["username"],
                        "author_display": reply_user_info["display_name"],
                        "date": reply_formatted_ts.split(" ")[0],
                        "timestamp": reply_formatted_ts,
                        "is_thread_reply": True,
                        "chunk_type": "thread_reply",
                        "filename": f"{channel_name}_{reply_ts}.slack",
                        "filepath": f"slack://{channel_name}/{reply_ts}",
                    }

                    chunks.append(
                        {"content": reply_content, "metadata": reply_metadata}
                    )

                processed_thread_parents.add(msg_ts)

            else:
                # Standalone message (no replies)
                # Resolve mentions in message text
                resolved_msg_text = resolve_mentions_in_text(
                    msg_text, tokens, user_cache
                )

                formatted_ts = format_timestamp(msg_ts)
                content = f"""# Message from @{user_info['username']} in #{channel_name}
**Date:** {formatted_ts}

{resolved_msg_text}
"""

                metadata = {
                    "channel_name": channel_name,
                    "channel_id": channel_id,
                    "message_ts": msg_ts,
                    "author_username": user_info["username"],
                    "author_display": user_info["display_name"],
                    "date": formatted_ts.split(" ")[0],
                    "timestamp": formatted_ts,
                    "is_thread": False,
                    "chunk_type": "message",
                    "filename": f"{channel_name}_{msg_ts}.slack",
                    "filepath": f"slack://{channel_name}/{msg_ts}",
                }

                chunks.append({"content": content, "metadata": metadata})

        process_time = time.time() - process_start
        print(f"  ✓ Processed into {len(chunks)} chunks in {process_time:.2f}s")

    # Save updated user cache to disk
    new_users_cached = len(user_cache) - initial_cache_size
    if new_users_cached > 0:
        print(f"  ✓ Cached {new_users_cached} new users (total: {len(user_cache)})")
    save_user_cache(cache_file, user_cache)

    return chunks
