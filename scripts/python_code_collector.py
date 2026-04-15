"""
Python Code Collector for RAG - AST-based chunking using tree-sitter.

Indexes Python code at semantic boundaries (classes, functions, methods).
"""

from pathlib import Path
from typing import List, Dict, Tuple
import tree_sitter_languages


def extract_text(node, source_code: bytes) -> str:
    """Extract text for a given node."""
    return source_code[node.start_byte : node.end_byte].decode("utf-8")


def extract_docstring(node, source_code: bytes) -> str:
    """
    Extract docstring if it exists as the first statement in a function/class.
    Returns empty string if no docstring found.
    """
    # Look for expression_statement > string as first child of block
    for child in node.children:
        if child.type == "block":
            for block_child in child.children:
                if block_child.type == "expression_statement":
                    for expr_child in block_child.children:
                        if expr_child.type == "string":
                            docstring = extract_text(expr_child, source_code)
                            return docstring.strip()
                    break
            break
    return ""


def extract_class_name(node, source_code: bytes) -> str:
    """Extract class name from class_definition node."""
    for child in node.children:
        if child.type == "identifier":
            return extract_text(child, source_code)
    return "UnknownClass"


def extract_function_name(node, source_code: bytes) -> str:
    """Extract function name from function_definition node."""
    for child in node.children:
        if child.type == "identifier":
            return extract_text(child, source_code)
    return "unknown_function"


def chunk_python_file(filepath: Path) -> List[Dict]:
    """
    Parse Python file with tree-sitter and extract semantic chunks.

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
    parser = tree_sitter_languages.get_parser("python")
    tree = parser.parse(source_code)

    chunks = []

    def traverse_node(node, current_class=None):
        """Recursively traverse AST and extract chunks."""

        # Class definition
        if node.type == "class_definition":
            class_name = extract_class_name(node, source_code)
            docstring = extract_docstring(node, source_code)

            # Create chunk for entire class
            code_text = extract_text(node, source_code)

            chunks.append(
                {
                    "content": code_text,
                    "metadata": {
                        "chunk_type": "class",
                        "class_name": class_name,
                        "start_line": node.start_point[0] + 1,
                        "end_line": node.end_point[0] + 1,
                        "has_docstring": bool(docstring),
                    },
                }
            )

            # Traverse children for methods
            for child in node.children:
                traverse_node(child, current_class=class_name)

        # Function definition (could be method if inside class, or top-level function)
        elif node.type == "function_definition":
            function_name = extract_function_name(node, source_code)
            docstring = extract_docstring(node, source_code)

            code_text = extract_text(node, source_code)

            chunk_type = "method" if current_class else "function"

            chunk_metadata = {
                "chunk_type": chunk_type,
                "function_name": function_name,
                "start_line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
                "has_docstring": bool(docstring),
            }

            if current_class:
                chunk_metadata["class_name"] = current_class

            chunks.append({"content": code_text, "metadata": chunk_metadata})

        # Traverse children (for nested structures)
        else:
            for child in node.children:
                traverse_node(child, current_class)

    # Start traversal from root
    traverse_node(tree.root_node)

    return chunks


def collect_python_code(
    directory_paths: List[str],
) -> List[Tuple[Path, str, List[Dict]]]:
    """
    Collect Python files from specified directories and chunk them.

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

        # Find all .py files recursively
        python_files = list(dir_path.rglob("*.py"))
        print(f"  Found {len(python_files)} Python files in {dir_path.name}")

        for py_file in python_files:
            # Skip common directories
            if any(
                skip in py_file.parts
                for skip in [".venv", "venv", "__pycache__", ".git", "node_modules"]
            ):
                continue

            chunks = chunk_python_file(py_file)
            if chunks:
                # Add filepath to each chunk's metadata
                for chunk in chunks:
                    chunk["metadata"]["filepath"] = str(py_file)
                    chunk["metadata"]["filename"] = py_file.name
                    chunk["metadata"]["relative_path"] = str(
                        py_file.relative_to(dir_path)
                    )

                results.append((py_file, "code", chunks))

    return results
