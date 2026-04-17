#!/usr/bin/env python3
"""
Unified RAG Indexer - Expandable architecture for indexing multiple sources.

Supports:
- Sanctum docs (markdown)
- PHP code (AST-based chunking with tree-sitter)
- Archive (session logs, old working memory)

Coming soon:
- Processes (workflow docs)
- Agent research (investigation outputs)
"""

import sys
import time
import uuid
import hashlib
import argparse
from pathlib import Path
from typing import List, Dict, Tuple
from collections import defaultdict

# Namespace for deterministic point IDs. Any fixed UUID works; we pin one
# here so IDs are stable across runs, machines, and Python versions.
_POINT_ID_NAMESPACE = uuid.UUID("6f1e8a7b-4c2d-4e0f-9b1a-0d2e3f4a5b6c")


def make_point_id(metadata: Dict, document: str) -> str:
    """
    Deterministic Qdrant point ID derived from (filepath, content).

    Keys off content hash so identical chunks dedupe across runs and
    identical chunks at different positions don't collide. Using uuid5
    (not Python's hash()) avoids PYTHONHASHSEED randomization, which was
    the source of duplicate points accumulating on every reindex.
    """
    filepath = metadata.get("filepath", "")
    content_hash = hashlib.sha256(document.encode("utf-8")).hexdigest()
    return str(uuid.uuid5(_POINT_ID_NAMESPACE, f"{filepath}::{content_hash}"))

# Import configuration
sys.path.insert(0, str(Path(__file__).parent.parent))
from config import (
    SANCTUM_PATHS,
    PHP_CODE_PATHS,
    PYTHON_CODE_PATHS,
    JS_TS_CODE_PATHS,
    PUPPET_CODE_PATHS,
    ARCHIVE_PATHS,
    JSONL_SESSION_PATHS,
    JIRA_URL,
    JIRA_EMAIL,
    JIRA_API_TOKEN,
    JIRA_PROJECTS,
    SLACK_TOKEN_FILE,
    SLACK_CHANNELS_FILE,
    SLACK_MAX_AGE_DAYS,
    SLACK_MAX_MESSAGES_PER_CHANNEL,
    SLACK_CHANNELS,
    SLITE_API_KEY,
    SLITE_ROOT_NOTE_IDS,
)

# Import collectors
from php_code_collector import collect_php_code
from python_code_collector import collect_python_code
from js_ts_code_collector import collect_js_ts_code
from archive_chunker import chunk_archive_file
from jsonl_session_chunker import collect_jsonl_sessions, chunk_jsonl_sessions
from jira_collector import collect_jira_issues
from slack_collector import collect_slack_messages
from slite_collector import collect_slite_docs
from puppet_collector import collect_puppet_code


def collect_sanctum_docs() -> List[Tuple[Path, str]]:
    """
    Collect all markdown files from the Sanctum.
    Returns list of (filepath, source_name) tuples.
    """
    files = []

    for path_str in SANCTUM_PATHS:
        sanctum_path = Path(path_str).expanduser()

        if not sanctum_path.exists():
            print(f"⚠️  Sanctum path not found: {sanctum_path}")
            continue

        for md_file in sanctum_path.rglob("*.md"):
            if ".git" not in str(md_file):
                files.append((md_file, "sanctum"))

    return files


def collect_archive_docs() -> List[Tuple[Path, str]]:
    """
    Collect archive files (session logs, old working memory).
    Returns list of (filepath, source_name) tuples.
    """
    files = []

    for path_str in ARCHIVE_PATHS:
        archive_path = Path(path_str).expanduser()

        if not archive_path.exists():
            print(f"⚠️  Archive path not found: {archive_path}")
            continue

        # Handle both files and directories
        if archive_path.is_file():
            if archive_path.suffix == ".md":
                files.append((archive_path, "archive"))
        elif archive_path.is_dir():
            for md_file in archive_path.rglob("*.md"):
                if ".git" not in str(md_file):
                    files.append((md_file, "archive"))

    return files


def load_markdown_docs(
    file_list: List[Tuple[Path, str]],
) -> Tuple[List[str], List[Dict], List[str]]:
    """
    Load markdown documents (simple: 1 file = 1 chunk).
    Returns (documents, metadatas, ids).
    """
    documents = []
    metadatas = []
    ids = []

    for i, (filepath, source) in enumerate(file_list):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()

            # Skip empty files
            if not content.strip():
                print(f"⚠️  Skipping empty file: {filepath.name}")
                continue

            documents.append(content)

            # Build metadata
            metadata = {
                "source": source,
                "doc_type": filepath.suffix.lstrip("."),
                "parent_dir": filepath.parent.name,
                "filename": filepath.name,
                "filepath": str(filepath),
            }

            # Add source-specific metadata
            if source == "archive":
                metadata["content_type"] = "session_logs"
                # For archive, relative path is relative to ~/.claude/archive/
                try:
                    archive_base = Path("~/.claude/archive").expanduser()
                    metadata["relative_path"] = str(filepath.relative_to(archive_base))
                except ValueError:
                    metadata["relative_path"] = filepath.name
            elif source == "sanctum":
                # Add relative path for sanctum
                for base_path_str in SANCTUM_PATHS:
                    base_path = Path(base_path_str).expanduser()
                    try:
                        metadata["relative_path"] = str(filepath.relative_to(base_path))
                        break
                    except ValueError:
                        continue

            if "relative_path" not in metadata:
                metadata["relative_path"] = filepath.name

            metadatas.append(metadata)
            ids.append(f"{source}_doc_{i}")

        except Exception as e:
            print(f"⚠️  Error reading {filepath}: {e}")

    return documents, metadatas, ids


def load_archive_docs(
    archive_files: List[Tuple[Path, str]],
) -> Tuple[List[str], List[Dict], List[str]]:
    """
    Load archive documents with chunking (complex: 1 file = N chunks).
    Returns (documents, metadatas, ids).
    """
    documents = []
    metadatas = []
    ids = []

    chunk_counter = 0
    for filepath, source in archive_files:
        chunks = chunk_archive_file(filepath, source)

        for chunk in chunks:
            documents.append(chunk["content"])

            # Merge chunk metadata with source metadata
            metadata = {
                "source": source,
                "doc_type": "md",
                "content_type": "session_logs",
                **chunk["metadata"],
            }

            metadatas.append(metadata)
            ids.append(f"archive_chunk_{chunk_counter}")
            chunk_counter += 1

    return documents, metadatas, ids


def load_jsonl_sessions(
    session_chunks: List[Dict],
) -> Tuple[List[str], List[Dict], List[str]]:
    """
    Load JSONL session chunks (complex: 1 file = N chunks).
    Returns (documents, metadatas, ids).
    """
    documents = []
    metadatas = []
    ids = []

    for i, chunk in enumerate(session_chunks):
        documents.append(chunk["content"])

        # Merge chunk metadata with source metadata
        metadata = {"source": "sessions", "doc_type": "jsonl", **chunk["metadata"]}

        metadatas.append(metadata)
        ids.append(f"session_chunk_{i}")

    return documents, metadatas, ids


def load_php_code(
    php_results: List[Tuple[Path, str, List[Dict]]],
) -> Tuple[List[str], List[Dict], List[str]]:
    """
    Load PHP code chunks (complex: 1 file = N chunks).
    Returns (documents, metadatas, ids).
    """
    documents = []
    metadatas = []
    ids = []

    chunk_counter = 0
    for filepath, source, chunks in php_results:
        for chunk in chunks:
            documents.append(chunk["content"])

            # Merge chunk metadata with source metadata
            metadata = {"source": source, "doc_type": "php", **chunk["metadata"]}

            metadatas.append(metadata)
            ids.append(f"php_chunk_{chunk_counter}")
            chunk_counter += 1

    return documents, metadatas, ids


def load_python_code(
    python_results: List[Tuple[Path, str, List[Dict]]],
) -> Tuple[List[str], List[Dict], List[str]]:
    """
    Load Python code chunks (complex: 1 file = N chunks).
    Returns (documents, metadatas, ids).
    """
    documents = []
    metadatas = []
    ids = []

    chunk_counter = 0
    for filepath, source, chunks in python_results:
        for chunk in chunks:
            documents.append(chunk["content"])

            # Merge chunk metadata with source metadata
            metadata = {"source": source, "doc_type": "py", **chunk["metadata"]}

            metadatas.append(metadata)
            ids.append(f"python_chunk_{chunk_counter}")
            chunk_counter += 1

    return documents, metadatas, ids


def load_js_ts_code(
    js_ts_results: List[Tuple[Path, str, List[Dict]]],
) -> Tuple[List[str], List[Dict], List[str]]:
    """
    Load JS/TS code chunks (complex: 1 file = N chunks).
    Returns (documents, metadatas, ids).
    """
    documents = []
    metadatas = []
    ids = []

    chunk_counter = 0
    for filepath, source, chunks in js_ts_results:
        for chunk in chunks:
            documents.append(chunk["content"])

            metadata = {
                "source": source,
                "doc_type": filepath.suffix.lstrip("."),
                **chunk["metadata"],
            }

            metadatas.append(metadata)
            ids.append(f"js_ts_chunk_{chunk_counter}")
            chunk_counter += 1

    return documents, metadatas, ids


def load_puppet_code(
    puppet_results: List[Tuple[Path, str, List[Dict]]],
) -> Tuple[List[str], List[Dict], List[str]]:
    """
    Load Puppet repo chunks (.pp manifests, .rb files, Hiera YAML).
    Returns (documents, metadatas, ids).
    """
    documents = []
    metadatas = []
    ids = []

    chunk_counter = 0
    for filepath, source, chunks in puppet_results:
        for chunk in chunks:
            documents.append(chunk["content"])

            metadata = {
                "source": "puppet",
                "doc_type": filepath.suffix.lstrip("."),
                **chunk["metadata"],
            }

            metadatas.append(metadata)
            ids.append(f"puppet_chunk_{chunk_counter}")
            chunk_counter += 1

    return documents, metadatas, ids


def load_jira_issues(
    jira_chunks: List[Dict],
) -> Tuple[List[str], List[Dict], List[str]]:
    """
    Load Jira issue chunks (complex: API fetch = N chunks).
    Returns (documents, metadatas, ids).
    """
    documents = []
    metadatas = []
    ids = []

    for i, chunk in enumerate(jira_chunks):
        documents.append(chunk["content"])

        # Merge chunk metadata with source metadata
        metadata = {"source": "jira", "doc_type": "jira", **chunk["metadata"]}

        metadatas.append(metadata)
        ids.append(f"jira_chunk_{i}")

    return documents, metadatas, ids


def load_slack_messages(
    slack_chunks: List[Dict],
) -> Tuple[List[str], List[Dict], List[str]]:
    """
    Load Slack message chunks (complex: API fetch = N chunks).
    Returns (documents, metadatas, ids).
    """
    documents = []
    metadatas = []
    ids = []

    for i, chunk in enumerate(slack_chunks):
        documents.append(chunk["content"])

        # Merge chunk metadata with source metadata
        metadata = {"source": "slack", "doc_type": "slack", **chunk["metadata"]}

        metadatas.append(metadata)
        ids.append(f"slack_chunk_{i}")

    return documents, metadatas, ids


def load_slite_docs(
    slite_chunks: List[Dict],
) -> Tuple[List[str], List[Dict], List[str]]:
    """
    Load Slite note chunks (complex: API fetch = N chunks).
    Returns (documents, metadatas, ids).
    """
    documents = []
    metadatas = []
    ids = []

    for i, chunk in enumerate(slite_chunks):
        documents.append(chunk["content"])

        metadata = {"source": "slite", "doc_type": "slite", **chunk["metadata"]}

        metadatas.append(metadata)
        ids.append(f"slite_chunk_{i}")

    return documents, metadatas, ids


def index_unified_collection(
    documents: List[str],
    metadatas: List[Dict],
    ids: List[str],
    sources_to_update: List[str],
):
    """
    Create or update the unified knowledge collection.
    Only updates chunks for specified sources, leaves others intact.
    """
    from qdrant_client import QdrantClient
    from qdrant_client.models import (
        Distance,
        VectorParams,
        Filter,
        FieldCondition,
        MatchAny,
        FilterSelector,
        PointStruct,
    )
    from fastembed import TextEmbedding

    # Connect to Qdrant in Docker (longer timeout for bulk delete operations)
    client = QdrantClient(url="http://localhost:6333", timeout=120)

    # Initialize embedding model (FastEmbed with ONNX for faster cold starts)
    print("Loading embedding model...")
    model = TextEmbedding(model_name="sentence-transformers/all-MiniLM-L6-v2")
    embedding_size = 384  # all-MiniLM-L6-v2 dimension

    collection_name = "unified_knowledge"

    # Check if collection exists; create it if not
    from qdrant_client.http.exceptions import UnexpectedResponse

    try:
        client.get_collection(collection_name=collection_name)
        collection_exists = True
    except UnexpectedResponse:
        collection_exists = False

    if not collection_exists:
        print(f"Creating '{collection_name}' collection...")
        client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=embedding_size, distance=Distance.COSINE),
        )
    else:
        print(f"Using existing '{collection_name}' collection")

        # Delete old chunks from sources being updated using filtered delete
        if sources_to_update:
            print(f"Removing old chunks from sources: {', '.join(sources_to_update)}")
            client.delete(
                collection_name=collection_name,
                points_selector=FilterSelector(
                    filter=Filter(
                        must=[
                            FieldCondition(
                                key="source", match=MatchAny(any=sources_to_update)
                            )
                        ]
                    )
                ),
                wait=True,
            )

    # Generate embeddings and add chunks (in batches for better performance)
    print(f"\nGenerating embeddings and indexing {len(documents)} new chunks...")
    start_time = time.time()

    BATCH_SIZE = 100  # Smaller batches for embedding generation + upload

    if len(documents) <= BATCH_SIZE:
        # Small enough for single batch
        print("  Generating embeddings...")
        # FastEmbed returns generator of numpy arrays, convert each to list
        vectors = [vec.tolist() for vec in model.embed(documents)]

        points = [
            PointStruct(
                id=make_point_id(metadata, doc),
                vector=vector,
                payload={
                    **metadata,
                    "document": doc,
                    "original_id": id_val,
                },
            )
            for id_val, vector, metadata, doc in zip(ids, vectors, metadatas, documents)
        ]

        client.upsert(collection_name=collection_name, points=points, wait=True)
    else:
        # Split into batches for progress visibility
        num_batches = (len(documents) + BATCH_SIZE - 1) // BATCH_SIZE
        print(f"Splitting into {num_batches} batches...")

        for i in range(0, len(documents), BATCH_SIZE):
            batch_end = min(i + BATCH_SIZE, len(documents))
            batch_num = (i // BATCH_SIZE) + 1
            # Show progress every 50 batches instead of every batch
            if batch_num % 50 == 0 or batch_num == num_batches:
                print(f"  Progress: {batch_num}/{num_batches} batches processed...")

            # Generate embeddings for this batch
            # FastEmbed returns generator of numpy arrays, convert each to list
            batch_vectors = [
                vec.tolist() for vec in model.embed(documents[i:batch_end])
            ]

            points = [
                PointStruct(
                    id=make_point_id(metadata, doc),
                    vector=vector,
                    payload={
                        **metadata,
                        "document": doc,
                        "original_id": id_val,
                    },
                )
                for id_val, vector, metadata, doc in zip(
                    ids[i:batch_end],
                    batch_vectors,
                    metadatas[i:batch_end],
                    documents[i:batch_end],
                )
            ]

            client.upsert(collection_name=collection_name, points=points, wait=True)

    end_time = time.time()
    index_time = end_time - start_time

    return client, index_time


def print_index_summary(metadatas: List[Dict], index_time: float):
    """
    Print summary of what was indexed.
    """
    by_source = defaultdict(int)
    by_type = defaultdict(int)
    by_chunk_type = defaultdict(int)

    for meta in metadatas:
        by_source[meta["source"]] += 1
        by_type[meta.get("doc_type", "unknown")] += 1

        # Track chunk types (PHP code, archive sessions, etc.)
        if "chunk_type" in meta:
            by_chunk_type[meta["chunk_type"]] += 1

    print("\n" + "=" * 60)
    print("INDEX SUMMARY")
    print("=" * 60)

    print(f"\nTotal chunks indexed: {len(metadatas)}")
    print(f"Indexing time: {index_time:.2f} seconds")

    print("\nBy source:")
    for source, count in sorted(by_source.items()):
        print(f"  {source}: {count} chunks")

    print("\nBy document type:")
    for doc_type, count in sorted(by_type.items()):
        print(f"  .{doc_type}: {count} chunks")

    if by_chunk_type:
        print("\nChunks by type:")
        for chunk_type, count in sorted(by_chunk_type.items()):
            print(f"  {chunk_type}: {count} chunks")

    print("\n" + "=" * 60)


def main():
    # Parse command-line arguments
    parser = argparse.ArgumentParser(
        description="Unified RAG Indexer - Index sanctum docs, code, and archives",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Index everything (default)
  python3 unified_indexer.py

  # Index only sanctum docs
  python3 unified_indexer.py --sources sanctum

  # Index sanctum and archive only
  python3 unified_indexer.py --sources sanctum archive

  # Index only PHP code
  python3 unified_indexer.py --sources code
        """,
    )
    parser.add_argument(
        "--sources",
        nargs="+",
        choices=[
            "sanctum",
            "archive",
            "code",
            "js_ts",
            "puppet",
            "sessions",
            "jira",
            "slack",
            "slite",
            "all",
        ],
        default=["all"],
        help="Which sources to index (default: all)",
    )
    args = parser.parse_args()

    # Determine which sources to index
    index_all = "all" in args.sources
    index_sanctum = index_all or "sanctum" in args.sources
    index_archive = index_all or "archive" in args.sources
    index_code = index_all or "code" in args.sources
    index_js_ts = index_all or "js_ts" in args.sources
    index_sessions = index_all or "sessions" in args.sources
    index_jira = index_all or "jira" in args.sources
    index_slack = index_all or "slack" in args.sources
    index_slite = index_all or "slite" in args.sources
    index_puppet = index_all or "puppet" in args.sources

    print("Unified RAG Indexer")
    print("=" * 60)

    if not index_all:
        sources_list = [s for s in args.sources if s != "all"]
        print(f"Indexing sources: {', '.join(sources_list)}")
        print("=" * 60)

    # Check dependencies
    try:
        import tree_sitter_languages

        print("✓ tree-sitter installed")
    except ImportError:
        print("✗ tree-sitter not installed")
        print("\nInstall with: pip3 install tree-sitter tree-sitter-languages")
        return 1

    print()

    # Collect all documents and chunks
    all_documents = []
    all_metadatas = []
    all_ids = []

    print("Collecting files...")

    # Source 1: Sanctum markdown docs
    if index_sanctum:
        sanctum_files = collect_sanctum_docs()
        print(f"  Sanctum: {len(sanctum_files)} markdown files")

        if sanctum_files:
            print("  Loading sanctum documents...")
            docs, metas, ids = load_markdown_docs(sanctum_files)
            all_documents.extend(docs)
            all_metadatas.extend(metas)
            all_ids.extend(ids)
            print(f"  ✓ Loaded {len(docs)} sanctum documents")

    # Source 2: Archive (session logs, working memory)
    if index_archive and ARCHIVE_PATHS and any(ARCHIVE_PATHS):
        archive_files = collect_archive_docs()
        if archive_files:
            print(f"  Archive: {len(archive_files)} files")
            print("  Chunking archive documents by session...")
            docs, metas, ids = load_archive_docs(archive_files)
            all_documents.extend(docs)
            all_metadatas.extend(metas)
            all_ids.extend(ids)
            print(
                f"  ✓ Extracted {len(docs)} session chunks from {len(archive_files)} files"
            )

    # Source 3: PHP code (AST-based chunking)
    if index_code and PHP_CODE_PATHS and any(PHP_CODE_PATHS):
        print("  Collecting PHP code...")
        php_results = collect_php_code(PHP_CODE_PATHS)

        if php_results:
            print("  Chunking PHP code with tree-sitter...")
            docs, metas, ids = load_php_code(php_results)
            all_documents.extend(docs)
            all_metadatas.extend(metas)
            all_ids.extend(ids)
            print(
                f"  ✓ Extracted {len(docs)} PHP code chunks from {len(php_results)} files"
            )

    # Source 3b: Python code (AST-based chunking)
    if index_code and PYTHON_CODE_PATHS and any(PYTHON_CODE_PATHS):
        print("  Collecting Python code...")
        python_results = collect_python_code(PYTHON_CODE_PATHS)

        if python_results:
            print("  Chunking Python code with tree-sitter...")
            docs, metas, ids = load_python_code(python_results)
            all_documents.extend(docs)
            all_metadatas.extend(metas)
            all_ids.extend(ids)
            print(
                f"  ✓ Extracted {len(docs)} Python code chunks from {len(python_results)} files"
            )

    # Source 3c: JS/TS code (AST-based chunking)
    if index_js_ts and JS_TS_CODE_PATHS and any(JS_TS_CODE_PATHS):
        print("  Collecting JS/TS code...")
        js_ts_results = collect_js_ts_code(JS_TS_CODE_PATHS)

        if js_ts_results:
            print("  Chunking JS/TS code with tree-sitter...")
            docs, metas, ids = load_js_ts_code(js_ts_results)
            all_documents.extend(docs)
            all_metadatas.extend(metas)
            all_ids.extend(ids)
            print(
                f"  ✓ Extracted {len(docs)} JS/TS code chunks from {len(js_ts_results)} files"
            )

    # Source 3d: Puppet repo (.pp manifests, .rb files, Hiera YAML)
    if index_puppet and PUPPET_CODE_PATHS and any(PUPPET_CODE_PATHS):
        print("  Collecting Puppet repo (manifests, Ruby, Hiera)...")
        puppet_results = collect_puppet_code(PUPPET_CODE_PATHS)

        if puppet_results:
            print("  Loading Puppet chunks...")
            docs, metas, ids = load_puppet_code(puppet_results)
            all_documents.extend(docs)
            all_metadatas.extend(metas)
            all_ids.extend(ids)
            print(
                f"  ✓ Extracted {len(docs)} puppet chunks from {len(puppet_results)} files"
            )

    # Source 4: JSONL sessions (conversation archives)
    if index_sessions and JSONL_SESSION_PATHS and any(JSONL_SESSION_PATHS):
        print("  Collecting JSONL sessions (all archived files, no age limit)...")
        session_files = collect_jsonl_sessions(max_age_days=None, max_files=None)

        if session_files:
            print(f"  Found {len(session_files)} recent session files")
            print("  Chunking sessions by conversation exchange...")
            session_chunks = chunk_jsonl_sessions(session_files)

            if session_chunks:
                docs, metas, ids = load_jsonl_sessions(session_chunks)
                all_documents.extend(docs)
                all_metadatas.extend(metas)
                all_ids.extend(ids)
                print(
                    f"  ✓ Extracted {len(docs)} conversation chunks from {len(session_files)} files"
                )
            else:
                print("  ⚠️  No conversation chunks extracted from sessions")

    # Source 5: Jira (ticket history via REST API)
    if index_jira and JIRA_URL and JIRA_EMAIL and JIRA_API_TOKEN and JIRA_PROJECTS:
        print("  Collecting Jira issues...")
        jira_chunks = []

        for project_config in JIRA_PROJECTS:
            project = project_config["project"]
            max_issues = project_config.get("max_issues", 100)

            chunks = collect_jira_issues(
                jira_url=JIRA_URL,
                email=JIRA_EMAIL,
                api_token=JIRA_API_TOKEN,
                project=project,
                max_issues=max_issues,
            )
            jira_chunks.extend(chunks)

        if jira_chunks:
            docs, metas, ids = load_jira_issues(jira_chunks)
            all_documents.extend(docs)
            all_metadatas.extend(metas)
            all_ids.extend(ids)
            print(
                f"  ✓ Collected {len(docs)} Jira issues from {len(JIRA_PROJECTS)} projects"
            )
        else:
            print("  ⚠️  No Jira issues collected")

    # Source 6: Slack (team conversations via API)
    if index_slack and SLACK_TOKEN_FILE and SLACK_CHANNELS_FILE and SLACK_CHANNELS:
        print("  Collecting Slack messages...")
        slack_chunks = collect_slack_messages(
            channel_names=SLACK_CHANNELS,
            token_file=SLACK_TOKEN_FILE,
            channels_file=SLACK_CHANNELS_FILE,
            max_age_days=SLACK_MAX_AGE_DAYS,
            max_messages_per_channel=SLACK_MAX_MESSAGES_PER_CHANNEL,
        )

        if slack_chunks:
            docs, metas, ids = load_slack_messages(slack_chunks)
            all_documents.extend(docs)
            all_metadatas.extend(metas)
            all_ids.extend(ids)
            print(
                f"  ✓ Collected {len(docs)} Slack message chunks from {len(SLACK_CHANNELS)} channels"
            )
        else:
            print("  ⚠️  No Slack messages collected")

    # Source 7: Slite (team knowledge via REST API)
    if index_slite and SLITE_API_KEY and SLITE_ROOT_NOTE_IDS:
        print("  Collecting Slite notes...")
        slite_chunks = collect_slite_docs(
            api_key=SLITE_API_KEY,
            root_note_ids=SLITE_ROOT_NOTE_IDS,
        )

        if slite_chunks:
            docs, metas, ids = load_slite_docs(slite_chunks)
            all_documents.extend(docs)
            all_metadatas.extend(metas)
            all_ids.extend(ids)
            print(f"  ✓ Collected {len(docs)} Slite notes")
        else:
            print("  ⚠️  No Slite notes collected")

    # Future sources:
    # process_files = collect_process_docs()
    # agent_research_files = collect_agent_research_docs()

    if not all_documents:
        print("\n⚠️  No documents found to index!")
        print("\nCheck configuration in config.py")
        return 1

    print(f"\nTotal chunks to index: {len(all_documents)}")

    # Determine which sources we're updating
    sources_being_updated = []
    if index_sanctum:
        sources_being_updated.append("sanctum")
    if index_archive:
        sources_being_updated.append("archive")
    if index_code:
        sources_being_updated.append("code")
    if index_js_ts:
        sources_being_updated.append("js_ts")
    if index_sessions:
        sources_being_updated.append("sessions")
    if index_jira:
        sources_being_updated.append("jira")
    if index_slack:
        sources_being_updated.append("slack")
    if index_slite:
        sources_being_updated.append("slite")
    if index_puppet:
        sources_being_updated.append("puppet")

    # Index
    client, index_time = index_unified_collection(
        all_documents, all_metadatas, all_ids, sources_being_updated
    )
    count_result = client.count(collection_name="unified_knowledge", exact=True)
    print(f"✓ Collection now contains {count_result.count} total chunks")

    # Summary
    print_index_summary(all_metadatas, index_time)

    print("\nCollection 'unified_knowledge' ready for querying")

    return 0


if __name__ == "__main__":
    sys.exit(main())
