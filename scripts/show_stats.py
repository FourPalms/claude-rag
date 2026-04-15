#!/usr/bin/env python3
"""
RAG Database Statistics

Display comprehensive statistics about the RAG database including:
- Total chunks and vector dimensions
- Breakdown by source (code, sessions, jira, etc.)
- Breakdown by document type (php, jsonl, md, etc.)
- Jira-specific chunk types (title, description, comments)

Usage:
    python3 show_stats.py
"""

from qdrant_client import QdrantClient
from collections import Counter


def main():
    # Connect to Qdrant server
    client = QdrantClient(url="http://localhost:6333")

    # Get collection info
    collection_info = client.get_collection("unified_knowledge")

    print("RAG Database Statistics")
    print("=" * 60)
    print(f"Collection name: unified_knowledge")
    print(f"Total chunks: {collection_info.points_count:,}")
    print(f"Vector size: {collection_info.config.params.vectors.size}")
    print()

    # Get all points to analyze metadata
    offset = None
    all_points = []
    batch_size = 1000

    print("Analyzing metadata...")
    while True:
        results = client.scroll(
            collection_name="unified_knowledge",
            limit=batch_size,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        points, offset = results
        all_points.extend(points)
        if offset is None:
            break
        if len(all_points) % 5000 == 0:
            print(f"  Loaded {len(all_points):,} chunks...")

    print(f"\nAnalyzed {len(all_points):,} total chunks")
    print()

    # Analyze by source
    sources = Counter()
    doc_types = Counter()
    chunk_types = Counter()

    for point in all_points:
        payload = point.payload
        sources[payload.get("source", "unknown")] += 1
        doc_type = payload.get("doc_type", "unknown")
        doc_types[doc_type] += 1

        # Chunk type (for all content)
        if payload.get("chunk_type"):
            chunk_types[payload["chunk_type"]] += 1

    print("Breakdown by Source:")
    print("-" * 60)
    for source, count in sources.most_common():
        pct = (count / len(all_points)) * 100
        print(f"  {source:20} {count:7,} chunks ({pct:5.1f}%)")

    print()
    print("Breakdown by Document Type:")
    print("-" * 60)
    for doc_type, count in doc_types.most_common():
        pct = (count / len(all_points)) * 100
        print(f"  {doc_type:20} {count:7,} chunks ({pct:5.1f}%)")

    print()
    print("Top Chunk Types:")
    print("-" * 60)
    for chunk_type, count in chunk_types.most_common(15):
        pct = (count / len(all_points)) * 100
        print(f"  {chunk_type:20} {count:7,} chunks ({pct:5.1f}%)")


if __name__ == "__main__":
    main()
