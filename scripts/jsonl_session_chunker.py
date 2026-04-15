"""
JSONL Session Chunker - Parse Claude Code session archives.

Extracts conversation exchanges (user + assistant) from JSONL session files.
Filters to sessions from last 2 months.
Excludes tool calls/results (just conversational content).
"""

import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional


def is_recent_file(filepath: Path, max_age_days: Optional[int] = 60) -> bool:
    """Check if file was modified within the last N days. None = no limit."""
    if max_age_days is None:
        return True
    mtime = datetime.fromtimestamp(filepath.stat().st_mtime, tz=timezone.utc)
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    return mtime >= cutoff


def extract_text_content(message: Dict) -> Optional[str]:
    """
    Extract text content from a message, skipping tool calls/results.

    Returns None if message has no meaningful text content.
    """
    if not message or "content" not in message:
        return None

    content = message["content"]

    # If content is a string, return it directly
    if isinstance(content, str):
        return content.strip() if content.strip() else None

    # If content is a list, extract text blocks (skip tool_use, tool_result)
    if isinstance(content, list):
        text_parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "").strip()
                if text:
                    text_parts.append(text)

        combined = "\n\n".join(text_parts)
        return combined if combined else None

    return None


def chunk_jsonl_session(filepath: Path) -> List[Dict]:
    """
    Parse JSONL session file and extract conversation exchanges.

    Returns list of chunks, each with:
    - content: user message + assistant response
    - metadata: timestamp, session_id, etc.
    """
    chunks = []

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        print(f"⚠️  Error reading {filepath}: {e}")
        return []

    # Parse all records
    records = []
    for line in lines:
        try:
            record = json.loads(line.strip())
            records.append(record)
        except json.JSONDecodeError:
            continue

    # Extract user/assistant message pairs
    i = 0
    while i < len(records):
        record = records[i]

        # Look for user messages
        if record.get("type") == "user":
            user_msg = extract_text_content(record.get("message"))
            user_timestamp = record.get("timestamp")
            session_id = record.get("sessionId")
            cwd = record.get("cwd")
            git_branch = record.get("gitBranch")

            # Look ahead for corresponding assistant response
            assistant_msg = None
            j = i + 1
            while j < len(records):
                next_record = records[j]
                if next_record.get("type") == "assistant":
                    assistant_msg = extract_text_content(next_record.get("message"))
                    break
                elif next_record.get("type") == "user":
                    # Hit next user message without finding assistant response
                    break
                j += 1

            # Create chunk if we have meaningful content
            if user_msg or assistant_msg:
                content_parts = []
                if user_msg:
                    content_parts.append(f"User: {user_msg}")
                if assistant_msg:
                    content_parts.append(f"Assistant: {assistant_msg}")

                content = "\n\n".join(content_parts)

                chunks.append(
                    {
                        "content": content,
                        "metadata": {
                            "timestamp": user_timestamp,
                            "session_id": session_id or "unknown",
                            "cwd": cwd or "unknown",
                            "git_branch": git_branch or "unknown",
                            "chunk_type": "conversation_exchange",
                        },
                    }
                )

        i += 1

    return chunks


def collect_jsonl_sessions(max_age_days: Optional[int] = 60, max_files: Optional[int] = 50) -> List[tuple]:
    """
    Find JSONL session files (excluding subagents).

    Args:
        max_age_days: Only include files modified within this many days (None = no limit)
        max_files: Maximum number of files to return (None = no limit)

    Returns list of (filepath, source) tuples.
    """
    # Import config here to avoid circular imports
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from config import JSONL_SESSION_PATHS

    files = []

    for path_str in JSONL_SESSION_PATHS:
        sessions_dir = Path(path_str).expanduser()

        if not sessions_dir.exists():
            print(f"⚠️  Sessions directory not found: {sessions_dir}")
            continue

        # Find all .jsonl files, excluding subagents
        for jsonl_file in sessions_dir.rglob("*.jsonl"):
            # Skip subagent files
            if "/subagents/" in str(jsonl_file):
                continue

            # Check if file is recent enough
            if is_recent_file(jsonl_file, max_age_days):
                files.append(jsonl_file)

    # Sort by modification time (most recent first)
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)

    # Take only max_files most recent (if limit specified)
    if max_files is not None:
        files = files[:max_files]

    # Return as (filepath, source) tuples
    return [(f, "sessions") for f in files]


def chunk_jsonl_sessions(session_files: List[tuple]) -> List[Dict]:
    """
    Chunk multiple JSONL session files.

    Returns list of chunks with content and metadata.
    """
    all_chunks = []

    for filepath, source in session_files:
        chunks = chunk_jsonl_session(filepath)

        # Add common metadata to all chunks from this file
        for chunk in chunks:
            chunk["metadata"].update(
                {
                    "filepath": str(filepath),
                    "filename": filepath.name,
                    "source_type": "jsonl_session",
                }
            )

        all_chunks.extend(chunks)

    return all_chunks
