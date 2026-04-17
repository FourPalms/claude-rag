#!/usr/bin/env python3
"""
Reusable RAG search function for Qdrant.
Can be called from query.py CLI or MCP server.
"""

from pathlib import Path
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue
from fastembed import TextEmbedding
from typing import Dict, List, Any, Optional

# Initialize embedding model once (module-level for reuse)
_model = None


def get_embedding_model():
    """Lazy-load embedding model to avoid slow imports."""
    global _model
    if _model is None:
        # Use FastEmbed with ONNX for faster cold starts (3.6x faster than PyTorch)
        _model = TextEmbedding(model_name="sentence-transformers/all-MiniLM-L6-v2")
    return _model


# Payload keys that are safe to expose as exact-match filters.
# All keys are real payload fields verified against the live Qdrant collection
# (see scroll: rag-llm-improvements-20260417.md, progress log 2026-04-17).
_ALLOWED_FILTER_KEYS = {
    "source",      # "archive" | "code" | "jira" | "js_ts" | "puppet" | "sanctum" | "sessions" | "slack"
    "doc_type",    # e.g. "php", "py", "ts", "tsx", "js", "jsx", "md", "jsonl", "jira", "slack", "pp", "rb", "yaml", "yml"
    "chunk_type",  # e.g. "title", "description", "comment" (jira); "class", "function", "method" (code); "message", "thread_parent", "thread_reply" (slack)
    "project",     # Jira only, e.g. "SKY", "BUGS"
    "issue_key",   # Jira only, e.g. "SKY-988"
}


def _build_filter(filters: Optional[Dict[str, Any]]) -> Optional[Filter]:
    """
    Translate a flat dict of {key: value} filters into a Qdrant Filter.

    - Only keys in _ALLOWED_FILTER_KEYS are honored; unknown keys are silently ignored
      to keep behavior forgiving (the MCP layer already validates what the LLM passes).
    - None / empty-string values are skipped (treated as "no filter on this key").
    - All conditions are combined with AND (must).
    - Returns None if no valid filter conditions are produced -> caller does an unfiltered search.
    """
    if not filters:
        return None

    conditions = []
    for key, value in filters.items():
        if key not in _ALLOWED_FILTER_KEYS:
            continue
        if value is None or value == "":
            continue
        conditions.append(FieldCondition(key=key, match=MatchValue(value=value)))

    if not conditions:
        return None
    return Filter(must=conditions)


def search_rag(
    query: str,
    n_results: int = 5,
    source: Optional[str] = None,
    doc_type: Optional[str] = None,
    chunk_type: Optional[str] = None,
    project: Optional[str] = None,
    issue_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Search the RAG knowledge base using semantic similarity, with optional metadata filters.

    Args:
        query: Natural language search query
        n_results: Maximum number of results to return (default 5)
        source: Filter by source type. Valid values:
            "archive", "code", "jira", "js_ts", "puppet", "sanctum", "sessions", "slack"
        doc_type: Filter by document type (e.g. "php", "py", "ts", "tsx", "md", "jira").
            Useful for narrowing source="code" to just PHP or Python.
        chunk_type: Filter by chunk type. Values depend on source:
            - jira: "title", "description", "comment"
            - code / js_ts: "class", "function", "method"
            - slack: "message", "thread_parent", "thread_reply"
            - sessions: "conversation_exchange"
            - puppet: "class", "define", "file", "function", "hiera_data", "method", "module"
        project: Filter by Jira project key (e.g. "SKY", "BUGS"). Jira chunks only.
        issue_key: Filter by exact Jira issue key (e.g. "SKY-988"). Jira chunks only.

    Returns:
        Dictionary with:
            - documents: List of matching text chunks
            - metadatas: List of metadata dicts (filename, filepath, etc.)
            - distances: List of similarity scores (lower = more similar)
            - count: Total documents in collection

    Raises:
        ValueError: If collection doesn't exist (need to run indexer first)

    Notes:
        - If all filter kwargs are None/empty, behavior is identical to unfiltered search.
        - Filter values that match no chunks return an empty result list (no crash).
    """
    # Connect to Qdrant in Docker
    client = QdrantClient(url="http://localhost:6333", timeout=60)

    # Use unified knowledge collection
    collection_name = "unified_knowledge"
    try:
        client.get_collection(collection_name=collection_name)
    except Exception as e:
        raise ValueError(
            f"Collection '{collection_name}' not found. "
            "Run unified_indexer.py first to create index."
        ) from e

    # Generate query embedding
    model = get_embedding_model()
    # FastEmbed returns a generator, convert to list and get first result
    query_vector = list(model.embed([query]))[0].tolist()

    # Build optional metadata filter
    qdrant_filter = _build_filter({
        "source": source,
        "doc_type": doc_type,
        "chunk_type": chunk_type,
        "project": project,
        "issue_key": issue_key,
    })

    # Search collection using vector similarity (optionally filtered).
    # query_filter=None is equivalent to no filter, preserving pre-change behavior.
    search_results = client.query_points(
        collection_name=collection_name,
        query=query_vector,
        limit=n_results,
        query_filter=qdrant_filter,
    ).points

    # Extract documents from payload
    # Convert Qdrant result format to ChromaDB-compatible format for minimal disruption
    documents = []
    metadatas = []
    distances = []

    for hit in search_results:
        # Document text stored in payload with key "document"
        documents.append(hit.payload.get("document", ""))
        # Remove "document" key from metadata to avoid duplication
        metadata = {k: v for k, v in hit.payload.items() if k != "document"}
        metadatas.append(metadata)
        # Qdrant uses score (higher = better), convert to distance (lower = better) for compatibility
        distances.append(1.0 - hit.score)

    # Get collection count
    count_result = client.count(collection_name=collection_name, exact=True)

    # Return structured results with collection metadata (ChromaDB-compatible format)
    return {
        "documents": documents,
        "metadatas": metadatas,
        "distances": distances,
        "count": count_result.count,
    }
