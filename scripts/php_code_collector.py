"""
PHP Code Collector for RAG - AST-based chunking using tree-sitter.

Indexes PHP code at semantic boundaries (classes, functions, methods).
"""

from pathlib import Path
from typing import List, Dict, Tuple
import tree_sitter_languages


def extract_text(node, source_code: bytes) -> str:
    """Extract text for a given node."""
    return source_code[node.start_byte : node.end_byte].decode("utf-8")


def extract_docblock(node, source_code: bytes) -> str:
    """
    Extract docblock comment if it exists immediately before the node.
    Returns empty string if no docblock found.
    """
    # Look for comment nodes that appear before this node
    if node.prev_sibling and node.prev_sibling.type == "comment":
        comment_text = extract_text(node.prev_sibling, source_code)
        if comment_text.strip().startswith("/**"):
            return comment_text.strip()
    return ""


def extract_namespace(tree, source_code: bytes) -> str:
    """Extract namespace from the file if it exists."""
    root = tree.root_node
    for child in root.children:
        if child.type == "namespace_definition":
            for ns_child in child.children:
                if ns_child.type == "namespace_name":
                    return extract_text(ns_child, source_code)
    return ""


def extract_class_name(node, source_code: bytes) -> str:
    """Extract class name from class_declaration node."""
    for child in node.children:
        if child.type == "name":
            return extract_text(child, source_code)
    return "UnknownClass"


def extract_function_name(node, source_code: bytes) -> str:
    """Extract function name from function_definition or method_declaration node."""
    for child in node.children:
        if child.type == "name":
            return extract_text(child, source_code)
    return "unknown_function"


def chunk_php_file(filepath: Path) -> List[Dict]:
    """
    Parse PHP file with tree-sitter and extract semantic chunks.

    Returns list of chunks, each with:
    - content: the code text
    - metadata: file, class, function, line numbers, type, etc.
    """
    try:
        with open(filepath, "rb") as f:
            source_code = f.read()
    except Exception as e:
        print(f"⚠️  Error reading {filepath}: {e}")
        return []

    # Parse with tree-sitter
    parser = tree_sitter_languages.get_parser("php")
    tree = parser.parse(source_code)

    chunks = []
    namespace = extract_namespace(tree, source_code)

    def traverse_node(node, current_class=None):
        """Recursively traverse AST and extract chunks."""

        # Class declaration
        if node.type == "class_declaration":
            class_name = extract_class_name(node, source_code)
            docblock = extract_docblock(node, source_code)

            # Create chunk for entire class
            code_text = extract_text(node, source_code)
            if docblock:
                code_text = docblock + "\n" + code_text

            chunks.append(
                {
                    "content": code_text,
                    "metadata": {
                        "chunk_type": "class",
                        "class_name": class_name,
                        "namespace": namespace,
                        "start_line": node.start_point[0] + 1,
                        "end_line": node.end_point[0] + 1,
                        "has_docblock": bool(docblock),
                    },
                }
            )

            # Traverse children for methods
            for child in node.children:
                traverse_node(child, current_class=class_name)

        # Method declaration (inside a class)
        elif node.type == "method_declaration":
            method_name = extract_function_name(node, source_code)
            docblock = extract_docblock(node, source_code)

            code_text = extract_text(node, source_code)
            if docblock:
                code_text = docblock + "\n" + code_text

            chunks.append(
                {
                    "content": code_text,
                    "metadata": {
                        "chunk_type": "method",
                        "function_name": method_name,
                        "class_name": current_class or "Unknown",
                        "namespace": namespace,
                        "start_line": node.start_point[0] + 1,
                        "end_line": node.end_point[0] + 1,
                        "has_docblock": bool(docblock),
                    },
                }
            )

        # Function declaration (top-level or in namespace)
        elif node.type == "function_definition":
            function_name = extract_function_name(node, source_code)
            docblock = extract_docblock(node, source_code)

            code_text = extract_text(node, source_code)
            if docblock:
                code_text = docblock + "\n" + code_text

            chunks.append(
                {
                    "content": code_text,
                    "metadata": {
                        "chunk_type": "function",
                        "function_name": function_name,
                        "namespace": namespace,
                        "start_line": node.start_point[0] + 1,
                        "end_line": node.end_point[0] + 1,
                        "has_docblock": bool(docblock),
                    },
                }
            )

        # Traverse children
        else:
            for child in node.children:
                traverse_node(child, current_class)

    # Start traversal from root
    traverse_node(tree.root_node)

    return chunks


def collect_php_code(directory_paths: List[str]) -> List[Tuple[Path, str, List[Dict]]]:
    """
    Collect PHP files from specified directories and chunk them.

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

        # Find all .php files recursively
        php_files = list(dir_path.rglob("*.php"))
        print(f"  Found {len(php_files)} PHP files in {dir_path.name}")

        for php_file in php_files:
            # Skip vendor directories
            if "vendor" in php_file.parts:
                continue

            chunks = chunk_php_file(php_file)
            if chunks:
                # Add filepath to each chunk's metadata
                for chunk in chunks:
                    chunk["metadata"]["filepath"] = str(php_file)
                    chunk["metadata"]["filename"] = php_file.name
                    chunk["metadata"]["relative_path"] = str(
                        php_file.relative_to(dir_path)
                    )

                results.append((php_file, "code", chunks))

    return results
