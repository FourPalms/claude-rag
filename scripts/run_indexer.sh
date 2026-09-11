#!/bin/sh
#
# launchd entry point for the daily RAG index (com.bamboohr.rag-indexer).
#
# Why this wrapper exists rather than launchd invoking python directly:
#
#   1. A wall-clock cap. On 2026-08-07 an 8am run blocked forever on a stalled
#      HTTPS socket — 0% CPU, three days alive, never crashing and never
#      exiting. launchd will not start a new instance while the previous one is
#      still running, so every subsequent day's run was skipped and the index
#      silently went stale. `timeout` guarantees a run cannot outlive its day.
#
#   2. An alert on that cap. The indexer notifies on ingest failures and (since
#      the same date) on unhandled crashes, but a process killed from outside
#      cannot report on itself. Without the notify below, a hung-then-killed run
#      would be exactly as silent as the hang it replaced.
#
#   3. An alert on ANY non-zero exit. The indexer's own notifications cover a
#      run that finished, and its crash handler covers an unhandled exception.
#      Neither covers a run that declined to start: main() returns early — and
#      silently — when tree-sitter is missing, when a code repo has uncommitted
#      changes, or when no source collected anything. On 2026-09-11 the 6am run
#      refused over a dirty repo, wrote its reason to this log, exited 2, and
#      said nothing; the index simply did not update that day and nobody knew
#      until asked. Checking the exit code here covers all three at once, plus
#      any early return added later, plus the failures Python cannot report on
#      itself: an import error before the crash handler is installed, a SIGKILL,
#      an OOM kill, an interpreter that never starts.
#
# Timeout budget: a full index of all sources measured ~46 minutes on
# 2026-08-10. 2 hours leaves generous headroom while still bounding a hang to
# well under the 24-hour gap between runs.

# Absolute defaults because launchd runs with a minimal PATH, but overridable
# so this is not pinned to one machine's Homebrew prefix and Python install.
TIMEOUT_BIN=${RAG_INDEXER_TIMEOUT_BIN:-/opt/homebrew/bin/timeout}
PYTHON_BIN=${RAG_INDEXER_PYTHON:-/Library/Frameworks/Python.framework/Versions/3.12/bin/python3}
# Overridable so the timeout-and-alert path can be exercised without waiting
# two hours: RAG_INDEXER_MAX_SECONDS=5 sh scripts/run_indexer.sh
MAX_SECONDS=${RAG_INDEXER_MAX_SECONDS:-7200}
GRACE_SECONDS=60

cd "$(dirname "$0")/.." || exit 1

if [ ! -x "$TIMEOUT_BIN" ]; then
	# Homebrew coreutils is gone. Run uncapped rather than not at all, but say
	# so — an uncapped run can hang, which is the failure this wrapper exists
	# to prevent.
	echo "WARNING: $TIMEOUT_BIN not found; running without a wall-clock cap."
	"$PYTHON_BIN" -u scripts/unified_indexer.py "$@"
	rc=$?
else
	# -k: if the run ignores TERM at the cap, follow up with KILL after the
	# grace. Any arguments given to this wrapper are forwarded to the indexer,
	# which is how the plist selects which sources to index.
	#
	# -u so a run killed at the cap doesn't lose its unflushed tail: launchd
	# redirects stdout to a file, which makes Python block-buffer it.
	"$TIMEOUT_BIN" -k "$GRACE_SECONDS" "$MAX_SECONDS" "$PYTHON_BIN" -u scripts/unified_indexer.py "$@"
	rc=$?
fi

notify() {
	# Backgrounded and output-suppressed so a failed or unavailable osascript
	# can never change the wrapper's exit code or block the run from ending.
	osascript -e "display dialog \"$2

Log: ~/Library/Logs/bamboohr-rag-indexer.log\" with title \"$1\" buttons {\"OK\"} default button \"OK\" with icon caution" >/dev/null 2>&1 &
}

# 124 is timeout(1)'s signal that it killed the child at the cap.
if [ "$rc" -eq 124 ]; then
	echo "ERROR: indexer exceeded ${MAX_SECONDS}s and was killed."
	# Report the cap that actually tripped rather than a hardcoded figure, so
	# the alert stays truthful when MAX_SECONDS is overridden.
	notify "RAG indexer: run timed out" \
		"The daily RAG index hung and was killed after ${MAX_SECONDS}s. The index was NOT updated.

Most likely a stalled API connection (Jira / Slack / Slite)."
elif [ "$rc" -ne 0 ]; then
	# Everything else non-zero. The indexer prints its reason to this log
	# before returning, so the log is where the answer is; the point of this
	# alert is only that the day's index did not happen at all.
	echo "ERROR: indexer exited $rc without indexing."
	notify "RAG indexer: run did not complete — index NOT updated" \
		"The daily RAG index exited with code $rc. The index was NOT updated.

Common causes: uncommitted changes in a code repo (exit 2), a broken tree-sitter install, or no documents collected."
fi

exit $rc
