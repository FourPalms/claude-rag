#!/usr/bin/env python3
"""
Reusable RAG search function for Qdrant.
Can be called from query.py CLI or MCP server.
"""

from pathlib import Path
from qdrant_client import QdrantClient
from fastembed import TextEmbedding
from typing import Dict, List, Any

# Initialize embedding model once (module-level for reuse)
_model = None


def get_embedding_model():
    """Lazy-load embedding model to avoid slow imports."""
    global _model
    if _model is None:
        # Use FastEmbed with ONNX for faster cold starts (3.6x faster than PyTorch)
        _model = TextEmbedding(model_name="sentence-transformers/all-MiniLM-L6-v2")
    return _model


def search_rag(query: str, n_results: int = 5) -> Dict[str, Any]:
    """
    Search the RAG knowledge base using semantic similarity.

    Args:
        query: Natural language search query
        n_results: Maximum number of results to return (default 5)

    Returns:
        Dictionary with:
            - documents: List of matching text chunks
            - metadatas: List of metadata dicts (filename, filepath, etc.)
            - distances: List of similarity scores (lower = more similar)
            - count: Total documents in collection

    Raises:
        ValueError: If collection doesn't exist (need to run indexer first)
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

    # Search collection using vector similarity
    search_results = client.query_points(
        collection_name=collection_name, query=query_vector, limit=n_results
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
