"""
Archive Chunker - Parse and chunk archive files by session.

Handles:
- working-memory.md (session summaries)
- Future: JSONL session logs
"""

import re
from pathlib import Path
from typing import List, Dict


def chunk_working_memory_archive(filepath: Path) -> List[Dict]:
    """
    Parse working-memory.md archive and chunk by session.

    Returns list of chunks, each with:
    - content: the session text
    - metadata: session_number, session_date, etc.
    """
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        print(f"⚠️  Error reading {filepath}: {e}")
        return []

    chunks = []

    # Split on session headers: ### YYYY-MM-DD (Session N)
    # Pattern: ### followed by date and session number
    session_pattern = r"^### (\d{4}-\d{2}-\d{2}) \(Session (\d+)\)"

    lines = content.split("\n")
    current_session = None
    current_content = []

    for line in lines:
        match = re.match(session_pattern, line)

        if match:
            # Save previous session if exists
            if current_session:
                session_text = "\n".join(current_content).strip()
                if session_text:
                    chunks.append(
                        {"content": session_text, "metadata": current_session}
                    )

            # Start new session
            session_date = match.group(1)
            session_number = int(match.group(2))

            current_session = {
                "session_number": session_number,
                "session_date": session_date,
                "chunk_type": "session",
            }
            current_content = [line]  # Include the header
        else:
            if current_session is not None:
                current_content.append(line)

    # Don't forget the last session
    if current_session and current_content:
        session_text = "\n".join(current_content).strip()
        if session_text:
            chunks.append({"content": session_text, "metadata": current_session})

    return chunks


def chunk_archive_file(filepath: Path, source: str) -> List[Dict]:
    """
    Chunk an archive file based on its type.

    Returns list of chunks with content and metadata.
    """
    filename = filepath.name

    # Working memory archive
    if filename == "working-memory.md":
        chunks = chunk_working_memory_archive(filepath)

        # Add common metadata to all chunks
        for chunk in chunks:
            chunk["metadata"].update(
                {
                    "filepath": str(filepath),
                    "filename": filename,
                    "archive_type": "working_memory",
                }
            )

        return chunks

    # Future: JSONL session logs
    # elif filename.endswith('.jsonl'):
    #     return chunk_session_logs(filepath)

    # Default: treat as single chunk (fallback)
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()

        return [
            {
                "content": content,
                "metadata": {
                    "filepath": str(filepath),
                    "filename": filename,
                    "chunk_type": "full_file",
                },
            }
        ]
    except Exception as e:
        print(f"⚠️  Error reading {filepath}: {e}")
        return []
