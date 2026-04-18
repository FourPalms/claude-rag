#!/usr/bin/env python3
"""
Matrix-style live display for the RAG indexer.

Digital rain, phosphor green on black, where the characters raining down are
sampled from whatever chunks the indexer is currently embedding. A single
stats line pinned to the bottom row; the currently-indexing chunk path
briefly flashes near the center.

Usage (context manager):

    with MatrixDisplay() as display:
        display.set_progress(0, total)
        for i, chunk in enumerate(chunks):
            display.push_chunk(metadata, document)
            ...embed + upsert...
            display.set_progress(i + 1, total)

Renders on a background thread at ~20fps using raw ANSI escapes. Writes go to
the real TTY (sys.__stdout__) so regular stdout can be safely redirected by
the caller to suppress normal indexer print() output.
"""

import random
import shutil
import sys
import threading
import time
from collections import deque
from typing import Deque, Dict, List, Optional

# Authentic Matrix glyph pool: the 33-character half-width katakana subset
# that frame-by-frame analysis of the 1999 film identifies as actually
# appearing on screen (production designer Simon Whiteley, Animal Logic,
# befores & afters podcast, March 2019). Full U+FF66–U+FF9F is broader; we
# narrow to the on-screen subset for authenticity. Plus Arabic numerals.
_KATAKANA = list("ｦｱｳｴｵｶｷｹｺｻｼｽｾｿﾀﾂﾃﾅﾆﾇﾈﾊﾋﾎﾏﾐﾑﾒﾓﾔﾕﾗﾘﾜ")
_DIGITS = list("0123456789")
_FALLBACK_POOL = _KATAKANA + _DIGITS

# ANSI escape helpers. We use 256-color mode for granular green fades; every
# reasonably modern terminal supports it.
#
# Every paint carries BOTH background and foreground in a single SGR sequence
# because terminals default to the user's theme background (typically not
# black). If we only set foreground, unpainted/cleared cells — and anywhere
# we wrote \033[0m — fall back to the theme bg, which blows out the Matrix
# look. Carrying bg=16 (true black) in every escape keeps the field pure.
_ESC = "\033["
_HIDE_CURSOR = f"{_ESC}?25l"
_SHOW_CURSOR = f"{_ESC}?25h"
_ENTER_ALT_SCREEN = f"{_ESC}?1049h"
_LEAVE_ALT_SCREEN = f"{_ESC}?1049l"
_CLEAR = f"{_ESC}2J"
_RESET = f"{_ESC}0m"

# Color combinations: black bg + the various fg shades we need.
# 256-color index 16 is true black; 46 is the classic #00FF00 phosphor green.
_HEAD = f"{_ESC}48;5;16;97;1m"        # bold white on black — leading char
_NEAR = f"{_ESC}48;5;16;38;5;46m"     # bright green on black
_MID = f"{_ESC}48;5;16;38;5;40m"       # mid green
_DIM = f"{_ESC}48;5;16;38;5;28m"       # dim green
_FAINT = f"{_ESC}48;5;16;38;5;22m"     # faintest green tail
_BG_ONLY = f"{_ESC}48;5;16m"           # black bg, keep current fg
_BORDER = f"{_ESC}48;5;16;38;5;46;1m"  # bright green bold — panel border
_LABEL = f"{_ESC}48;5;16;38;5;40m"     # mid green — label text
_VALUE = f"{_ESC}48;5;16;97m"          # white — value text
_STATS = f"{_ESC}48;5;16;38;5;46;1m"   # bright green bold — stats line


def _at(row: int, col: int) -> str:
    """ANSI cursor-position escape. 1-indexed, per the spec."""
    return f"{_ESC}{row};{col}H"


class _Drop:
    """One rain column. Position, speed, character buffer, last-paint state."""

    __slots__ = ("col", "rows", "pos", "speed", "tail_len", "buf", "_last_int_pos")

    def __init__(self, col: int, rows: int):
        self.col = col
        self.rows = rows
        self._reset(initial=True)

    def _reset(self, initial: bool = False) -> None:
        # Start above the visible area so the stream "emerges" at the top —
        # head-first, tail building up row by row as the head descends.
        # Initial batch uses a wider offset range so the startup state has
        # streams at varying levels of maturity (not all bunched at the top).
        if initial:
            self.pos: float = float(-random.randint(0, self.rows * 2))
        else:
            self.pos = float(-random.randint(0, 4))
        # Speed floor of 0.6 keeps the slowest drops from stall-then-jumping.
        # Upper bound 1.0 guarantees we never skip a row per frame.
        self.speed: float = random.uniform(0.6, 1.0)
        # Stream length range from frame-by-frame analysis of the 1999 film
        # (carlnewton / Rezmason / SwiftToolkit). Shorter than our old 6–18;
        # leaves room for gaps and second streams in the same column.
        self.tail_len: int = random.randint(5, 10)
        self.buf: Deque[str] = deque(maxlen=self.tail_len + 4)
        # Sentinel — None forces a full-tail repaint on the first frame after
        # reset. Set by mark_painted() each time the renderer finishes a tick.
        self._last_int_pos = None

    def advance(self, char_source: "list[str]") -> bool:
        """Tick by one speed step. Returns True iff the integer head row changed.

        Sub-integer movement is invisible — no repaint needed. Only when we
        actually cross into a new row does a fresh glyph enter the buffer and
        the renderer need to emit any ANSI. This is the core trick that keeps
        Terminal.app from choking: skipped drops cost 0 paint ops.
        """
        self.pos += self.speed
        if int(self.pos) == self._last_int_pos:
            return False
        # ~70% authentic katakana/digits, ~30% chars sampled from chunk
        # content so the rain feels connected to the current workload.
        if char_source and random.random() < 0.3:
            ch = random.choice(char_source)
        else:
            ch = random.choice(_FALLBACK_POOL)
        self.buf.appendleft(ch)
        return True

    def needs_full_paint(self) -> bool:
        return self._last_int_pos is None

    def mark_painted(self) -> None:
        self._last_int_pos = int(self.pos)

    def off_screen(self) -> bool:
        return int(self.pos) - self.tail_len > self.rows

    def reset(self) -> None:
        self._reset()


class MatrixDisplay:
    """Live Matrix-rain display. Safe as a context manager."""

    FRAME_HZ = 20
    FRAME_TIME = 1.0 / FRAME_HZ
    # Stride 3 leaves breathing room between columns — readable instead of a
    # solid wall of glyphs.
    COLUMN_STRIDE = 3
    # Bottom row is reserved for stats, second-to-bottom for breathing room.
    STATS_ROWS = 2
    # Cap the rain area to a fixed-size window centered in the terminal.
    # Rendering the full terminal saturates slow emulators (Terminal.app
    # in particular) — Jeremy's keystrokes started lagging when the rain
    # covered a 200×50 terminal. Bounding the area makes CPU cost constant
    # rather than scaling with terminal size, at the cost of some black
    # letterboxing around the edges.
    RAIN_WIDTH = 100
    RAIN_HEIGHT = 28
    # Per-frame probability of spawning a new stream in each column. At 20fps,
    # 1/25 means ~one new stream every 1.25s per column on average. Combined
    # with MAX_PER_COLUMN, columns naturally drift in and out of activity —
    # this is what produces the authentic "gaps between streams" look.
    SPAWN_PROB = 1.0 / 25.0
    MAX_PER_COLUMN = 3
    # Don't spawn if another stream's head is still in the top N rows of the
    # column — keeps streams from overlapping or spawning on top of each other.
    MIN_SPAWN_GAP = 6

    def __init__(self) -> None:
        size = shutil.get_terminal_size(fallback=(100, 30))
        self.term_cols: int = size.columns
        self.term_rows: int = size.lines
        # Rain area is capped to RAIN_WIDTH × RAIN_HEIGHT, or smaller if the
        # terminal itself is smaller. Centered within the terminal so the
        # letterboxing looks intentional.
        self.cols: int = min(self.RAIN_WIDTH, self.term_cols)
        self.rows: int = min(self.RAIN_HEIGHT, self.term_rows)
        self.rain_rows: int = max(self.rows - self.STATS_ROWS, 5)
        # Origin of the rain area inside the terminal, 1-indexed for ANSI.
        self.origin_col: int = max(1, (self.term_cols - self.cols) // 2 + 1)
        self.origin_row: int = max(1, (self.term_rows - self.rows) // 2 + 1)

        # The set of columns the rain can occupy. Streams spawn and die over
        # time; self.drops is a flat list of every currently-alive stream
        # across all columns, not a 1:1 mapping.
        self.drop_cols: List[int] = list(range(0, self.cols, self.COLUMN_STRIDE))
        self.drops: List[_Drop] = [
            _Drop(c, self.rain_rows) for c in self.drop_cols
        ]

        # Characters the rain pulls from — seeded by indexed chunks.
        self.char_pool: Deque[str] = deque(maxlen=2000)
        self.current_chunk: Optional[str] = None
        self.current_meta: Optional[Dict] = None

        self.current_progress: int = 0
        self.total_progress: int = 0
        self.started_at: float = 0.0

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._out = sys.__stdout__  # real TTY, even if stdout is redirected

    # ----- internals ---------------------------------------------------------

    def _pos(self, row: int, col: int) -> str:
        """ANSI cursor-position for (row, col) inside the rain area, 0-indexed."""
        return _at(self.origin_row + row, self.origin_col + col)

    # ----- context manager ---------------------------------------------------

    def __enter__(self) -> "MatrixDisplay":
        self.started_at = time.monotonic()
        # Enter alt screen, hide cursor, force black bg, then paint ONLY the
        # rain rectangle black. Leaving the surrounding area untouched (alt
        # screen default) keeps us from sending extra ANSI to the terminal
        # for cells we don't render — that's where the Terminal.app hog was.
        buf = [_ENTER_ALT_SCREEN, _HIDE_CURSOR, _BG_ONLY, _CLEAR]
        for row in range(self.rows):
            buf.append(self._pos(row, 0))
            buf.append(_BG_ONLY + " " * self.cols)
        self._out.write("".join(buf))
        self._out.flush()
        self._thread = threading.Thread(target=self._render_loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        # Always restore the terminal, even on exception.
        self._out.write(_RESET + _SHOW_CURSOR + _LEAVE_ALT_SCREEN)
        self._out.flush()

    # ----- API called by indexer --------------------------------------------

    def push_chunk(self, metadata: Dict, document: str) -> None:
        """Record that a chunk is being indexed. Seeds the rain with its chars."""
        path = metadata.get("filepath", "") or metadata.get("filename", "") or "?"
        # Sample a bounded amount of content so enormous chunks don't flood
        # the pool with one file's characters.
        sample = (path + " " + document[:400]).replace("\n", " ")
        with self._lock:
            for ch in sample:
                if ch.isprintable() and not ch.isspace():
                    self.char_pool.append(ch)
            self.current_chunk = path
            # Snapshot the metadata so the panel can show source / chunk_type /
            # etc. even if the caller mutates the dict after push_chunk returns.
            self.current_meta = dict(metadata)

    def set_progress(self, current: int, total: int) -> None:
        with self._lock:
            self.current_progress = current
            self.total_progress = total

    # ----- render loop -------------------------------------------------------

    def _render_loop(self) -> None:
        while not self._stop.is_set():
            frame_start = time.monotonic()
            try:
                self._render_frame()
            except Exception:
                # A render crash must not kill the indexer. Swallow silently
                # and try again next frame.
                pass
            elapsed = time.monotonic() - frame_start
            self._stop.wait(max(0.0, self.FRAME_TIME - elapsed))

    def _render_frame(self) -> None:
        parts: List[str] = []

        with self._lock:
            char_source = list(self.char_pool) if self.char_pool else []

            # Intensity boundaries along the tail. When the head advances one
            # row, each of these relative positions needs exactly one re-paint
            # to demote the character that was there to the next color tier.
            # Everything between boundaries keeps its prior paint (unchanged
            # visually, so we don't re-emit ANSI for it).
            TRANSITIONS = (
                (0, _HEAD),
                (1, _NEAR),
                (2, _MID),
                (4, _DIM),
                (8, _FAINT),
            )

            # --- Advance + paint every live drop ---
            surviving: List[_Drop] = []
            per_col_count: dict = {}
            for drop in self.drops:
                if drop.advance(char_source):
                    head_row = int(drop.pos)

                    if drop.needs_full_paint():
                        # First frame after spawn — no prior paint to demote
                        # from. Paint the entire visible tail bottom-up once;
                        # subsequent frames will only touch transitions.
                        for i, ch in enumerate(drop.buf):
                            row = head_row - i
                            if row < 0 or row >= self.rain_rows:
                                continue
                            parts.append(self._pos(row, drop.col))
                            if i == 0:
                                parts.append(_HEAD + ch)
                            elif i == 1:
                                parts.append(_NEAR + ch)
                            elif i < 4:
                                parts.append(_MID + ch)
                            elif i < 8:
                                parts.append(_DIM + ch)
                            else:
                                parts.append(_FAINT + ch)
                    else:
                        # Delta paint: only the 5 color-transition boundaries
                        # + the cell falling off the tail end actually changed.
                        # ~6 paint ops/frame/drop instead of ~tail_len.
                        for offset, color in TRANSITIONS:
                            row = head_row - offset
                            if 0 <= row < self.rain_rows and offset < len(drop.buf):
                                parts.append(self._pos(row, drop.col))
                                parts.append(color + drop.buf[offset])
                        erase_row = head_row - drop.tail_len
                        if 0 <= erase_row < self.rain_rows:
                            parts.append(self._pos(erase_row, drop.col))
                            parts.append(_BG_ONLY + " ")

                    drop.mark_painted()

                if drop.off_screen():
                    # Stream finished — it dies. A new one may spawn in this
                    # column in subsequent frames via the spawn loop below.
                    continue
                surviving.append(drop)
                per_col_count[drop.col] = per_col_count.get(drop.col, 0) + 1

            # --- Probabilistic spawning per column ---
            # For each column, maybe spawn a new stream. Rate-limited by
            # MAX_PER_COLUMN and a minimum-gap check so new streams don't
            # materialize on top of existing heads.
            new_drops: List[_Drop] = []
            for col in self.drop_cols:
                if per_col_count.get(col, 0) >= self.MAX_PER_COLUMN:
                    continue
                # Gap check: any existing stream with head still in the top
                # MIN_SPAWN_GAP rows blocks a new spawn here.
                head_too_close = False
                if per_col_count.get(col, 0) > 0:
                    for d in surviving:
                        if d.col == col and 0 <= int(d.pos) < self.MIN_SPAWN_GAP:
                            head_too_close = True
                            break
                if head_too_close:
                    continue
                if random.random() < self.SPAWN_PROB:
                    new_drops.append(_Drop(col, self.rain_rows))

            self.drops = surviving + new_drops

            # Current-chunk panel — painted after rain so it sits on top.
            parts.extend(self._compose_panel())

            # Stats line pinned to the last row of the rain area.
            stats = self._format_stats()
            parts.append(self._pos(self.rows - 1, 0))
            parts.append(_STATS + stats)
            trailing = self.cols - len(stats)
            if trailing > 0:
                # Trailing padding uses bg-only so leftover cells stay black
                # with the current (green) fg — purely cosmetic.
                parts.append(" " * trailing)

        self._out.write("".join(parts))
        self._out.flush()

    def _compose_panel(self) -> List[str]:
        """Centered bordered box showing what chunk is currently being indexed."""
        meta = self.current_meta
        panel_w = min(64, self.cols - 6)
        panel_h = 7
        if panel_w < 30 or self.rain_rows < panel_h + 2:
            # Too small to render a usable panel — skip gracefully.
            return []
        # Panel position is relative to the rain area, not the terminal.
        panel_left = (self.cols - panel_w) // 2
        panel_top = max(0, (self.rain_rows - panel_h) // 2)
        inner_w = panel_w - 2  # width between the left/right border chars

        # Title bar — centered " INDEXING " surrounded by ─ dashes.
        title = " INDEXING "
        dash_budget = inner_w - len(title)
        lpad = max(0, dash_budget // 2)
        rpad = max(0, dash_budget - lpad)
        top = "╭" + "─" * lpad + title + "─" * rpad + "╮"
        bottom = "╰" + "─" * inner_w + "╯"

        # Pull the values the panel shows. When nothing has been indexed yet
        # we still render the frame (so the shape is visible from startup) —
        # just with placeholder text.
        if meta is None:
            source = "—"
            chunk_type = "—"
            path = "(warming up)"
        else:
            source = str(meta.get("source", "—"))
            chunk_type = str(meta.get("chunk_type", "—"))
            path = (
                meta.get("filepath")
                or meta.get("filename")
                or meta.get("issue_key")
                or "—"
            )

        label_w = 8  # width of the label column
        # Leading padding (2) + label + gap (2) + value + trailing = inner_w
        value_w = inner_w - 2 - label_w - 2

        def fit(value: str, width: int) -> str:
            if len(value) > width:
                return "…" + value[-(width - 1) :]
            return value + " " * (width - len(value))

        def row_content(label: str, value: str) -> str:
            # Returns a single interior line (no border chars), with escape
            # codes for label vs value styling. The visible width is exactly
            # inner_w thanks to the fit() calls.
            return (
                _BG_ONLY + "  "
                + _LABEL + fit(label, label_w)
                + _BG_ONLY + "  "
                + _VALUE + fit(value, value_w)
            )

        def blank_row() -> str:
            return _BG_ONLY + " " * inner_w

        lines: List[str] = [
            _BORDER + top,
            _BORDER + "│" + blank_row() + _BORDER + "│",
            _BORDER + "│" + row_content("source", source) + _BORDER + "│",
            _BORDER + "│" + row_content("chunk", chunk_type) + _BORDER + "│",
            _BORDER + "│" + row_content("file", str(path)) + _BORDER + "│",
            _BORDER + "│" + blank_row() + _BORDER + "│",
            _BORDER + bottom,
        ]

        parts: List[str] = []
        for i, line in enumerate(lines):
            parts.append(self._pos(panel_top + i, panel_left))
            parts.append(line)
        return parts

    # ----- helpers -----------------------------------------------------------

    def _format_stats(self) -> str:
        current = self.current_progress
        total = self.total_progress
        elapsed = max(time.monotonic() - self.started_at, 0.001)
        rate = current / elapsed if elapsed > 0 else 0.0
        if total > 0:
            pct = 100.0 * current / total
            remaining = max(total - current, 0)
            eta_s = remaining / rate if rate > 0 else 0.0
            eta = _format_duration(eta_s)
        else:
            pct = 0.0
            eta = "--"
        return (
            f" ▪ {current:,} / {total:,}"
            f"  ▪ {pct:5.1f}%"
            f"  ▪ {rate:6.1f}/s"
            f"  ▪ ETA {eta}"
            f"  ▪ elapsed {_format_duration(elapsed)} "
        )

def _format_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m{s:02d}s"


def is_tty() -> bool:
    """True only when the real stdout is a terminal that can render ANSI."""
    try:
        return sys.__stdout__.isatty()
    except (AttributeError, ValueError):
        return False


def _demo() -> None:
    """Standalone demo: feed fake indexing events into the display.

    Run directly to preview the effect without a real indexer pass:
        python3 scripts/matrix_display.py
    """
    # Mirror the real indexer's metadata shape — every chunk carries source,
    # chunk_type, and filepath. Lets the demo show what the panel really
    # looks like mid-reindex.
    fake_chunks = [
        {
            "source": "code",
            "chunk_type": "method",
            "filepath": "/Users/jeremyhunt/repos/main/app/BambooHR/Silo/Payroll/Adapter.php",
            "doc": "class PayrollAdapter implements AdapterInterface { function sync(): void",
        },
        {
            "source": "code",
            "chunk_type": "class",
            "filepath": "/Users/jeremyhunt/repos/main/app/BambooHR/Silo/NewHire/Controller.php",
            "doc": "NewHirePacketTemplateRepositoryTest::testDeletesTemplate",
        },
        {
            "source": "jira",
            "chunk_type": "description",
            "filepath": "jira://SKY/SKY-988#description",
            "doc": "Workflows endpoints auth investigation and rollout plan",
        },
        {
            "source": "jira",
            "chunk_type": "comment",
            "filepath": "jira://SKY/SKY-995#comment-1085876",
            "doc": "I don't think we ever added API key auth. It just is in main.",
        },
        {
            "source": "jira",
            "chunk_type": "comment",
            "filepath": "jira://BUGS/BUGS-35152#comment-1112749",
            "doc": "Repro: hit endpoint with no token, get 500 instead of 401",
        },
        {
            "source": "js_ts",
            "chunk_type": "function",
            "filepath": "/Users/jeremyhunt/repos/Po/packages/settings-job-organization/src/components/job-org-entity-table/cant-delete-modal.tsx",
            "doc": "const JobOrgEntityTable = ({ entities }) => entities.map(render)",
        },
        {
            "source": "sanctum",
            "chunk_type": "file",
            "filepath": "/Users/jeremyhunt/.claude/skysteelers-sanctum/initiatives/workflows/tickets/sky-947.md",
            "doc": "SKY-947: OAS contract completeness across six GET endpoints",
        },
        {
            "source": "puppet",
            "chunk_type": "class",
            "filepath": "/Users/jeremyhunt/repos/puppet/site/profiles/manifests/datadog/agent.pp",
            "doc": "class profile::datadog::agent { include datadog_agent::integrations::http }",
        },
        {
            "source": "sessions",
            "chunk_type": "conversation_exchange",
            "filepath": "/Users/jeremyhunt/.claude/session-archive/5673a6d7-3467-4e79-b2c6-d00d17ecd6d7.jsonl",
            "doc": "USER: can we inspect the metadata keys? ASSISTANT: let me scroll qdrant",
        },
        {
            "source": "slack",
            "chunk_type": "message",
            "filepath": "slack://development/1774466654.550559",
            "doc": "fyi the release branch cuts thursday, anything non-critical waits",
        },
    ]
    total = 4000
    with MatrixDisplay() as display:
        for i in range(total):
            chunk = random.choice(fake_chunks)
            meta = {k: v for k, v in chunk.items() if k != "doc"}
            display.push_chunk(meta, chunk["doc"])
            display.set_progress(i + 1, total)
            # Simulate a realistic indexer cadence (~80 chunks/s).
            time.sleep(0.012)


if __name__ == "__main__":
    _demo()
