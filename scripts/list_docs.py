#!/usr/bin/env python3
"""
List all documents in the RAG index, grouped by source.

Usage:
    python3 list_docs.py
"""

import sys
from collections import defaultdict
from qdrant_client import QdrantClient


def main():
    client = QdrantClient(url="http://localhost:6333")
    collection_name = "unified_knowledge"

    try:
        info = client.get_collection(collection_name)
    except Exception:
        print(f"Error: Collection '{collection_name}' not found.")
        print("Run unified_indexer.py first to create index.")
        return 1

    print(f"Collection: {collection_name}")
    print(f"Total chunks: {info.points_count:,}\n")
    print("=" * 80)

    # Scroll all points to gather metadata
    offset = None
    all_payloads = []
    while True:
        results, offset = client.scroll(
            collection_name=collection_name,
            limit=1000,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        all_payloads.extend(r.payload for r in results)
        if offset is None:
            break

    # Group by source → parent_dir
    by_source = defaultdict(lambda: defaultdict(list))
    for payload in all_payloads:
        source = payload.get("source", "unknown")
        parent_dir = payload.get("parent_dir", payload.get("filename", "unknown"))
        filename = payload.get("filename", "unknown")
        by_source[source][parent_dir].append(filename)

    for source in sorted(by_source.keys()):
        print(f"\n{'='*60}")
        print(f"SOURCE: {source.upper()}")
        print("=" * 60)
        for dir_name in sorted(by_source[source].keys()):
            print(f"\n  {dir_name}/")
            print("  " + "-" * 38)
            seen = sorted(set(by_source[source][dir_name]))
            for filename in seen:
                count = by_source[source][dir_name].count(filename)
                suffix = f" ({count}x)" if count > 1 else ""
                print(f"    {filename}{suffix}")

    print("\n" + "=" * 80)
    print(f"Total: {len(all_payloads):,} chunks indexed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
