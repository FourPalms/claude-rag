"""
Slite Note Collector for RAG - Fetch notes via REST API.

Recursively fetches all notes under configured root note IDs
and returns them as searchable chunks (1 note = 1 chunk).

Rate limiting is the normal condition for a full traversal, not an
exceptional one: the walk issues two requests per node with no pacing, and
Slite starts returning 429 partway through. Before the retry logic below, a
429 was treated as a permanent fact about the data — the note was dropped,
and a 429 on a *children* call silently pruned that node's entire subtree,
with nothing in the output saying how much had gone missing. A run on
2026-09-10 lost 9 notes and one subtree of unknown size that way, then the
index wipe-and-replace baked the degraded result in. The existing
"don't wipe on 0 chunks" guard does not help, because a partial pull is not
an empty one.
"""

import time
from typing import Callable, Dict, List, NamedTuple, Tuple

import requests

SLITE_API_BASE = "https://api.slite.com/v1"

# Retry schedule for a throttled or briefly failing request, in seconds
# between attempts. Slite sends Retry-After on most 429s and that value wins
# when present; this is the fallback and the cap on how long a single node may
# hold up the walk.
RETRY_DELAYS = (1, 2, 5, 10)

# Ceiling on a server-supplied Retry-After. A pathological value would
# otherwise stall the whole run behind one note.
MAX_RETRY_AFTER = 30

# Requests that are worth retrying. 429 is throttling; 5xx is the API having a
# bad moment. A 401/403/404 will not improve by asking again.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class TraversalReport(NamedTuple):
    """
    What the walk could not retrieve, alongside what it did.

    A caller needs to distinguish "Slite has 109 notes" from "we managed to
    read 109 notes", which is why the losses are counted rather than only
    printed. `pruned_subtrees` is the number of nodes whose children call
    failed after retries: each one hides an unknown number of descendants, so
    it is the count that should make a reader distrust the total.
    """

    notes_fetched: int
    nodes_visited: int
    failed_notes: List[str]
    pruned_subtrees: List[str]

    @property
    def degraded(self) -> bool:
        return bool(self.failed_notes or self.pruned_subtrees)

    def summary(self) -> str:
        """One-line description of the losses, for logs and alerts."""
        if not self.degraded:
            return f"{self.notes_fetched} notes, complete"
        parts = []
        if self.failed_notes:
            parts.append(f"{len(self.failed_notes)} note(s) unreadable")
        if self.pruned_subtrees:
            parts.append(
                f"{len(self.pruned_subtrees)} subtree(s) unwalkable "
                "(descendants of unknown number missing)"
            )
        return f"{self.notes_fetched} notes fetched but incomplete: " + ", ".join(parts)


def _get_headers(api_key: str) -> Dict:
    return {"x-slite-api-key": api_key, "Accept": "application/json"}


def _retry_after_seconds(response: requests.Response, fallback: int) -> float:
    """
    How long to wait before retrying, preferring the server's own instruction.

    Retry-After may be absent or malformed, so the caller's backoff step is
    used whenever a sane number cannot be read from it.
    """
    header = response.headers.get("Retry-After")
    if not header:
        return fallback
    try:
        return min(float(header), MAX_RETRY_AFTER)
    except (TypeError, ValueError):
        return fallback


def _get_with_retry(
    api_key: str,
    url: str,
    delays: Tuple[int, ...] = RETRY_DELAYS,
    sleep: Callable[[float], None] = time.sleep,
) -> requests.Response | None:
    """
    GET a Slite URL, retrying throttling and transient server errors.

    Returns the successful response, or None once the retries are spent or the
    failure is one that retrying cannot fix. `sleep` is injectable so tests do
    not idle.
    """
    last_error: Exception | None = None

    for attempt in range(len(delays) + 1):
        try:
            response = requests.get(url, headers=_get_headers(api_key), timeout=10)
        except requests.exceptions.RequestException as request_error:
            last_error = request_error
        else:
            if response.status_code not in RETRYABLE_STATUS:
                try:
                    response.raise_for_status()
                    return response
                except requests.exceptions.RequestException as http_error:
                    print(f"  ⚠️  {url}: {http_error}")
                    return None
            last_error = requests.exceptions.HTTPError(
                f"HTTP {response.status_code}", response=response
            )

        if attempt == len(delays):
            break

        wait = delays[attempt]
        if isinstance(last_error, requests.exceptions.HTTPError) and (
            last_error.response is not None
        ):
            wait = _retry_after_seconds(last_error.response, wait)
        sleep(wait)

    print(f"  ⚠️  {url}: giving up after {len(delays)} retries — {last_error}")
    return None


def _fetch_note(api_key: str, note_id: str, sleep=time.sleep) -> Dict | None:
    """Fetch a single note by ID."""
    response = _get_with_retry(
        api_key, f"{SLITE_API_BASE}/notes/{note_id}", sleep=sleep
    )
    if response is None:
        return None
    return response.json()


def _fetch_children(
    api_key: str, note_id: str, sleep=time.sleep
) -> Tuple[List[Dict], bool]:
    """
    Fetch direct children of a note.

    Returns the children and whether the call succeeded. The second value
    matters: an empty list from a leaf node and an empty list from a failed
    request are indistinguishable otherwise, and conflating them is what let a
    whole subtree disappear without trace.
    """
    url = f"{SLITE_API_BASE}/notes/{note_id}/children"
    response = _get_with_retry(api_key, url, sleep=sleep)
    if response is None:
        return [], False

    data = response.json()
    # API returns {"notes": [...]} or a list directly
    if isinstance(data, list):
        return data, True
    return data.get("notes", []), True


def _collect_recursive(
    api_key: str,
    note_id: str,
    chunks: List[Dict],
    visited: set,
    failed_notes: List[str],
    pruned_subtrees: List[str],
    depth: int = 0,
    sleep=time.sleep,
) -> None:
    """Recursively collect a note and all its descendants."""
    if note_id in visited:
        return
    visited.add(note_id)

    note = _fetch_note(api_key, note_id, sleep=sleep)
    if not note:
        failed_notes.append(note_id)
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

        chunks.append(
            {
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
            }
        )

    # Recurse into children
    children, children_ok = _fetch_children(api_key, note_id_actual, sleep=sleep)
    if not children_ok:
        pruned_subtrees.append(note_id_actual)
        return

    for child in children:
        child_id = child.get("id")
        if child_id:
            _collect_recursive(
                api_key,
                child_id,
                chunks,
                visited,
                failed_notes,
                pruned_subtrees,
                depth + 1,
                sleep=sleep,
            )


def collect_slite_docs_with_report(
    api_key: str, root_note_ids: List[str], sleep=time.sleep
) -> Tuple[List[Dict], TraversalReport]:
    """
    Fetch all notes under root_note_ids recursively, reporting what was missed.

    Prefer this over collect_slite_docs when the caller can act on a degraded
    pull; the report is the only way to tell a complete traversal from one that
    lost a subtree.
    """
    chunks: List[Dict] = []
    visited: set = set()
    failed_notes: List[str] = []
    pruned_subtrees: List[str] = []

    print(f"  Fetching Slite notes from {len(root_note_ids)} root note(s)...")

    for root_id in root_note_ids:
        _collect_recursive(
            api_key,
            root_id,
            chunks,
            visited,
            failed_notes,
            pruned_subtrees,
            sleep=sleep,
        )

    report = TraversalReport(
        notes_fetched=len(chunks),
        nodes_visited=len(visited),
        failed_notes=failed_notes,
        pruned_subtrees=pruned_subtrees,
    )

    print(f"  ✓ Fetched {len(chunks)} Slite notes ({len(visited)} total nodes visited)")
    if report.degraded:
        print(f"  ⚠️  Slite pull incomplete: {report.summary()}")
        if pruned_subtrees:
            print(
                "     Unwalkable parents: "
                + ", ".join(pruned_subtrees[:10])
                + (" ..." if len(pruned_subtrees) > 10 else "")
            )

    return chunks, report


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
    chunks, _report = collect_slite_docs_with_report(api_key, root_note_ids)
    return chunks
