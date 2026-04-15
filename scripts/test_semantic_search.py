#!/usr/bin/env python3
"""
DEPRECATED — original ChromaDB proof-of-concept (Phase 1, April 2026).

Kept for historical reference only. System now uses Qdrant (unified_indexer.py).
"""

import os
import sys
from pathlib import Path
import json

def check_dependencies():
    """Check if required packages are installed."""
    try:
        import chromadb
        print("✓ chromadb installed")
        return True
    except ImportError:
        print("✗ chromadb not installed")
        print("\nInstall with: pip3 install chromadb")
        return False

def collect_markdown_files(base_paths):
    """Collect markdown files from specified directories."""
    markdown_files = []

    for base_path in base_paths:
        base = Path(base_path).expanduser()
        if not base.exists():
            print(f"⚠️  Path does not exist: {base}")
            continue

        for md_file in base.rglob("*.md"):
            if ".git" not in str(md_file):
                markdown_files.append(md_file)

    return markdown_files

def load_documents(file_paths, max_files=50):
    """Load content from markdown files."""
    documents = []
    metadatas = []
    ids = []

    for i, filepath in enumerate(file_paths[:max_files]):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()

            documents.append(content)
            metadatas.append({
                "filepath": str(filepath),
                "filename": filepath.name
            })
            ids.append(f"doc_{i}")

        except Exception as e:
            print(f"⚠️  Error reading {filepath}: {e}")

    return documents, metadatas, ids

def create_collection_and_index(documents, metadatas, ids):
    """Create ChromaDB collection and index documents."""
    import chromadb
    from pathlib import Path

    # Create client with persistent storage
    db_path = Path("~/.claude/rag-system/chroma_db").expanduser()
    client = chromadb.PersistentClient(path=str(db_path))

    # Try to get existing collection, or create new one
    collection_exists = False
    try:
        collection = client.get_collection(name="team_docs_test")
        print(f"✓ Using existing collection with {collection.count()} documents")
        collection_exists = True
    except:
        collection = client.create_collection(
            name="team_docs_test",
            metadata={"description": "RAG V1 proof of concept"}
        )
        print("✓ Created new collection")

    # Only add documents if this is a new collection
    if not collection_exists:
        collection.add(
            documents=documents,
            metadatas=metadatas,
            ids=ids
        )
        print(f"✓ Indexed {len(documents)} documents")

    return collection

def semantic_search(collection, query, n_results=3):
    """Perform semantic search."""
    results = collection.query(
        query_texts=[query],
        n_results=n_results
    )

    return results

def keyword_search(file_paths, query_terms, max_files=50):
    """Simple keyword search for comparison."""
    results = []

    for filepath in file_paths[:max_files]:
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read().lower()

            # Check if any query term appears
            matches = sum(1 for term in query_terms if term.lower() in content)
            if matches > 0:
                results.append({
                    "filepath": str(filepath),
                    "matches": matches
                })
        except Exception as e:
            pass

    # Sort by number of matches
    results.sort(key=lambda x: x["matches"], reverse=True)
    return results[:3]

def run_test_queries(collection, file_paths):
    """Run test queries and compare semantic vs keyword search."""

    test_queries = [
        {
            "name": "Architecture",
            "query": "How does OAuth token authentication work in our system?",
            "keywords": ["oauth", "token", "authentication"]
        },
        {
            "name": "Process",
            "query": "What is the workflow for reviewing pull requests?",
            "keywords": ["pull request", "review", "pr"]
        },
        {
            "name": "Technical",
            "query": "How do we handle Temporal workflow errors and retries?",
            "keywords": ["temporal", "workflow", "error", "retry"]
        }
    ]

    results = []

    for test in test_queries:
        print(f"\n{'='*60}")
        print(f"Query Type: {test['name']}")
        print(f"Query: {test['query']}")
        print(f"{'='*60}")

        # Semantic search
        print("\n--- SEMANTIC SEARCH RESULTS ---")
        semantic_results = semantic_search(collection, test['query'])

        for i, (doc, metadata, distance) in enumerate(zip(
            semantic_results['documents'][0],
            semantic_results['metadatas'][0],
            semantic_results['distances'][0]
        )):
            print(f"\n{i+1}. {metadata['filename']}")
            print(f"   Path: {metadata['filepath']}")
            print(f"   Distance: {distance:.4f}")
            print(f"   Preview: {doc[:200]}...")

        # Keyword search
        print("\n--- KEYWORD SEARCH RESULTS ---")
        keyword_results = keyword_search(file_paths, test['keywords'])

        for i, result in enumerate(keyword_results):
            filepath = Path(result['filepath'])
            print(f"\n{i+1}. {filepath.name}")
            print(f"   Path: {result['filepath']}")
            print(f"   Keyword matches: {result['matches']}")

        results.append({
            "query_type": test['name'],
            "query": test['query'],
            "semantic_results": [m['filename'] for m in semantic_results['metadatas'][0]],
            "keyword_results": [Path(r['filepath']).name for r in keyword_results]
        })

    return results

def main():
    """Main execution."""
    print("RAG V1 Step One: Semantic Search Proof of Concept")
    print("="*60)

    # Check dependencies
    if not check_dependencies():
        return 1

    # Define paths to index
    base_paths = [
        "~/.claude/sanctum",
        "~/.claude/processes",
        "~/.claude/agent-research"
    ]

    print("\nCollecting markdown files...")
    file_paths = collect_markdown_files(base_paths)
    print(f"Found {len(file_paths)} markdown files")

    if len(file_paths) == 0:
        print("No markdown files found!")
        return 1

    # Load documents (limit to first 50 for this test)
    print("\nLoading documents (max 50 for proof of concept)...")
    documents, metadatas, ids = load_documents(file_paths, max_files=50)
    print(f"Loaded {len(documents)} documents")

    # Create collection and index
    print("\nConnecting to ChromaDB...")
    collection = create_collection_and_index(documents, metadatas, ids)
    print(f"✓ Collection ready with {collection.count()} documents")

    # Run test queries
    print("\nRunning test queries...")
    results = run_test_queries(collection, file_paths)

    # Save results
    results_file = Path("~/.claude/rag-system/results/step_one_results.json").expanduser()
    with open(results_file, 'w') as f:
        json.dump(results, f, indent=2)

    print(f"\n{'='*60}")
    print(f"Results saved to: {results_file}")
    print(f"{'='*60}")

    return 0

if __name__ == "__main__":
    sys.exit(main())
