"""
JavaScript/TypeScript Code Collector for RAG - AST-based chunking using tree-sitter.

Indexes JS/TS/JSX/TSX code at semantic boundaries:
- Classes and their methods
- Named function declarations
- Exported arrow functions / const-assigned arrow functions (React components, etc.)
"""

from pathlib import Path
from typing import List, Dict, Tuple, Optional
import tree_sitter_languages

SKIP_DIRS = {
    "node_modules",
    "dist",
    "build",
    "output",
    ".git",
    "__pycache__",
    ".next",
    ".turbo",
    "coverage",
}

EXTENSIONS = {
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",  # tree-sitter-languages uses 'javascript' for jsx too
}


def extract_text(node, source_code: bytes) -> str:
    return source_code[node.start_byte : node.end_byte].decode(
        "utf-8", errors="replace"
    )


def find_child_of_type(node, *types) -> Optional[object]:
    for child in node.children:
        if child.type in types:
            return child
    return None


def extract_identifier(node, source_code: bytes) -> str:
    """Extract the first identifier or type_identifier child as a name."""
    child = find_child_of_type(
        node, "identifier", "type_identifier", "property_identifier"
    )
    if child:
        return extract_text(child, source_code)
    return ""


def extract_jsdoc(node, source_code: bytes) -> str:
    """
    Extract JSDoc comment immediately before a node.
    Checks prev_sibling for block_comment starting with '/**'.
    """
    if node.prev_sibling and node.prev_sibling.type == "comment":
        text = extract_text(node.prev_sibling, source_code).strip()
        if text.startswith("/**"):
            return text
    return ""


def chunk_js_ts_file(filepath: Path) -> List[Dict]:
    """
    Parse a JS/TS/JSX/TSX file with tree-sitter and extract semantic chunks.

    Returns list of chunks, each with:
    - content: the code text (with jsdoc prepended if present)
    - metadata: file, class, function, line numbers, chunk_type, etc.
    """
    ext = filepath.suffix.lower()
    language = EXTENSIONS.get(ext)
    if language is None:
        return []

    try:
        with open(filepath, "rb") as f:
            source_code = f.read()
    except Exception as e:
        print(f"⚠️  Error reading {filepath}: {e}")
        return []

    try:
        parser = tree_sitter_languages.get_parser(language)
        tree = parser.parse(source_code)
    except Exception as e:
        print(f"⚠️  Error parsing {filepath}: {e}")
        return []

    chunks = []

    def make_chunk(
        node, chunk_type: str, name: str, class_name: str = "", jsdoc: str = ""
    ) -> Dict:
        code_text = extract_text(node, source_code)
        if jsdoc:
            code_text = jsdoc + "\n" + code_text
        meta = {
            "chunk_type": chunk_type,
            "function_name": name,
            "class_name": class_name,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "has_jsdoc": bool(jsdoc),
        }
        if not class_name:
            del meta["class_name"]
        return {"content": code_text, "metadata": meta}

    def traverse(node, current_class: str = ""):
        # ── Class declaration ──────────────────────────────────────────────
        if node.type == "class_declaration":
            name = extract_identifier(node, source_code)
            jsdoc = extract_jsdoc(node, source_code)
            chunks.append(make_chunk(node, "class", name, jsdoc=jsdoc))
            for child in node.children:
                traverse(child, current_class=name)
            return  # children handled above

        # ── Method inside a class ──────────────────────────────────────────
        elif node.type == "method_definition":
            name = extract_identifier(node, source_code)
            jsdoc = extract_jsdoc(node, source_code)
            chunks.append(
                make_chunk(node, "method", name, class_name=current_class, jsdoc=jsdoc)
            )
            # Don't recurse into method bodies for nested classes (rare)
            return

        # ── Named function declaration ─────────────────────────────────────
        elif node.type == "function_declaration":
            name = extract_identifier(node, source_code)
            jsdoc = extract_jsdoc(node, source_code)
            chunk_type = "method" if current_class else "function"
            chunks.append(
                make_chunk(
                    node, chunk_type, name, class_name=current_class, jsdoc=jsdoc
                )
            )
            return

        # ── export statement — unwrap and handle contents ──────────────────
        elif node.type == "export_statement":
            # Look for class or function directly inside
            for child in node.children:
                if child.type == "class_declaration":
                    name = extract_identifier(child, source_code)
                    jsdoc = extract_jsdoc(node, source_code)
                    chunks.append(make_chunk(child, "class", name, jsdoc=jsdoc))
                    for grandchild in child.children:
                        traverse(grandchild, current_class=name)
                    return
                elif child.type == "function_declaration":
                    name = extract_identifier(child, source_code)
                    jsdoc = extract_jsdoc(node, source_code)
                    chunks.append(make_chunk(child, "function", name, jsdoc=jsdoc))
                    return
                elif child.type in ("lexical_declaration", "variable_declaration"):
                    # export const Foo = () => ...
                    _handle_var_decl(child, node)
                    return
            # Fall through to recurse for other export shapes
            for child in node.children:
                traverse(child, current_class)

        # ── const/let/var with arrow function ─────────────────────────────
        elif node.type in ("lexical_declaration", "variable_declaration"):
            _handle_var_decl(node, None)
            return

        # ── Default: recurse ───────────────────────────────────────────────
        else:
            for child in node.children:
                traverse(child, current_class)

    def _handle_var_decl(decl_node, parent_export_node):
        """
        Handle:  const Foo = () => { ... }   or   const foo = function() { ... }
        Only creates a chunk when the RHS is an arrow_function or function_expression.
        """
        for declarator in decl_node.children:
            if declarator.type != "variable_declarator":
                continue
            # Name is first identifier child
            name_node = find_child_of_type(declarator, "identifier", "type_identifier")
            if not name_node:
                continue
            name = extract_text(name_node, source_code)

            # Value is arrow_function or function_expression
            value = None
            for child in declarator.children:
                if child.type in ("arrow_function", "function_expression", "function"):
                    value = child
                    break
            if value is None:
                continue

            # Only chunk if it has a body (not just a signature)
            has_body = any(
                c.type in ("statement_block", "expression") for c in value.children
            )
            if not has_body:
                continue

            jsdoc = ""
            if parent_export_node:
                jsdoc = extract_jsdoc(parent_export_node, source_code)
            if not jsdoc:
                jsdoc = extract_jsdoc(decl_node, source_code)

            # Use the full declarator as the chunk content for context
            code_text = extract_text(decl_node, source_code)
            if jsdoc:
                code_text = jsdoc + "\n" + code_text

            chunks.append(
                {
                    "content": code_text,
                    "metadata": {
                        "chunk_type": "function",
                        "function_name": name,
                        "start_line": decl_node.start_point[0] + 1,
                        "end_line": decl_node.end_point[0] + 1,
                        "has_jsdoc": bool(jsdoc),
                    },
                }
            )

    traverse(tree.root_node)
    return chunks


def collect_js_ts_code(
    directory_paths: List[str],
) -> List[Tuple[Path, str, List[Dict]]]:
    """
    Collect JS/TS/JSX/TSX files from specified directories and chunk them.

    Args:
        directory_paths: List of directory paths to index recursively

    Returns:
        List of (filepath, source_name, chunks) tuples
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

        # Collect all matching files, skipping known noise dirs
        all_files = []
        for ext in EXTENSIONS:
            for f in dir_path.rglob(f"*{ext}"):
                if not any(skip in f.parts for skip in SKIP_DIRS):
                    all_files.append(f)

        print(f"  Found {len(all_files)} JS/TS files in {dir_path.name}")

        for src_file in all_files:
            if not src_file.is_file():
                continue
            chunks = chunk_js_ts_file(src_file)
            if chunks:
                for chunk in chunks:
                    chunk["metadata"]["filepath"] = str(src_file)
                    chunk["metadata"]["filename"] = src_file.name
                    chunk["metadata"]["relative_path"] = str(
                        src_file.relative_to(dir_path)
                    )
                    chunk["metadata"]["language"] = EXTENSIONS.get(
                        src_file.suffix.lower(), "javascript"
                    )

                results.append((src_file, "js_ts", chunks))

    return results
