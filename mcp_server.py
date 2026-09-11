#!/usr/bin/env python3
"""
MCP Server for RAG knowledge base.
Exposes semantic search over team knowledge, code, and conversations.
"""

import sys
import time
from pathlib import Path
from typing import Optional

# Add scripts directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from mcp.server.fastmcp import FastMCP
from search import search_rag

# Initialize MCP server
mcp = FastMCP("rag-system")


@mcp.tool()
async def search_knowledge(
    query: str,
    limit: int = 5,
    source: Optional[str] = None,
    doc_type: Optional[str] = None,
    chunk_type: Optional[str] = None,
    project: Optional[str] = None,
    issue_key: Optional[str] = None,
) -> str:
    """Search team knowledge, code, and conversation history semantically.

    Searches across a unified index of team documentation, code, Jira tickets,
    Slack messages, Slite notes, Puppet configs, session transcripts, and archives.
    Optional filters narrow the search to a subset of chunks — pass them when you
    know what kind of content you want (big context win; filter values that match
    no chunks return an empty result list, never an error).

    Args:
        query: Natural language search query.
        limit: Maximum number of results to return (1-20, default 5).
        source: Restrict to one source. Valid values:
            - "jira"     — Jira issues (titles, descriptions, comments)
            - "slack"    — Slack messages and thread replies
            - "code"     — PHP and Python source code (use doc_type to pick one)
            - "js_ts"    — JS / TS / JSX / TSX source code
            - "puppet"   — Puppet manifests, Ruby, Hiera YAML
            - "sanctum"  — Team knowledge base (markdown)
            - "sessions" — Claude Code session transcripts (JSONL)
            - "archive"  — Archived session logs / old working memory
            - "meetings" — Meeting docs (Gemini notes + transcripts from Meet)
            Any other value returns empty.
        doc_type: Restrict to a file/content type. Useful values:
            - "php", "py"           — under source="code"
            - "ts", "tsx", "js", "jsx" — under source="js_ts"
            - "pp", "rb", "yaml", "yml" — under source="puppet"
            - "md"                  — markdown (sanctum, archive)
            - "jira", "slack", "jsonl" — set to the source name for those sources
            - "gdoc"                — under source="meetings"
        chunk_type: Restrict to a chunk kind. Meaning depends on source:
            - jira:     "title", "description", "comment"
            - code:     "class", "function", "method"
            - js_ts:    "class", "function", "method"
            - slack:    "message", "thread_parent", "thread_reply"
            - sessions: "conversation_exchange"
            - puppet:   "class", "define", "file", "function", "hiera_data", "method", "module"
            - archive:  "session"
            - meetings: "meeting_summary" (whole-meeting summary — the densest
                        and usually the best first hit), "meeting_topic" (one
                        discussion topic, carries a transcript timestamp),
                        "meeting_action_item" (one next step, with its owner),
                        "transcript_turn_group" (raw speaker turns — verbatim
                        but noisy; prefer the first three unless you need the
                        exact wording)
        project: Jira project key, e.g. "SKY" or "BUGS". Only matches Jira chunks.
            Implicitly narrows to source="jira" because no other source carries this field.
        issue_key: Exact Jira issue key, e.g. "SKY-988". Only matches Jira chunks
            (title + description + every comment for that issue). Best way to pull
            everything about a single ticket.

    Returns:
        Formatted search results with file paths, relevance scores, and previews.
    """
    # Validate limit
    limit = max(1, min(20, limit))

    # Time the search operation
    start_time = time.time()

    try:
        results = search_rag(
            query,
            n_results=limit,
            source=source,
            doc_type=doc_type,
            chunk_type=chunk_type,
            project=project,
            issue_key=issue_key,
        )
    except ConnectionError as e:
        # Qdrant is down, not empty. Say so plainly so this doesn't get
        # misdiagnosed as a lost index — no re-indexing is needed.
        return f"RAG unavailable: {str(e)}"
    except ValueError as e:
        return f"Error: {str(e)}\n\nThe RAG index may not be initialized. Run unified_indexer.py first."
    except Exception as e:
        return f"Search error: {str(e)}"

    elapsed_time = time.time() - start_time

    # Build a short filter summary for the header (so the LLM can tell at a glance
    # whether its filters were actually applied).
    active_filters = {
        k: v
        for k, v in {
            "source": source,
            "doc_type": doc_type,
            "chunk_type": chunk_type,
            "project": project,
            "issue_key": issue_key,
        }.items()
        if v
    }

    # Format results for Claude
    output = []
    output.append(
        f"Found {len(results['documents'])} results from {results['count']} total documents"
    )
    output.append(f"Query time: {elapsed_time*1000:.1f}ms")
    if active_filters:
        filters_str = ", ".join(f"{k}={v}" for k, v in active_filters.items())
        output.append(f"Filters: {filters_str}")
    output.append("")

    for i, (doc, metadata, distance) in enumerate(
        zip(results["documents"], results["metadatas"], results["distances"]), 1
    ):
        output.append(f"{i}. {metadata['filename']}")
        output.append(f"   Distance: {distance:.4f} (lower = more relevant)")
        output.append(f"   Path: {metadata['filepath']}")

        # Include additional metadata if available
        if "doc_type" in metadata:
            doc_type_val = metadata["doc_type"]
            if doc_type_val == "php":
                output.append(
                    f"   Type: PHP code - {metadata.get('chunk_type', 'unknown')}"
                )
                if "function_name" in metadata:
                    output.append(f"   Function: {metadata['function_name']}")
                if "class_name" in metadata:
                    output.append(f"   Class: {metadata['class_name']}")
            elif doc_type_val == "py":
                output.append(
                    f"   Type: Python code - {metadata.get('chunk_type', 'unknown')}"
                )
                if "function_name" in metadata:
                    output.append(f"   Function: {metadata['function_name']}")
                if "class_name" in metadata:
                    output.append(f"   Class: {metadata['class_name']}")
            elif doc_type_val == "gdoc":
                output.append(
                    f"   Type: meeting - {metadata.get('chunk_type', 'unknown')}"
                )
                if metadata.get("meeting_date"):
                    output.append(f"   Meeting date: {metadata['meeting_date']}")
                if metadata.get("ts_start"):
                    output.append(
                        f"   Transcript position: {metadata['ts_start']} "
                        "(open the source doc to verify)"
                    )
                if metadata.get("owner"):
                    output.append(f"   Action owner: {metadata['owner']}")
                if metadata.get("speakers"):
                    output.append(f"   Speakers: {metadata['speakers']}")

                # Attribution is the one field a reader must not miss. Google
                # Meet labels everyone sharing a conference room with the room
                # name, so Gemini credits statements and action items to the
                # room rather than a person. Presenting such a chunk as
                # somebody's words invents a quote, so say so in the result
                # itself -- a flag nobody sees protects nobody.
                attribution = metadata.get("speaker_attribution")
                if attribution == "room":
                    output.append(
                        "   ⚠️  ATTRIBUTION: speakers here are a CONFERENCE ROOM, "
                        "not a person. Do not attribute anything in this chunk to "
                        "an individual."
                    )
                elif attribution == "inferred":
                    output.append(
                        "   ⚠️  ATTRIBUTION: speaker labels were inferred from turn "
                        "shape, not recorded by Meet. Treat them as proposed, not "
                        "authoritative."
                    )
            else:
                output.append(f"   Type: {doc_type_val}")

        # Full chunk content
        output.append(f"   Content: {doc}")
        output.append("")

    return "\n".join(output)


def main():
    """Run the MCP server on stdio transport."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
