#!/usr/bin/env python3
"""
DEPRECATED — legacy ChromaDB sanctum indexer (superseded by unified_indexer.py).

This script used ChromaDB and is no longer maintained.
Use: python3 unified_indexer.py --sources sanctum
"""

import sys
import time
from pathlib import Path

def main():
    import chromadb

    sanctum_path = Path("~/.claude/sanctum").expanduser()

    if not sanctum_path.exists():
        print(f"Error: Sanctum not found at {sanctum_path}")
        return 1

    print("Collecting markdown files from sanctum...")
    markdown_files = []
    for md_file in sanctum_path.rglob("*.md"):
        if ".git" not in str(md_file):
            markdown_files.append(md_file)

    print(f"Found {len(markdown_files)} markdown files")

    # Load all documents
    print("\nLoading documents...")
    documents = []
    metadatas = []
    ids = []

    for i, filepath in enumerate(markdown_files):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()

            documents.append(content)
            metadatas.append({
                "filepath": str(filepath),
                "filename": filepath.name,
                "parent_dir": filepath.parent.name
            })
            ids.append(f"sanctum_doc_{i}")

        except Exception as e:
            print(f"⚠️  Error reading {filepath}: {e}")

    print(f"Loaded {len(documents)} documents")

    # Connect to persistent database
    db_path = Path("~/.claude/rag-system/chroma_db").expanduser()
    client = chromadb.PersistentClient(path=str(db_path))

    # Create or recreate the sanctum collection
    collection_name = "sanctum_docs"

    try:
        client.delete_collection(name=collection_name)
        print(f"\nDeleted existing '{collection_name}' collection")
    except:
        pass

    print(f"Creating '{collection_name}' collection...")
    collection = client.create_collection(
        name=collection_name,
        metadata={"description": "Team knowledge base documentation"}
    )

    # Index documents
    print("\nIndexing documents...")
    start_time = time.time()

    collection.add(
        documents=documents,
        metadatas=metadatas,
        ids=ids
    )

    end_time = time.time()
    index_time = end_time - start_time

    print(f"✓ Indexed {collection.count()} documents in {index_time:.2f} seconds")
    print(f"\nCollection '{collection_name}' ready for querying")

    return 0

if __name__ == "__main__":
    sys.exit(main())
