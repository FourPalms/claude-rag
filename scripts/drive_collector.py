"""
Google Drive meeting-doc collector for RAG.

Fetches Gemini-generated meeting documents from Drive and hands them to
transcript_chunker. Uses the Drive v3 and Docs v1 REST APIs directly over
`requests` -- no Google client libraries -- so this adds no dependencies
beyond what slite_collector.py already needs.

Credentials (option A): reuses the OAuth client and refresh token already on
this machine for the google-docs MCP server. Nothing is hardcoded here; both
are read at call time from:

    ~/.claude.json                              client id + secret
    ~/.config/google-docs-mcp/token.json        refresh token

That means this collector and the MCP server share one credential. A revoke or
re-consent on either side breaks both, and this read-only job inherits whatever
write scopes that client was granted. A separate read-only client would be
better hygiene; this was chosen deliberately for zero setup cost.

A Gemini meeting doc has several tabs. Two carry content worth indexing:
"Full notes" (Summary / Next steps / Details) and "Transcript". Tab bodies come
back as structural JSON, so `_tab_to_markdown` reconstructs just enough markdown
-- headings, list items, bold -- for the chunker's patterns to match.
"""

import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

from transcript_chunker import chunk_meeting

TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
DOCS_URL = "https://docs.googleapis.com/v1/documents"

CLAUDE_CONFIG = Path.home() / ".claude.json"
MCP_TOKEN_FILE = Path.home() / ".config/google-docs-mcp/token.json"

NOTES_TAB_NAMES = ("full notes", "notes")
TRANSCRIPT_TAB_NAMES = ("transcript",)

# Gemini titles docs "<Meeting> - 2026/09/03 10:33 MDT - Notes by Gemini".
TITLE_DATE_RE = re.compile(r"(\d{4})/(\d{2})/(\d{2})")


class DriveAuthError(RuntimeError):
    """Raised when credentials are missing, revoked, or lack the needed scope."""


def _load_credentials() -> Tuple[str, str, str]:
    """Read the client id/secret and refresh token from their existing homes."""
    if not MCP_TOKEN_FILE.exists():
        raise DriveAuthError(f"No refresh token at {MCP_TOKEN_FILE}")
    refresh_token = json.loads(MCP_TOKEN_FILE.read_text()).get("refresh_token")
    if not refresh_token:
        raise DriveAuthError(f"{MCP_TOKEN_FILE} has no refresh_token")

    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    if not (client_id and client_secret):
        # Fall back to the google-docs MCP server's declared env args.
        if not CLAUDE_CONFIG.exists():
            raise DriveAuthError(f"No client credentials in env and no {CLAUDE_CONFIG}")
        args = (
            json.loads(CLAUDE_CONFIG.read_text())
            .get("mcpServers", {})
            .get("google-docs", {})
            .get("args", [])
        )
        for arg in args:
            if arg.startswith("GOOGLE_CLIENT_ID="):
                client_id = arg.split("=", 1)[1]
            elif arg.startswith("GOOGLE_CLIENT_SECRET="):
                client_secret = arg.split("=", 1)[1]
    if not (client_id and client_secret):
        raise DriveAuthError(
            "Could not resolve GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET"
        )
    return client_id, client_secret, refresh_token


def _access_token() -> str:
    """Exchange the refresh token for a short-lived access token."""
    client_id, client_secret, refresh_token = _load_credentials()
    response = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=20,
    )
    if response.status_code != 200:
        raise DriveAuthError(
            "Refresh failed "
            f"({response.status_code}). The token may be revoked or expired; "
            "re-authorize the google-docs MCP server, or switch to a dedicated "
            f"read-only client. Response: {response.text[:200]}"
        )
    token = response.json().get("access_token")
    if not token:
        raise DriveAuthError("Token endpoint returned no access_token")
    return token


def _check(response: requests.Response, what: str) -> Dict:
    """Turn an API error into a message that says what to do about it."""
    if response.status_code == 403 and "insufficientPermissions" in response.text:
        raise DriveAuthError(
            f"{what}: the shared credential lacks the required scope. It was "
            "consented for the google-docs MCP server, which may not include "
            "drive.readonly. Create a dedicated read-only client (drive.readonly "
            "+ documents.readonly) and point this collector at its token."
        )
    if response.status_code != 200:
        raise RuntimeError(
            f"{what} failed ({response.status_code}): {response.text[:200]}"
        )
    return response.json()


def _list_meeting_docs(
    token: str,
    name_contains: str,
    modified_after: Optional[str] = None,
    max_docs: int = 500,
) -> List[Dict]:
    """List Google Docs whose title matches, newest first, across owned + shared."""
    clauses = [
        f"name contains '{name_contains}'",
        "mimeType = 'application/vnd.google-apps.document'",
        "trashed = false",
    ]
    if modified_after:
        clauses.append(f"modifiedTime > '{modified_after}'")

    files: List[Dict] = []
    page_token = None
    while len(files) < max_docs:
        params = {
            "q": " and ".join(clauses),
            "fields": "nextPageToken, files(id, name, modifiedTime, owners(emailAddress))",
            "orderBy": "modifiedTime desc",
            "pageSize": 100,
            # Shared drives as well as My Drive.
            "includeItemsFromAllDrives": "true",
            "supportsAllDrives": "true",
        }
        if page_token:
            params["pageToken"] = page_token
        payload = _check(
            requests.get(
                DRIVE_FILES_URL,
                headers={"Authorization": f"Bearer {token}"},
                params=params,
                timeout=30,
            ),
            "Drive files.list",
        )
        files.extend(payload.get("files", []))
        page_token = payload.get("nextPageToken")
        if not page_token:
            break
    return files[:max_docs]


def _paragraph_to_markdown(paragraph: Dict) -> str:
    """Render one paragraph, keeping heading level, bullets, and bold."""
    pieces: List[str] = []
    for element in paragraph.get("elements", []):
        run = element.get("textRun")
        if not run:
            continue
        text = run.get("content", "").replace("\v", "\n")
        if not text.strip():
            pieces.append(text)
            continue
        if run.get("textStyle", {}).get("bold"):
            # Keep trailing whitespace outside the emphasis markers.
            stripped = text.strip()
            trailing = text[len(text.rstrip()) :]
            pieces.append(f"**{stripped}**{trailing}")
        else:
            pieces.append(text)
    line = "".join(pieces).rstrip("\n")
    if not line.strip():
        return ""

    style = paragraph.get("paragraphStyle", {}).get("namedStyleType", "")
    heading = re.match(r"HEADING_(\d)", style)
    if heading:
        return f"{'#' * min(int(heading.group(1)), 4)} {line.strip()}"
    if style == "TITLE":
        return f"# {line.strip()}"
    if "bullet" in paragraph:
        return f"- {line.strip()}"
    return line


def _tab_to_markdown(tab: Dict) -> str:
    """Flatten a documentTab body into markdown-ish text."""
    body = tab.get("documentTab", {}).get("body", {})
    lines: List[str] = []
    for element in body.get("content", []):
        paragraph = element.get("paragraph")
        if paragraph:
            rendered = _paragraph_to_markdown(paragraph)
            lines.append(rendered)
            continue
        table = element.get("table")
        if table:
            for row in table.get("tableRows", []):
                cells = []
                for cell in row.get("tableCells", []):
                    cell_text = " ".join(
                        _paragraph_to_markdown(item["paragraph"]).strip()
                        for item in cell.get("content", [])
                        if item.get("paragraph")
                    )
                    cells.append(cell_text.strip())
                if any(cells):
                    lines.append("- " + " | ".join(cells))
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _walk_tabs(tabs: List[Dict]) -> List[Dict]:
    """Flatten nested tabs into one list."""
    flat: List[Dict] = []
    for tab in tabs or []:
        flat.append(tab)
        flat.extend(_walk_tabs(tab.get("childTabs", [])))
    return flat


def _fetch_doc_tabs(token: str, doc_id: str) -> Dict[str, str]:
    """Return {tab_title_lower: markdown} for one document."""
    payload = _check(
        requests.get(
            f"{DOCS_URL}/{doc_id}",
            headers={"Authorization": f"Bearer {token}"},
            params={"includeTabsContent": "true"},
            timeout=60,
        ),
        f"Docs documents.get({doc_id})",
    )
    result: Dict[str, str] = {}
    for tab in _walk_tabs(payload.get("tabs", [])):
        title = tab.get("tabProperties", {}).get("title", "").strip().lower()
        if title:
            result[title] = _tab_to_markdown(tab)
    return result


def _pick(tabs: Dict[str, str], names: Tuple[str, ...]) -> str:
    """First non-empty tab whose title matches one of `names`."""
    for name in names:
        for title, text in tabs.items():
            if title == name and text.strip():
                return text
    for name in names:
        for title, text in tabs.items():
            if name in title and text.strip():
                return text
    return ""


def collect_drive_meetings(
    name_contains: str = "Notes by Gemini",
    modified_after: Optional[str] = None,
    max_docs: int = 500,
) -> List[Dict]:
    """
    Fetch Gemini meeting docs from Drive and return chunks.

    Args:
        name_contains: title substring identifying meeting docs
        modified_after: RFC3339 timestamp for an incremental pull
        max_docs: safety cap on how many documents to fetch

    Returns:
        Chunks in the collector contract: {"content", "metadata"} with a
        chunk_type of meeting_summary / meeting_action_item / meeting_topic /
        transcript_turn_group.
    """
    token = _access_token()
    docs = _list_meeting_docs(token, name_contains, modified_after, max_docs)
    print(
        f"  Found {len(docs)} meeting doc(s) matching {name_contains!r}"
        + (f" modified after {modified_after}" if modified_after else "")
    )

    chunks: List[Dict] = []
    skipped = 0
    for index, doc in enumerate(docs, 1):
        doc_id, name = doc["id"], doc.get("name", "Untitled")
        try:
            tabs = _fetch_doc_tabs(token, doc_id)
        except DriveAuthError:
            raise
        except (
            Exception
        ) as fetch_exception:  # noqa: BLE001 - one bad doc must not stop the run
            print(f"  ⚠️  [{index}/{len(docs)}] {name}: {fetch_exception}")
            skipped += 1
            continue

        notes = _pick(tabs, NOTES_TAB_NAMES)
        transcript = _pick(tabs, TRANSCRIPT_TAB_NAMES)
        if not (notes.strip() or transcript.strip()):
            skipped += 1
            continue

        date_match = TITLE_DATE_RE.search(name)
        base = {
            "doc_id": doc_id,
            "title": name.split(" - Notes by Gemini")[0].strip(),
            "meeting_date": "-".join(date_match.groups()) if date_match else "",
            "modified": (doc.get("modifiedTime") or "")[:10],
            # Named doc_owner, not owner: meeting_action_item chunks use `owner`
            # for the action's assignee, and a shared key would silently collide.
            "doc_owner": (doc.get("owners") or [{}])[0].get("emailAddress", ""),
            "filename": f"{name}.gdoc",
            "filepath": f"gdoc://{doc_id}",
        }
        doc_chunks = chunk_meeting(base, full_notes=notes, transcript=transcript)
        chunks.extend(doc_chunks)
        if index % 25 == 0 or index == len(docs):
            print(f"  … {index}/{len(docs)} docs, {len(chunks)} chunks so far")

    print(
        f"  ✓ {len(chunks)} chunks from {len(docs) - skipped} doc(s)"
        + (f", {skipped} skipped" if skipped else "")
    )
    return chunks
