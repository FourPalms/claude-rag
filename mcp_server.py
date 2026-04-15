#!/usr/bin/env python3
"""
MCP Server for RAG knowledge base.
Exposes semantic search over team knowledge, code, and conversations.
"""

import sys
import time
from pathlib import Path

# Add scripts directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from mcp.server.fastmcp import FastMCP
from search import search_rag

# Initialize MCP server
mcp = FastMCP("rag-system")


@mcp.tool()
async def search_knowledge(query: str, limit: int = 5) -> str:
    """Search team knowledge, code, and conversation history semantically.

    Searches across:
    - Team documentation (Sanctum, processes, architecture)
    - PHP code (configured directories)
    - Python code (configured directories)
    - Conversation archives (JSONL session logs)
    - Working memory and agent research

    Args:
        query: Natural language search query
        limit: Maximum number of results to return (1-20, default 5)

    Returns:
        Formatted search results with file paths, relevance scores, and previews
    """
    # Validate limit
    limit = max(1, min(20, limit))

    # Time the search operation
    start_time = time.time()

    try:
        results = search_rag(query, n_results=limit)
    except ValueError as e:
        return f"Error: {str(e)}\n\nThe RAG index may not be initialized. Run unified_indexer.py first."
    except Exception as e:
        return f"Search error: {str(e)}"

    elapsed_time = time.time() - start_time

    # Format results for Claude
    output = []
    output.append(
        f"Found {len(results['documents'])} results from {results['count']} total documents"
    )
    output.append(f"Query time: {elapsed_time*1000:.1f}ms")
    output.append("")

    for i, (doc, metadata, distance) in enumerate(
        zip(results["documents"], results["metadatas"], results["distances"]), 1
    ):
        output.append(f"{i}. {metadata['filename']}")
        output.append(f"   Distance: {distance:.4f} (lower = more relevant)")
        output.append(f"   Path: {metadata['filepath']}")

        # Include additional metadata if available
        if "doc_type" in metadata:
            doc_type = metadata["doc_type"]
            if doc_type == "php":
                output.append(
                    f"   Type: PHP code - {metadata.get('chunk_type', 'unknown')}"
                )
                if "function_name" in metadata:
                    output.append(f"   Function: {metadata['function_name']}")
                if "class_name" in metadata:
                    output.append(f"   Class: {metadata['class_name']}")
            elif doc_type == "py":
                output.append(
                    f"   Type: Python code - {metadata.get('chunk_type', 'unknown')}"
                )
                if "function_name" in metadata:
                    output.append(f"   Function: {metadata['function_name']}")
                if "class_name" in metadata:
                    output.append(f"   Class: {metadata['class_name']}")
            else:
                output.append(f"   Type: {doc_type}")

        # Full chunk content
        output.append(f"   Content: {doc}")
        output.append("")

    return "\n".join(output)


def main():
    """Run the MCP server on stdio transport."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
