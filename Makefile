.PHONY: test test-verbose lint lint-fix index index-all index-matrix reindex-clean reindex-bg drop matrix-demo stats install db help

PYTHON := python3
SCRIPTS := scripts
TESTS   := tests

# Cap ONNX / OpenMP / MKL thread pools during indexing so the embedding
# pipeline doesn't try to grab every core on the machine. 2 threads is plenty
# for MiniLM-L6 on CPU and leaves the rest of the system responsive (typing,
# browser, other apps) while a full reindex is running.
INDEX_ENV := OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 TOKENIZERS_PARALLELISM=false

help:
	@echo "RAG System — available targets:"
	@echo ""
	@echo "  make install        Install Python dependencies"
	@echo "  make db             Start Qdrant vector database (Docker)"
	@echo ""
	@echo "  make index          Index all configured sources"
	@echo "  make index-matrix   Index all sources with Matrix-rain live display"
	@echo "  make reindex-clean  Drop + rebuild from scratch with Matrix-rain display (for devs)"
	@echo "  make reindex-bg     Drop + rebuild, quietly logging to logs/reindex-latest.log"
	@echo "  make drop           Drop the unified_knowledge collection (destructive)"
	@echo "  make matrix-demo    Preview the Matrix-rain display (no real indexing)"
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
	$(PYTHON) -m pytest $(TESTS) -q

test-verbose:
	$(PYTHON) -m pytest $(TESTS) -v

# ── Linting ───────────────────────────────────────────────────────────────────

# Files to lint: all Python source (not legacy deprecated scripts)
LINT_FILES := \
	config.py \
	search.py \
	mcp_server.py \
	$(SCRIPTS)/unified_indexer.py \
	$(SCRIPTS)/matrix_display.py \
	$(SCRIPTS)/git_sync.py \
	$(SCRIPTS)/puppet_collector.py \
	$(SCRIPTS)/php_code_collector.py \
	$(SCRIPTS)/python_code_collector.py \
	$(SCRIPTS)/js_ts_code_collector.py \
	$(SCRIPTS)/archive_chunker.py \
	$(SCRIPTS)/jsonl_session_chunker.py \
	$(SCRIPTS)/jira_collector.py \
	$(SCRIPTS)/slack_collector.py \
	$(SCRIPTS)/slite_collector.py \
	$(SCRIPTS)/query.py \
	$(SCRIPTS)/list_docs.py \
	$(SCRIPTS)/show_stats.py \
	$(TESTS)/test_parsers.py \
	$(TESTS)/test_slack_collector_overrides.py \
	$(TESTS)/test_slite_retry.py

lint:
	$(PYTHON) -m black --check $(LINT_FILES)

lint-fix:
	$(PYTHON) -m black $(LINT_FILES)

# ── Indexing ──────────────────────────────────────────────────────────────────

index:
	$(INDEX_ENV) $(PYTHON) $(SCRIPTS)/unified_indexer.py $(INDEX_ARGS)

index-all: index

index-matrix:
	$(INDEX_ENV) $(PYTHON) $(SCRIPTS)/unified_indexer.py --matrix

# Drop the Qdrant collection. Destructive — used before a clean-schema rebuild
# (e.g. after changing named-vector layout). Idempotent: no-op if the
# collection doesn't exist.
drop:
	$(PYTHON) -c "from qdrant_client import QdrantClient; c = QdrantClient(url='http://localhost:6333'); c.delete_collection('unified_knowledge') if c.collection_exists('unified_knowledge') else None; print('unified_knowledge: dropped' if not c.collection_exists('unified_knowledge') else 'still exists')"

# Clean rebuild: drop the collection, then reindex all sources from scratch
# with the Matrix-rain display. Use after a schema-incompatible change.
reindex-clean: drop
	$(INDEX_ENV) $(PYTHON) $(SCRIPTS)/unified_indexer.py --matrix

# Quiet clean rebuild: same as reindex-clean but silences progress output and
# streams it to a log file. Intended for delegated / automated runs where
# dumping tens of thousands of lines into a caller's context is the opposite
# of helpful. On failure the tail of the log is surfaced so the cause isn't
# buried. On success the tail shows the index summary (counts, timings).
reindex-bg: drop
	@mkdir -p logs
	@echo "Clean reindex starting. Log: logs/reindex-latest.log"
	@$(INDEX_ENV) $(PYTHON) $(SCRIPTS)/unified_indexer.py > logs/reindex-latest.log 2>&1 \
		|| (echo "Reindex FAILED. Last 30 lines of log:"; tail -n 30 logs/reindex-latest.log; exit 1)
	@echo "Reindex complete. Summary:"
	@tail -n 20 logs/reindex-latest.log

matrix-demo:
	$(PYTHON) $(SCRIPTS)/matrix_display.py

# ── Stats ─────────────────────────────────────────────────────────────────────

stats:
	$(PYTHON) $(SCRIPTS)/show_stats.py
