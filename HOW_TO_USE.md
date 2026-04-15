# RAG System - How to Use

## Architecture

**One unified collection** with rich metadata. Query anything, get answers from anywhere (docs + code).

## Components

### 1. Configuration (`config.py`)

Copy `config.example.py` to `config.py` and edit to specify what to index:

```python
# Documentation
SANCTUM_PATHS = [
    "~/.claude/sanctum"
]

# PHP code - ADD YOUR DIRECTORIES HERE
PHP_CODE_PATHS = [
    "~/repos/my-project/app/Services/Payment",
    "~/repos/my-project/app/Controller",
    # Add more paths...
]
```

**How it works:**
- Each path is indexed recursively (all subdirectories)
- PHP files are parsed with tree-sitter and chunked at class/function boundaries
- Each chunk gets rich metadata (file, class, function, line numbers)

### 2. Indexer (`scripts/unified_indexer.py`)

Run this to index everything:

```bash
cd ~/.claude/rag-system
python3 scripts/unified_indexer.py
```

**When to re-index:**
- After adding new paths to `config.py`
- After significant changes to sanctum docs or code
- Re-indexing is fast (~3-5 seconds)

### 3. Query (`scripts/query.py`)

Ask questions:

```bash
# With question as argument
python3 ~/.claude/rag-system/scripts/query.py "How does OAuth work?"

# Interactive (prompts for question)
python3 ~/.claude/rag-system/scripts/query.py
```

**You don't need to know:**
- Which collection to use (automatic)
- Where the answer lives (searches everything)
- Document types or sources (metadata handled automatically)

**Just ask and get answers.**

### 4. List Documents (`scripts/list_docs.py`)

See what's indexed:

```bash
python3 ~/.claude/rag-system/scripts/list_docs.py
```

Shows all indexed chunks grouped by source and directory.

## Adding PHP Code

1. **Edit `config.py`** - add directory paths to `PHP_CODE_PATHS`
2. **Run indexer** - `python3 scripts/unified_indexer.py`
3. **Query** - ask questions about your code

Example:

```python
PHP_CODE_PATHS = [
    "~/repos/my-project/app/Services/Payment",
]
```

This will:
- Find all `.php` files recursively in that directory
- Parse with tree-sitter into Abstract Syntax Tree
- Extract classes, methods, functions as separate chunks
- Tag each chunk with: class name, function name, line numbers, docblock
- Index everything into the unified collection

## PHP Chunking Strategy

**AST-based semantic chunking** (not line-based):
- Each class = 1 chunk (with docblock)
- Each method = 1 chunk (with docblock)
- Each function = 1 chunk (with docblock)

**Rich metadata per chunk:**
```python
{
    "source": "code",
    "doc_type": "php",
    "chunk_type": "method",  # or "class", "function"
    "class_name": "PaymentProcessor",
    "function_name": "processPayment",
    "namespace": "App\\Services\\Payment",
    "start_line": 42,
    "end_line": 87,
    "filepath": "/full/path/to/file.php",
    "has_docblock": true
}
```

## Query Examples

Once PHP code is indexed:

```bash
# Find specific functions
python3 scripts/query.py "PaymentProcessor processPayment method"

# Understand architecture
python3 scripts/query.py "How does the payment service process transactions?"

# Find related code
python3 scripts/query.py "Which classes handle workflow orchestration?"

# Mix docs and code
python3 scripts/query.py "OAuth implementation in controller"
```

The system semantically understands your question and returns relevant code chunks or docs.

## Future Sources (Coming Soon)

- **Archive**: Old session logs, working memory
- **Processes**: Workflow documentation
- **Agent Research**: Research outputs

All will live in the same unified collection with proper metadata.

## Technical Details

- **Embedding Model**: all-MiniLM-L6-v2 (general-purpose, works for both text and code)
- **Vector Database**: Qdrant (Docker, `http://localhost:6333`)
- **PHP/Python/Ruby Parser**: tree-sitter (battle-tested, used by Neovim/Helix/Zed)
- **Puppet Parser**: regex-based brace-depth tracking (tree-sitter-puppet not available)
- **Query Speed**: ~60-500ms per query
- **Storage**: `~/.claude/rag-system/qdrant_db/` (Docker volume)

## Troubleshooting

**Error: Collection not found**
- Run `python3 scripts/unified_indexer.py` first

**Error: No files found**
- Check paths in `config.py`
- Ensure paths exist and contain .md or .php files

**PHP parsing errors**
- tree-sitter skips unparseable files automatically
- Check warnings during indexing
