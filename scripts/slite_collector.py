"""
Slite Note Collector for RAG - Fetch notes via REST API.

Recursively fetches all notes under configured root note IDs
and returns them as searchable chunks (1 note = 1 chunk).
"""

import requests
from typing import List, Dict


SLITE_API_BASE = "https://api.slite.com/v1"


def _get_headers(api_key: str) -> Dict:
    return {"x-slite-api-key": api_key, "Accept": "application/json"}


def _fetch_note(api_key: str, note_id: str) -> Dict | None:
    """Fetch a single note by ID."""
    url = f"{SLITE_API_BASE}/notes/{note_id}"
    try:
        response = requests.get(url, headers=_get_headers(api_key), timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as fetch_exception:
        print(f"  ⚠️  Error fetching note {note_id}: {fetch_exception}")
        return None


def _fetch_children(api_key: str, note_id: str) -> List[Dict]:
    """Fetch direct children of a note."""
    url = f"{SLITE_API_BASE}/notes/{note_id}/children"
    try:
        response = requests.get(url, headers=_get_headers(api_key), timeout=10)
        response.raise_for_status()
        data = response.json()
        # API returns {"notes": [...]} or a list directly
        if isinstance(data, list):
            return data
        return data.get("notes", [])
    except requests.exceptions.RequestException as fetch_exception:
        print(f"  ⚠️  Error fetching children of {note_id}: {fetch_exception}")
        return []


def _collect_recursive(
    api_key: str,
    note_id: str,
    chunks: List[Dict],
    visited: set,
    depth: int = 0,
) -> None:
    """Recursively collect a note and all its descendants."""
    if note_id in visited:
        return
    visited.add(note_id)

    note = _fetch_note(api_key, note_id)
    if not note:
        return

    note_id_actual = note.get("id", note_id)
    title = note.get("title", "Untitled")
    content = note.get("content", "") or ""
    updated_at = (note.get("updatedAt") or "")[:10]
    created_at = (note.get("createdAt") or "")[:10]
    parent_id = note.get("parentNoteId", "")

    # Skip folder-like notes with no content (but still recurse into children)
    if content.strip():
        chunk_content = f"# {title}\n\n{content}"

        chunks.append({
            "content": chunk_content,
            "metadata": {
                "note_id": note_id_actual,
                "title": title,
                "parent_note_id": parent_id or "",
                "created": created_at,
                "updated": updated_at,
                "filename": f"{title}.slite",
                "filepath": f"slite://{note_id_actual}",
                "chunk_type": "note",
            },
        })

    # Recurse into children
    children = _fetch_children(api_key, note_id_actual)
    for child in children:
        child_id = child.get("id")
        if child_id:
            _collect_recursive(api_key, child_id, chunks, visited, depth + 1)


def collect_slite_docs(api_key: str, root_note_ids: List[str]) -> List[Dict]:
    """
    Fetch all notes under root_note_ids recursively via Slite REST API.

    Args:
        api_key: Slite API key (x-slite-api-key)
        root_note_ids: List of root note IDs to traverse from

    Returns:
        List of chunks, each with:
        - content: Note title + markdown body
        - metadata: note_id, title, parent_note_id, created, updated, filepath
    """
    chunks: List[Dict] = []
    visited: set = set()

    print(f"  Fetching Slite notes from {len(root_note_ids)} root note(s)...")

    for root_id in root_note_ids:
        _collect_recursive(api_key, root_id, chunks, visited)

    print(f"  ✓ Fetched {len(chunks)} Slite notes ({len(visited)} total nodes visited)")
    return chunks
