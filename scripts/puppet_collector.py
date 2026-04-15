"""
Puppet Repository Collector for RAG.

Indexes three file types from the puppet repo:
  .pp  — Puppet manifests: regex-based extraction of class/define/function blocks
  .rb  — Ruby code: tree-sitter AST extraction of modules, classes, methods
  .yaml/.yml — Hiera data: whole-file chunks (they are configuration data, not code)

Only indexes ~/repos/puppet/site/ (custom modules).
Skips ~/repos/puppet/modules/ (external Forge packages).
"""

import re
import warnings
from pathlib import Path
from typing import List, Dict, Tuple, Optional

import tree_sitter_languages

warnings.filterwarnings("ignore", category=FutureWarning)


# ── Directories to skip ────────────────────────────────────────────────────────
SKIP_DIRS = {".git", "__pycache__", "spec", ".bundle", "vendor"}

# ── Ruby tree-sitter parser (created once) ────────────────────────────────────
_ruby_parser = None


def _get_ruby_parser():
    global _ruby_parser
    if _ruby_parser is None:
        _ruby_parser = tree_sitter_languages.get_parser("ruby")
    return _ruby_parser


# ══════════════════════════════════════════════════════════════════════════════
#  PUPPET MANIFEST PARSER  (.pp)
# ══════════════════════════════════════════════════════════════════════════════

# Puppet DSL top-level block declarations (always start a line, optionally with
# a leading parameter list that spans multiple lines before the opening '{').
_PP_BLOCK_RE = re.compile(r"^(class|define|function)\s+([\w:]+)", re.MULTILINE)


def _extract_comment_block(lines: List[str], start_idx: int) -> str:
    """
    Walk backwards from start_idx to collect a contiguous run of # comment lines.
    Returns them as a single string (or '' if none).
    """
    i = start_idx - 1
    comment_lines = []
    while i >= 0 and lines[i].lstrip().startswith("#"):
        comment_lines.append(lines[i])
        i -= 1
    comment_lines.reverse()
    return "".join(comment_lines)


def _find_block_end(lines: List[str], header_line_idx: int) -> int:
    """
    Given the line index where a class/define/function keyword was found,
    scan forward to find the matching closing '}' and return its line index.

    Tracks brace depth; handles strings naively (good enough for Puppet DSL,
    which rarely embeds literal braces inside double-quoted strings without
    them being variable interpolations like ${var}).
    """
    depth = 0
    found_open = False
    in_single_quote = False

    for i in range(header_line_idx, len(lines)):
        line = lines[i]
        j = 0
        while j < len(line):
            ch = line[j]

            # Toggle single-quote string state (Puppet single-quotes are literal)
            if ch == "'" and not in_single_quote:
                in_single_quote = True
                j += 1
                continue
            if ch == "'" and in_single_quote:
                in_single_quote = False
                j += 1
                continue

            if in_single_quote:
                j += 1
                continue

            # Skip line comments
            if ch == "#":
                break  # rest of this line is a comment

            if ch == "{":
                depth += 1
                found_open = True
            elif ch == "}":
                depth -= 1
                if found_open and depth == 0:
                    return i  # closing brace found on this line
            j += 1

    # Fell off the end — return last line
    return len(lines) - 1


def chunk_puppet_file(filepath: Path) -> List[Dict]:
    """
    Parse a Puppet manifest (.pp) and extract semantic chunks.

    Each class, define, or function declaration becomes one chunk.
    Leading comment blocks are prepended for context.
    If no top-level block is found (e.g. node declarations, simple resource
    files), the whole file is returned as a single chunk.
    """
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        print(f"⚠️  Error reading {filepath}: {e}")
        return []

    if not source.strip():
        return []

    lines = source.splitlines(keepends=True)
    chunks = []
    covered_lines: set = set()

    for m in _PP_BLOCK_RE.finditer(source):
        block_type = m.group(1)  # 'class' | 'define' | 'function'
        block_name = m.group(2)  # e.g. 'profile::base'

        # Convert byte offset → line index
        header_line_idx = source[: m.start()].count("\n")

        # Skip if already consumed (nested classes are rare in Puppet but
        # inner declarations inside 'define' bodies shouldn't be re-chunked)
        if header_line_idx in covered_lines:
            continue

        end_line_idx = _find_block_end(lines, header_line_idx)

        # Mark all lines in this block as covered
        for li in range(header_line_idx, end_line_idx + 1):
            covered_lines.add(li)

        # Grab preceding comment block for context
        comment = _extract_comment_block(lines, header_line_idx)
        block_text = "".join(lines[header_line_idx : end_line_idx + 1])
        content = (comment + block_text).strip()

        chunks.append(
            {
                "content": content,
                "metadata": {
                    "chunk_type": block_type,  # 'class' | 'define' | 'function'
                    "puppet_name": block_name,
                    "start_line": header_line_idx + 1,
                    "end_line": end_line_idx + 1,
                    "has_comment": bool(comment.strip()),
                },
            }
        )

    # If nothing was extracted (e.g. a file with only node{} or resource
    # declarations), fall back to whole-file chunk.
    if not chunks and source.strip():
        chunks.append(
            {
                "content": source.strip(),
                "metadata": {
                    "chunk_type": "file",
                    "puppet_name": filepath.stem,
                    "start_line": 1,
                    "end_line": len(lines),
                    "has_comment": False,
                },
            }
        )

    return chunks


# ══════════════════════════════════════════════════════════════════════════════
#  RUBY CODE PARSER  (.rb)
# ══════════════════════════════════════════════════════════════════════════════


def _rb_text(node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _rb_name(node, source: bytes) -> str:
    """Extract the primary name identifier from a Ruby module/class/method node."""
    for child in node.children:
        if child.type in ("identifier", "constant", "scope_resolution"):
            return _rb_text(child, source)
    return "unknown"


def chunk_ruby_file(filepath: Path) -> List[Dict]:
    """
    Parse a Ruby file with tree-sitter and extract semantic chunks.

    Extracts:
      - module definitions
      - class definitions
      - method definitions (def / def self.xxx)

    For very small files (< 30 lines) that produce no structured chunks,
    falls back to a whole-file chunk.
    """
    try:
        source = filepath.read_bytes()
    except Exception as e:
        print(f"⚠️  Error reading {filepath}: {e}")
        return []

    if not source.strip():
        return []

    try:
        parser = _get_ruby_parser()
        tree = parser.parse(source)
    except Exception as e:
        print(f"⚠️  Error parsing {filepath}: {e}")
        return []

    chunks = []

    def traverse(node, current_class: str = "", current_module: str = ""):
        # node.is_named=False means it's an anonymous keyword/punctuation token —
        # skip it so that e.g. the 'module' keyword inside a module definition
        # is not mistakenly treated as a nested module declaration.
        if not node.is_named:
            return

        if node.type == "module":
            name = _rb_name(node, source)
            code = _rb_text(node, source)
            chunks.append(
                {
                    "content": code,
                    "metadata": {
                        "chunk_type": "module",
                        "ruby_name": name,
                        "start_line": node.start_point[0] + 1,
                        "end_line": node.end_point[0] + 1,
                    },
                }
            )
            # Recurse into module body for inner classes/methods
            for child in node.children:
                traverse(child, current_class=name, current_module=name)

        elif node.type == "class":
            name = _rb_name(node, source)
            code = _rb_text(node, source)
            chunks.append(
                {
                    "content": code,
                    "metadata": {
                        "chunk_type": "class",
                        "ruby_name": name,
                        "module_name": current_module,
                        "start_line": node.start_point[0] + 1,
                        "end_line": node.end_point[0] + 1,
                    },
                }
            )
            # Recurse for nested methods
            for child in node.children:
                traverse(child, current_class=name, current_module=current_module)

        elif node.type == "method":
            name = _rb_name(node, source)
            code = _rb_text(node, source)
            meta: Dict = {
                "chunk_type": "method",
                "ruby_name": name,
                "start_line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
            }
            if current_class:
                meta["class_name"] = current_class
            if current_module:
                meta["module_name"] = current_module
            chunks.append({"content": code, "metadata": meta})

        elif node.type == "singleton_method":
            # def self.name(...)
            name = _rb_name(node, source)
            code = _rb_text(node, source)
            meta = {
                "chunk_type": "method",
                "ruby_name": f"self.{name}",
                "start_line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
            }
            if current_class:
                meta["class_name"] = current_class
            if current_module:
                meta["module_name"] = current_module
            chunks.append({"content": code, "metadata": meta})

        else:
            for child in node.children:
                traverse(child, current_class, current_module)

    traverse(tree.root_node)

    # Fallback: small files or files with no parseable structure → whole file
    if not chunks:
        source_str = source.decode("utf-8", errors="replace").strip()
        if source_str:
            line_count = source_str.count("\n") + 1
            chunks.append(
                {
                    "content": source_str,
                    "metadata": {
                        "chunk_type": "file",
                        "ruby_name": filepath.stem,
                        "start_line": 1,
                        "end_line": line_count,
                    },
                }
            )

    return chunks


# ══════════════════════════════════════════════════════════════════════════════
#  HIERA YAML COLLECTOR  (.yaml / .yml)
# ══════════════════════════════════════════════════════════════════════════════


def chunk_yaml_file(filepath: Path) -> List[Dict]:
    """
    Index a Hiera YAML file as a single whole-file chunk.
    These are configuration/data files, not code.
    """
    try:
        content = filepath.read_text(encoding="utf-8", errors="replace").strip()
    except Exception as e:
        print(f"⚠️  Error reading {filepath}: {e}")
        return []

    if not content:
        return []

    line_count = content.count("\n") + 1
    return [
        {
            "content": content,
            "metadata": {
                "chunk_type": "hiera_data",
                "start_line": 1,
                "end_line": line_count,
                "has_comment": False,
            },
        }
    ]


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN COLLECTION FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════


def _should_skip(filepath: Path) -> bool:
    """Return True if any part of the path is in SKIP_DIRS."""
    return any(part in SKIP_DIRS for part in filepath.parts)


def collect_puppet_code(
    directory_paths: List[str],
) -> List[Tuple[Path, str, List[Dict]]]:
    """
    Collect Puppet manifests (.pp), Ruby code (.rb), and Hiera YAML from the
    specified directories.

    Args:
        directory_paths: List of directory paths to index recursively.
                         Typically ['~/repos/puppet/site',
                                    '~/repos/puppet/hieradata']

    Returns:
        List of (filepath, source_name, chunks) tuples.
    """
    results = []

    for dir_path_str in directory_paths:
        dir_path = Path(dir_path_str).expanduser().resolve()

        if not dir_path.exists():
            print(f"⚠️  Directory not found: {dir_path}")
            continue

        if not dir_path.is_dir():
            print(f"⚠️  Not a directory: {dir_path}")
            continue

        pp_files = [f for f in dir_path.rglob("*.pp") if not _should_skip(f)]
        rb_files = [f for f in dir_path.rglob("*.rb") if not _should_skip(f)]
        yaml_files = [
            f
            for ext in ("*.yaml", "*.yml")
            for f in dir_path.rglob(ext)
            if not _should_skip(f)
        ]

        print(
            f"  Found {len(pp_files)} .pp, {len(rb_files)} .rb, "
            f"{len(yaml_files)} .yaml/.yml files in {dir_path.name}/"
        )

        # --- .pp manifests ---
        for pp_file in pp_files:
            chunks = chunk_puppet_file(pp_file)
            if chunks:
                for chunk in chunks:
                    chunk["metadata"]["filepath"] = str(pp_file)
                    chunk["metadata"]["filename"] = pp_file.name
                    chunk["metadata"]["relative_path"] = str(
                        pp_file.relative_to(dir_path)
                    )
                    chunk["metadata"]["language"] = "puppet"
                results.append((pp_file, "puppet", chunks))

        # --- .rb files ---
        for rb_file in rb_files:
            chunks = chunk_ruby_file(rb_file)
            if chunks:
                for chunk in chunks:
                    chunk["metadata"]["filepath"] = str(rb_file)
                    chunk["metadata"]["filename"] = rb_file.name
                    chunk["metadata"]["relative_path"] = str(
                        rb_file.relative_to(dir_path)
                    )
                    chunk["metadata"]["language"] = "ruby"
                results.append((rb_file, "puppet", chunks))

        # --- Hiera YAML ---
        for yaml_file in yaml_files:
            chunks = chunk_yaml_file(yaml_file)
            if chunks:
                for chunk in chunks:
                    chunk["metadata"]["filepath"] = str(yaml_file)
                    chunk["metadata"]["filename"] = yaml_file.name
                    chunk["metadata"]["relative_path"] = str(
                        yaml_file.relative_to(dir_path)
                    )
                    chunk["metadata"]["language"] = "yaml"
                results.append((yaml_file, "puppet", chunks))

    return results
