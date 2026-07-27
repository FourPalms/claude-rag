# RAG System - Unified Knowledge Search

A production-ready RAG (Retrieval-Augmented Generation) system for hybrid semantic + keyword search across team knowledge, code, session archives, Jira tickets, Slack conversations, and Slite docs — integrated as a native Claude Code tool via MCP.

**Status:** Production-ready

---

## Quick Start

**Prerequisites:** Python 3.10+, Docker, Claude Code CLI

### 1. Clone and install

```bash
git clone https://github.com/FourPalms/claude-rag.git
cd claude-rag
make install
```

### 2. Start the vector database

```bash
make db
```

### 3. Configure your sources

Open `config.py` and point the path variables at your content. Only configure what you have — everything is optional except at least one source:

```python
# Markdown docs (team wikis, notes, architecture docs, etc.)
DOCS_PATHS = ["~/path/to/your/docs"]

# Claude Code session logs
JSONL_SESSION_PATHS = ["~/.claude/projects"]

# Code repos — add any combination you want indexed
PHP_CODE_PATHS    = ["~/repos/your-php-app"]
PYTHON_CODE_PATHS = ["~/repos/your-python-app"]
JS_TS_CODE_PATHS  = ["~/repos/your-frontend"]

# Puppet infrastructure (optional)
PUPPET_CODE_PATHS = ["~/repos/puppet/site", "~/repos/puppet/hieradata"]

# Jira (optional) — set your org URL, email, and API token
JIRA_URL       = "https://yourorg.atlassian.net"
JIRA_EMAIL     = "you@yourorg.com"
JIRA_API_TOKEN = "your-api-token"
JIRA_PROJECTS  = [{"project": "PROJ", "max_issues": 300}]

# Slack (optional) — token/channel files plus per-channel ingestion config.
# Each channel maps to a dict of overrides ({} = use global defaults).
SLACK_TOKEN_FILE    = "~/.claude/skills/slack-tools/config/tokens.json"
SLACK_CHANNELS_FILE = "~/.claude/skills/slack-tools/config/channels.json"
SLACK_MAX_AGE_DAYS  = 30
SLACK_CHANNELS = {
    "engineering-team": {},
    "deep-history-channel": {"max_age_days": 180},  # per-channel override
}

# Slite (optional) — team knowledge base via REST API
SLITE_API_KEY       = os.getenv("SLITE_API_KEY")
SLITE_ROOT_NOTE_IDS = ["your-root-note-id"]
```

Slack can be ingested either through the Slack API directly or through the
`slack_mcp_bridge.py` bridge to Claude Code's official Slack MCP connector — see
`config.example.py` and `HOW_TO_USE.md` for the token setup.

### 4. Index your content

```bash
make index      # index all configured sources
make stats      # verify what was indexed
```

To re-index a single source without rebuilding everything:

```bash
python3 scripts/unified_indexer.py --sources jira
python3 scripts/unified_indexer.py --sources code sessions
# Available: sanctum archive code js_ts puppet sessions jira slack slite
```

Code repos are synced to their primary branch before indexing by default; pass
`--no-sync` to index the working tree as-is (used by the nightly job so a dirty
or off-branch checkout never blocks the run).

### 5. Register with Claude Code

```bash
claude mcp add --transport stdio --scope user rag-system \
  python3 /absolute/path/to/claude-rag/mcp_server.py
```

Restart Claude Code. The `search_knowledge` tool is now available — Claude will call it automatically when your questions touch indexed content.

```python
# Tool signature
search_knowledge(
    query: str,                 # Natural language search query
    limit: int = 5,             # Max results (1–20)
    source: str | None = None,  # Restrict to one source (e.g. "code", "jira", "slack")
    doc_type: str | None = None,   # Restrict to a file/content type (e.g. "php", "md")
    chunk_type: str | None = None, # Restrict to a chunk kind (e.g. "method", "description")
    project: str | None = None,    # Restrict to a Jira project
    issue_key: str | None = None,  # Restrict to a single Jira issue
) -> str
```

---

## What This Is

A unified RAG system that indexes and semantically searches:
- **Team knowledge** (markdown documentation)
- **Code** (PHP, Python, and JS/TS repos with AST-based chunking)
- **Puppet infrastructure** (`.pp` manifests, `.rb` Ruby, Hiera YAML)
- **Archives** (session logs chunked by session)
- **Conversations** (JSONL session logs from a configurable lookback window)
- **Jira tickets** (one or more projects via REST API)
- **Slack messages** (one or more channels via the Slack API or MCP bridge)
- **Slite docs** (team knowledge base via REST API)

**Key features:**
- One unified collection — query anything, get answers from anywhere
- Hybrid retrieval — dense embeddings + BM25 sparse vectors fused server-side with Reciprocal Rank Fusion, so exact identifiers and semantic meaning both count
- Metadata-filtered search (restrict by source, doc type, chunk type, Jira project/issue)
- ~60–500ms query speed
- Selective incremental indexing (update one source without rebuilding everything)
- Rich metadata per chunk (source, type, class, function, session number, Jira issue, Slack channel)
- Thread-aware Slack indexing (full conversation context preserved)

---

## Architecture

### Vector Database
- **Qdrant** running in Docker (`http://localhost:6333`)
- **Dense embedding model:** `all-MiniLM-L6-v2` (general-purpose, works for text and code)
- **Sparse model:** BM25 (via `fastembed`) for keyword/identifier matching
- **Storage:** One unified collection (`unified_knowledge`) holding both dense and sparse vectors, with metadata filtering
- **Migrated from ChromaDB:** Qdrant handles large-scale indexing better (50k+ chunks) and natively supports hybrid vectors

### Retrieval
Every query runs against both the dense (semantic) and sparse (BM25 keyword) vectors,
and Qdrant fuses the two result sets server-side with Reciprocal Rank Fusion (RRF).
This means an exact identifier like `refreshToken` and a fuzzy concept like
"how do we renew auth tokens" both surface the right chunks. Optional metadata
filters (source, doc type, chunk type, Jira project/issue) narrow the search space
before ranking.

### Chunking Strategies

**Markdown documents:**
- Flat docs: 1 file = 1 chunk
- Session archives: 1 file = N chunks (split by session headers)

**PHP/Python/JS/TS code (AST-based with tree-sitter):**
- Parse code into Abstract Syntax Tree
- Extract semantic boundaries: classes, methods, functions
- Each class/method/function = 1 searchable chunk
- Includes docblocks and preserves context

**Puppet manifests + Ruby (regex + tree-sitter):**
- `.pp` files: regex-based extraction of `class`, `define`, and `function` blocks (no tree-sitter-puppet grammar available; regex is robust for Puppet DSL)
- `.rb` files: tree-sitter-ruby AST for modules, classes, methods
- `.yaml`/`.yml`: whole-file Hiera data chunks

**JSONL sessions:**
- 1 conversation exchange (user + assistant) = 1 chunk
- Configurable lookback window (e.g., last 60 days)
- Preserves conversation context and timestamps

**Jira tickets:**
- Each ticket split into up to 3 chunk types:
  - Title chunk (metadata only)
  - Description chunk (full description)
  - Comment chunks (one chunk per comment, all comments indexed)
- Preserves issue key, status, assignee, dates

**Slack messages:**
- Thread-based chunking for optimal context:
  - **Threads:** Parent message + all replies = 1 chunk
  - **Standalone messages:** Each message = 1 chunk
- Preserves channel, author, timestamp, thread context

**Metadata examples:**

```python
# Markdown document
{
  "source": "docs",
  "doc_type": "md",
  "filename": "architecture.md",
  "filepath": "/full/path",
  "relative_path": "team/architecture.md"
}

# Archive session
{
  "source": "archive",
  "doc_type": "md",
  "chunk_type": "session",
  "session_number": 42,
  "session_date": "2025-12-30",
  "archive_type": "working_memory"
}

# PHP code
{
  "source": "code",
  "doc_type": "php",
  "chunk_type": "method",
  "class_name": "PaymentService",
  "function_name": "processRefund",
  "namespace": "App\\Services\\Billing",
  "start_line": 42,
  "end_line": 87,
  "has_docblock": true
}

# Jira ticket
{
  "source": "jira",
  "doc_type": "jira",
  "chunk_type": "description",
  "issue_key": "PROJ-123",
  "summary": "Implement OAuth flow",
  "status": "In Progress",
  "assignee": "Jane Smith",
  "created": "2026-04-01",
  "updated": "2026-04-07",
  "project": "PROJ"
}

# Slack message
{
  "source": "slack",
  "doc_type": "slack",
  "chunk_type": "thread",
  "channel_name": "engineering-team",
  "channel_id": "C123456",
  "message_ts": "1775236533.550409",
  "author_username": "jsmith",
  "author_display": "Jane Smith",
  "date": "2026-04-03",
  "timestamp": "2026-04-03 13:15:33",
  "is_thread": true,
  "reply_count": 5
}
```

---

## File Structure

```
~/.claude/rag-system/
├── README.md                      # This file
├── HOW_TO_USE.md                  # Detailed usage guide
├── config.py                      # Configure paths to index
├── search.py                      # Core search function (used by MCP + scripts)
├── mcp_server.py                  # FastMCP server (Claude Code integration)
├── docker-compose.yml             # Qdrant container configuration
├── .gitignore                     # Exclude database and temp files
├── tests/
│   ├── test_parsers.py            # pytest suite (all parsers/collectors covered)
│   ├── test_slack_collector_overrides.py  # per-channel Slack override tests
│   └── test_slack_mcp_reshape.py  # Slack MCP bridge reshaping tests
├── scripts/
│   ├── unified_indexer.py         # Main indexer (selective updates, git sync)
│   ├── git_sync.py                # Sync code repos to primary branch before indexing
│   ├── php_code_collector.py      # Tree-sitter AST-based PHP chunking
│   ├── python_code_collector.py   # Tree-sitter AST-based Python chunking
│   ├── js_ts_code_collector.py    # Tree-sitter AST-based JS/TS chunking
│   ├── puppet_collector.py        # Puppet .pp + Ruby .rb + Hiera YAML chunking
│   ├── archive_chunker.py         # Session-based archive chunking
│   ├── jsonl_session_chunker.py   # JSONL conversation log chunking
│   ├── jira_collector.py          # Jira REST API fetcher
│   ├── slack_collector.py         # Slack API fetcher (thread-aware)
│   ├── slack_mcp_bridge.py        # Bridge to Claude Code's official Slack MCP connector
│   ├── slite_collector.py         # Slite REST API fetcher
│   ├── index_sanctum.py           # Sanctum knowledge-base indexer
│   ├── matrix_display.py          # Matrix-rain live indexer display
│   ├── query.py                   # Semantic search CLI interface
│   ├── list_docs.py               # Show indexed chunks
│   └── show_stats.py              # Display collection statistics
├── qdrant_storage/                # Vector database (Docker volume)
└── results/                       # Test outputs (gitignored)
```

---

## Testing

```bash
# Run parser test suite
make test

# Run with verbose output
make test-verbose

# Lint with Black
make lint
make lint-fix     # auto-format
```

The `tests/test_parsers.py` suite covers all collectors:
- Tests across Puppet (`.pp`), Ruby (`.rb`), Hiera YAML, PHP, Python, JS/TS, and archive chunkers
- Tests semantic chunk extraction, fallback behavior, metadata fields, directory skipping

---

## Configuration

Edit `config.py` to configure your sources:

```python
# Example config.py structure
DOCS_PATHS = ["~/path/to/your/docs"]
ARCHIVE_PATHS = ["~/.claude/archive/working-memory.md"]
PHP_CODE_PATHS = ["~/repos/your-php-app"]
PYTHON_CODE_PATHS = ["~/repos/your-python-app"]
PUPPET_CODE_PATHS = ["~/repos/puppet/site", "~/repos/puppet/hieradata"]
JSONL_SESSION_PATHS = ["~/.claude/projects"]   # Claude Code session logs
JIRA_PROJECTS = [{"project": "PROJ", "max_issues": 300}]  # One or more Jira projects
SLACK_CHANNELS = {"your-channel": {}}          # channel -> per-channel override dict
SLITE_ROOT_NOTE_IDS = ["your-root-note-id"]    # Slite docs to index
JSONL_LOOKBACK_DAYS = 60                        # How many days of sessions to index
```

**Query performance:** ~60–500ms per query  
**Storage:** Qdrant Docker container (`qdrant_storage/`)

---

## Next Steps

**Done recently:**
- ✅ Hybrid dense + BM25 retrieval with server-side RRF fusion
- ✅ Metadata filtering in the query tool (source / doc_type / chunk_type / project / issue)
- ✅ Slack MCP bridge + per-channel `max_age_days` overrides
- ✅ Slite ingestion; git sync to primary branch before indexing

**Future enhancements:**
- Query history and analytics (track what's being searched)
- Temporal scoping (e.g., "last 30 days only")
- Reranking layer for improved result quality
- Support for additional languages via tree-sitter

---

## Dependencies

- Python 3.10+
- `qdrant-client` — Vector database client
- `fastembed` — Fast embedding generation
- `tree-sitter` — AST parsing
- `tree-sitter-languages` — Language grammars (PHP, Python, Ruby, etc.)
- `mcp[cli]` — FastMCP framework (for Claude Code integration)
- `requests` — HTTP client (Jira and Slack APIs)
- `black` (dev) — code formatter
- `pytest` (dev) — test runner
- Docker (for Qdrant)

---

## Research References

**ChromaDB/Qdrant best practices:**
- Single collection + metadata filtering > multiple collections for shared semantic space
- Metadata should be atomic fields, not JSON blobs
- 10–20% chunk overlap recommended for text

**Code indexing best practices:**
- AST-based chunking > line-based chunking for code
- Tree-sitter is the production standard (used by major editors)
- General-purpose embeddings work for code + text (no need for code-specific models for most use cases)

**RAG chunking strategies:**
- Recursive chunking at ~512 tokens scores well in benchmarks
- Semantic chunking can underperform when fragments are too small to preserve context
- For code: preserve semantic boundaries (classes, functions) over fixed token counts
- For conversations: preserve thread/exchange context over arbitrary splits
