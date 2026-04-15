#!/usr/bin/env python3
"""
CLI query interface for the RAG system.
Searches the Qdrant unified_knowledge collection using semantic similarity.

Usage:
    python3 query.py "your search query"
    python3 query.py                      # interactive prompt
"""

import sys
import time
from pathlib import Path

# Allow importing search.py from the parent directory
sys.path.insert(0, str(Path(__file__).parent.parent))
from search import search_rag


def main():
    # Get query from command line or interactive prompt
    if len(sys.argv) > 1:
        query = " ".join(sys.argv[1:])
    else:
        query = input("\nEnter your query: ")

    print(f"\nQuerying: {query}")
    print("=" * 60)

    start_time = time.time()
    try:
        result = search_rag(query, n_results=5)
    except ValueError as e:
        print(f"Error: {e}")
        return 1
    elapsed_ms = (time.time() - start_time) * 1000

    print(f"\n⏱️  Query completed in {elapsed_ms:.2f} milliseconds")
    print(f"Collection size: {result['count']:,} chunks\n")

    for i, (doc, meta, dist) in enumerate(
        zip(result["documents"], result["metadatas"], result["distances"]), start=1
    ):
        filename = meta.get("filename", meta.get("filepath", "unknown"))
        source = meta.get("source", "?")
        chunk_type = meta.get("chunk_type", "")
        name = (
            meta.get("puppet_name")
            or meta.get("function_name")
            or meta.get("class_name")
            or ""
        )

        label = f"[{source}] {chunk_type}"
        if name:
            label += f":{name}"

        print(f"{i}. {filename}  —  {label}")
        print(f"   Score: {1 - dist:.4f}  |  Path: {meta.get('filepath', '')}")
        print(f"   {doc[:300].strip()}...")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
