.PHONY: test test-verbose lint lint-fix index index-all stats install db help

PYTHON := python3
SCRIPTS := scripts
TESTS   := tests

help:
	@echo "RAG System — available targets:"
	@echo ""
	@echo "  make install        Install Python dependencies"
	@echo "  make db             Start Qdrant vector database (Docker)"
	@echo ""
	@echo "  make index          Index all configured sources"
	@echo "  make stats          Show database statistics"
	@echo ""
	@echo "  Selective indexing: python3 scripts/unified_indexer.py --sources <name>"
	@echo "  Source names: sanctum archive code js_ts puppet sessions jira slack"
	@echo ""
	@echo "  make test           Run parser test suite (pytest)"
	@echo "  make test-verbose   Run tests with verbose output"
	@echo "  make lint           Check formatting with Black (no changes)"
	@echo "  make lint-fix       Auto-format with Black"
	@echo ""

# ── Setup ─────────────────────────────────────────────────────────────────────

install:
	pip3 install qdrant-client fastembed tree-sitter tree-sitter-languages "mcp[cli]" requests black pytest

db:
	docker-compose up -d

# ── Testing ───────────────────────────────────────────────────────────────────

test:
	$(PYTHON) -m pytest $(TESTS)/test_parsers.py -q

test-verbose:
	$(PYTHON) -m pytest $(TESTS)/test_parsers.py -v

# ── Linting ───────────────────────────────────────────────────────────────────

# Files to lint: all Python source (not legacy deprecated scripts)
LINT_FILES := \
	config.py \
	search.py \
	mcp_server.py \
	$(SCRIPTS)/unified_indexer.py \
	$(SCRIPTS)/puppet_collector.py \
	$(SCRIPTS)/php_code_collector.py \
	$(SCRIPTS)/python_code_collector.py \
	$(SCRIPTS)/js_ts_code_collector.py \
	$(SCRIPTS)/archive_chunker.py \
	$(SCRIPTS)/jsonl_session_chunker.py \
	$(SCRIPTS)/jira_collector.py \
	$(SCRIPTS)/slack_collector.py \
	$(SCRIPTS)/query.py \
	$(SCRIPTS)/list_docs.py \
	$(SCRIPTS)/show_stats.py \
	$(TESTS)/test_parsers.py

lint:
	$(PYTHON) -m black --check $(LINT_FILES)

lint-fix:
	$(PYTHON) -m black $(LINT_FILES)

# ── Indexing ──────────────────────────────────────────────────────────────────

index:
	$(PYTHON) $(SCRIPTS)/unified_indexer.py

index-all: index

# ── Stats ─────────────────────────────────────────────────────────────────────

stats:
	$(PYTHON) $(SCRIPTS)/show_stats.py
