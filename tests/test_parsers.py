"""
Parser/collector tests for the RAG system.

Covers:
  - puppet_collector: .pp manifest chunking (class/define/function/fallback)
  - puppet_collector: .rb Ruby chunking (module/class/method/fallback)
  - puppet_collector: .yaml Hiera data chunking
  - puppet_collector: collect_puppet_code integration
  - php_code_collector: class/method/function chunking
  - python_code_collector: class/function/method chunking
  - js_ts_code_collector: class/function/method chunking
  - archive_chunker: session-based archive chunking
"""

import sys
from pathlib import Path

# Make scripts/ importable from the tests/ directory
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import pytest

# ══════════════════════════════════════════════════════════════════════════════
#  puppet_collector
# ══════════════════════════════════════════════════════════════════════════════

from puppet_collector import (
    chunk_puppet_file,
    chunk_ruby_file,
    chunk_yaml_file,
    collect_puppet_code,
)


class TestPuppetManifestChunker:
    def test_simple_class(self, tmp_path):
        pp = tmp_path / "init.pp"
        pp.write_text(
            "class mymodule {\n  file { '/tmp/test': ensure => present }\n}\n"
        )
        chunks = chunk_puppet_file(pp)
        assert len(chunks) == 1
        assert chunks[0]["metadata"]["chunk_type"] == "class"
        assert chunks[0]["metadata"]["puppet_name"] == "mymodule"
        assert "file { '/tmp/test'" in chunks[0]["content"]

    def test_class_with_parameters(self, tmp_path):
        pp = tmp_path / "service.pp"
        pp.write_text(
            "class mymodule::service (\n"
            "  String $ensure = 'running',\n"
            "  Boolean $enable = true,\n"
            ") {\n"
            "  service { 'myservice': ensure => $ensure }\n"
            "}\n"
        )
        chunks = chunk_puppet_file(pp)
        assert len(chunks) == 1
        assert chunks[0]["metadata"]["chunk_type"] == "class"
        assert chunks[0]["metadata"]["puppet_name"] == "mymodule::service"

    def test_define_type(self, tmp_path):
        pp = tmp_path / "user.pp"
        pp.write_text(
            "define mymodule::user (\n"
            "  String $username,\n"
            "  Enum['present','absent'] $ensure = 'present',\n"
            ") {\n"
            "  user { $username: ensure => $ensure }\n"
            "}\n"
        )
        chunks = chunk_puppet_file(pp)
        assert len(chunks) == 1
        assert chunks[0]["metadata"]["chunk_type"] == "define"
        assert chunks[0]["metadata"]["puppet_name"] == "mymodule::user"

    def test_function_definition(self, tmp_path):
        pp = tmp_path / "myfunc.pp"
        pp.write_text(
            "function mymodule::greet(String $name) >> String {\n"
            '  "Hello, ${name}!"\n'
            "}\n"
        )
        chunks = chunk_puppet_file(pp)
        assert len(chunks) == 1
        assert chunks[0]["metadata"]["chunk_type"] == "function"
        assert chunks[0]["metadata"]["puppet_name"] == "mymodule::greet"

    def test_multiple_blocks_in_one_file(self, tmp_path):
        pp = tmp_path / "multi.pp"
        pp.write_text(
            "class multi::one {\n  notify { 'one': }\n}\n\n"
            "class multi::two {\n  notify { 'two': }\n}\n"
        )
        chunks = chunk_puppet_file(pp)
        assert len(chunks) == 2
        names = {c["metadata"]["puppet_name"] for c in chunks}
        assert names == {"multi::one", "multi::two"}

    def test_comment_block_prepended(self, tmp_path):
        pp = tmp_path / "commented.pp"
        pp.write_text(
            "# This class manages something.\n"
            "# It does important things.\n"
            "class commented::class {\n"
            "}\n"
        )
        chunks = chunk_puppet_file(pp)
        assert len(chunks) == 1
        assert "This class manages something" in chunks[0]["content"]
        assert chunks[0]["metadata"]["has_comment"] is True

    def test_fallback_to_whole_file_for_plain_resources(self, tmp_path):
        """Files with only resource declarations (no class/define) → whole-file chunk."""
        pp = tmp_path / "resources.pp"
        pp.write_text(
            "file { '/etc/motd':\n"
            "  ensure  => present,\n"
            "  content => 'Hello',\n"
            "}\n"
        )
        chunks = chunk_puppet_file(pp)
        assert len(chunks) == 1
        assert chunks[0]["metadata"]["chunk_type"] == "file"

    def test_empty_file_returns_empty(self, tmp_path):
        pp = tmp_path / "empty.pp"
        pp.write_text("")
        assert chunk_puppet_file(pp) == []

    def test_metadata_includes_filepath(self, tmp_path):
        pp = tmp_path / "check.pp"
        pp.write_text("class check {}\n")
        chunks = chunk_puppet_file(pp)
        # filepath is injected by collect_puppet_code, not chunk_puppet_file,
        # but start_line/end_line are always present.
        assert "start_line" in chunks[0]["metadata"]
        assert "end_line" in chunks[0]["metadata"]

    def test_nested_braces_do_not_confuse_parser(self, tmp_path):
        """Brace depth tracking must handle nested resource blocks."""
        pp = tmp_path / "nested.pp"
        pp.write_text(
            "class nested::test {\n"
            "  case $facts['os']['family'] {\n"
            "    'Debian': { package { 'wget': ensure => present } }\n"
            "    default:  { package { 'wget': ensure => absent } }\n"
            "  }\n"
            "}\n"
        )
        chunks = chunk_puppet_file(pp)
        assert len(chunks) == 1
        assert chunks[0]["metadata"]["chunk_type"] == "class"


class TestRubyChunker:
    def test_simple_module(self, tmp_path):
        rb = tmp_path / "my_module.rb"
        rb.write_text(
            "module MyModule\n" "  def self.hello\n" "    'hello'\n" "  end\n" "end\n"
        )
        chunks = chunk_ruby_file(rb)
        types = {c["metadata"]["chunk_type"] for c in chunks}
        assert "module" in types

    def test_class_with_methods(self, tmp_path):
        rb = tmp_path / "my_class.rb"
        rb.write_text(
            "class MyClass\n"
            "  def initialize(name)\n"
            "    @name = name\n"
            "  end\n\n"
            "  def greet\n"
            '    "Hello #{@name}"\n'
            "  end\n"
            "end\n"
        )
        chunks = chunk_ruby_file(rb)
        chunk_types = [c["metadata"]["chunk_type"] for c in chunks]
        assert "class" in chunk_types
        assert "method" in chunk_types

    def test_singleton_method(self, tmp_path):
        rb = tmp_path / "singleton.rb"
        rb.write_text(
            "class Util\n" "  def self.create(x)\n" "    new(x)\n" "  end\n" "end\n"
        )
        chunks = chunk_ruby_file(rb)
        method_chunks = [c for c in chunks if c["metadata"]["chunk_type"] == "method"]
        assert len(method_chunks) >= 1
        names = [c["metadata"]["ruby_name"] for c in method_chunks]
        assert any("self.create" in n for n in names)

    def test_standalone_method(self, tmp_path):
        rb = tmp_path / "standalone.rb"
        rb.write_text("def my_function(x)\n" "  x * 2\n" "end\n")
        chunks = chunk_ruby_file(rb)
        assert len(chunks) >= 1
        method_chunks = [c for c in chunks if c["metadata"]["chunk_type"] == "method"]
        assert any(c["metadata"]["ruby_name"] == "my_function" for c in method_chunks)

    def test_puppet_parser_functions_module(self, tmp_path):
        """Puppet custom Ruby functions follow a specific module pattern."""
        rb = tmp_path / "my_func.rb"
        rb.write_text(
            "require 'yaml'\n\n"
            "module Puppet::Parser::Functions\n"
            "  newfunction(:my_func, type: :rvalue) do |args|\n"
            "    args[0].upcase\n"
            "  end\n"
            "end\n"
        )
        chunks = chunk_ruby_file(rb)
        module_chunks = [c for c in chunks if c["metadata"]["chunk_type"] == "module"]
        assert len(module_chunks) == 1
        # Should NOT produce phantom extra chunks from keyword tokens
        assert len(chunks) == 1

    def test_fallback_for_small_utility_file(self, tmp_path):
        """Simple Facter fact files with no class/module → whole-file fallback."""
        rb = tmp_path / "facter_fact.rb"
        rb.write_text(
            "require 'facter'\n"
            "Facter.add('my_fact') do\n"
            "  setcode { 'value' }\n"
            "end\n"
        )
        chunks = chunk_ruby_file(rb)
        assert len(chunks) >= 1
        # Either extracted methods/modules or fell back to file chunk
        for c in chunks:
            assert c["metadata"]["chunk_type"] in ("module", "class", "method", "file")

    def test_empty_ruby_file_returns_empty(self, tmp_path):
        rb = tmp_path / "empty.rb"
        rb.write_text("")
        assert chunk_ruby_file(rb) == []


class TestYamlChunker:
    def test_yaml_whole_file_chunk(self, tmp_path):
        yaml = tmp_path / "common.yaml"
        yaml.write_text(
            "---\n"
            "profile::base::ntp_servers:\n"
            "  - 0.pool.ntp.org\n"
            "  - 1.pool.ntp.org\n"
        )
        chunks = chunk_yaml_file(yaml)
        assert len(chunks) == 1
        assert chunks[0]["metadata"]["chunk_type"] == "hiera_data"
        assert "ntp_servers" in chunks[0]["content"]

    def test_empty_yaml_returns_empty(self, tmp_path):
        yaml = tmp_path / "empty.yaml"
        yaml.write_text("")
        assert chunk_yaml_file(yaml) == []


class TestCollectPuppetCode:
    def test_collect_from_directory(self, tmp_path):
        manifests = tmp_path / "manifests"
        manifests.mkdir()
        (manifests / "init.pp").write_text("class mymod {}\n")
        (manifests / "service.pp").write_text("class mymod::service {}\n")

        lib = tmp_path / "lib"
        lib.mkdir()
        (lib / "helper.rb").write_text(
            "module MyHelper\n  def self.help; 42; end\nend\n"
        )

        data = tmp_path / "data"
        data.mkdir()
        (data / "common.yaml").write_text("---\nkey: value\n")

        results = collect_puppet_code([str(tmp_path)])
        assert len(results) >= 4

        sources = {source for _, source, _ in results}
        assert sources == {"puppet"}

        all_chunks = [c for _, _, chunks in results for c in chunks]
        meta_languages = {c["metadata"]["language"] for c in all_chunks}
        assert "puppet" in meta_languages
        assert "ruby" in meta_languages
        assert "yaml" in meta_languages

    def test_collect_skips_spec_directory(self, tmp_path):
        spec = tmp_path / "spec"
        spec.mkdir()
        (spec / "init_spec.rb").write_text(
            "require 'spec_helper'\ndescribe 'mymod' do\n  it { is_expected.to compile }\nend\n"
        )
        manifests = tmp_path / "manifests"
        manifests.mkdir()
        (manifests / "init.pp").write_text("class mymod {}\n")

        results = collect_puppet_code([str(tmp_path)])
        filenames = [fp.name for fp, _, _ in results]
        assert "init_spec.rb" not in filenames

    def test_collect_missing_directory_skips_gracefully(self, tmp_path, capsys):
        results = collect_puppet_code([str(tmp_path / "does_not_exist")])
        assert results == []
        captured = capsys.readouterr()
        assert "not found" in captured.out

    def test_collect_metadata_fields(self, tmp_path):
        (tmp_path / "test.pp").write_text("class meta::test {}\n")
        results = collect_puppet_code([str(tmp_path)])
        assert len(results) == 1
        _, _, chunks = results[0]
        meta = chunks[0]["metadata"]
        assert "filepath" in meta
        assert "filename" in meta
        assert "relative_path" in meta
        assert "language" in meta


# ══════════════════════════════════════════════════════════════════════════════
#  php_code_collector
# ══════════════════════════════════════════════════════════════════════════════

from php_code_collector import chunk_php_file, collect_php_code


class TestPhpCollector:
    def test_simple_class(self, tmp_path):
        php = tmp_path / "Simple.php"
        php.write_text(
            "<?php\nnamespace App;\n\nclass SimpleClass {\n"
            "    public function hello(): string {\n"
            "        return 'hello';\n"
            "    }\n"
            "}\n"
        )
        chunks = chunk_php_file(php)
        types = [c["metadata"]["chunk_type"] for c in chunks]
        assert "class" in types or "method" in types

    def test_standalone_function(self, tmp_path):
        php = tmp_path / "helpers.php"
        php.write_text(
            "<?php\nfunction doSomething(string $x): string {\n"
            "    return strtolower($x);\n"
            "}\n"
        )
        chunks = chunk_php_file(php)
        assert any(c["metadata"]["chunk_type"] == "function" for c in chunks)

    def test_empty_file_returns_empty(self, tmp_path):
        php = tmp_path / "empty.php"
        php.write_text("")
        assert chunk_php_file(php) == []

    def test_collect_php_code_from_directory(self, tmp_path):
        (tmp_path / "A.php").write_text("<?php\nclass A {}\n")
        (tmp_path / "B.php").write_text("<?php\nclass B {}\n")
        results = collect_php_code([str(tmp_path)])
        assert len(results) == 2
        sources = {source for _, source, _ in results}
        assert sources == {"code"}

    def test_collect_skips_missing_directory(self, tmp_path, capsys):
        results = collect_php_code([str(tmp_path / "no_such_dir")])
        assert results == []


# ══════════════════════════════════════════════════════════════════════════════
#  python_code_collector
# ══════════════════════════════════════════════════════════════════════════════

from python_code_collector import chunk_python_file, collect_python_code


class TestPythonCollector:
    def test_class_chunk(self, tmp_path):
        py = tmp_path / "mymodule.py"
        py.write_text(
            "class MyClass:\n"
            '    """A simple class."""\n'
            "    def __init__(self, x):\n"
            "        self.x = x\n\n"
            "    def double(self):\n"
            "        return self.x * 2\n"
        )
        chunks = chunk_python_file(py)
        types = [c["metadata"]["chunk_type"] for c in chunks]
        assert "class" in types
        assert "method" in types

    def test_top_level_function(self, tmp_path):
        py = tmp_path / "funcs.py"
        py.write_text(
            "def add(a, b):\n" '    """Add two numbers."""\n' "    return a + b\n"
        )
        chunks = chunk_python_file(py)
        assert any(c["metadata"]["chunk_type"] == "function" for c in chunks)

    def test_empty_file_returns_empty(self, tmp_path):
        py = tmp_path / "empty.py"
        py.write_text("")
        assert chunk_python_file(py) == []

    def test_collect_python_code_from_directory(self, tmp_path):
        (tmp_path / "a.py").write_text("class A:\n    def method(self): pass\n")
        (tmp_path / "b.py").write_text("def standalone(): pass\n")
        results = collect_python_code([str(tmp_path)])
        assert len(results) == 2

    def test_collect_skips_venv(self, tmp_path):
        venv = tmp_path / ".venv" / "lib"
        venv.mkdir(parents=True)
        (venv / "helper.py").write_text("class InVenv: pass\n")
        (tmp_path / "real.py").write_text("class Real: pass\n")
        results = collect_python_code([str(tmp_path)])
        filenames = [fp.name for fp, _, _ in results]
        assert "helper.py" not in filenames
        assert "real.py" in filenames


# ══════════════════════════════════════════════════════════════════════════════
#  js_ts_code_collector
# ══════════════════════════════════════════════════════════════════════════════

from js_ts_code_collector import chunk_js_ts_file, collect_js_ts_code


class TestJsTsCollector:
    def test_typescript_class(self, tmp_path):
        ts = tmp_path / "MyClass.ts"
        ts.write_text(
            "export class MyClass {\n"
            "  constructor(private name: string) {}\n\n"
            "  greet(): string {\n"
            "    return `Hello ${this.name}`;\n"
            "  }\n"
            "}\n"
        )
        chunks = chunk_js_ts_file(ts)
        assert len(chunks) >= 1
        types = [c["metadata"]["chunk_type"] for c in chunks]
        assert "class" in types or "method" in types

    def test_typescript_function_declaration(self, tmp_path):
        ts = tmp_path / "utils.ts"
        ts.write_text(
            "export function formatDate(date: Date): string {\n"
            "  return date.toISOString();\n"
            "}\n"
        )
        chunks = chunk_js_ts_file(ts)
        assert any(c["metadata"]["chunk_type"] == "function" for c in chunks)

    def test_javascript_arrow_function(self, tmp_path):
        js = tmp_path / "component.js"
        js.write_text("const MyComponent = () => {\n" "  return 'hello';\n" "};\n")
        chunks = chunk_js_ts_file(js)
        assert len(chunks) >= 1
        assert any(c["metadata"]["function_name"] == "MyComponent" for c in chunks)

    def test_unsupported_extension_returns_empty(self, tmp_path):
        f = tmp_path / "data.json"
        f.write_text('{"key": "value"}')
        assert chunk_js_ts_file(f) == []

    def test_collect_skips_node_modules(self, tmp_path):
        nm = tmp_path / "node_modules" / "pkg"
        nm.mkdir(parents=True)
        (nm / "index.ts").write_text("export class Pkg {}")
        (tmp_path / "app.ts").write_text("export class App {}")
        results = collect_js_ts_code([str(tmp_path)])
        filenames = [fp.name for fp, _, _ in results]
        assert "index.ts" not in filenames
        assert "app.ts" in filenames

    def test_collect_missing_directory_skips_gracefully(self, tmp_path, capsys):
        results = collect_js_ts_code([str(tmp_path / "missing")])
        assert results == []


# ══════════════════════════════════════════════════════════════════════════════
#  archive_chunker
# ══════════════════════════════════════════════════════════════════════════════

from archive_chunker import chunk_working_memory_archive


class TestArchiveChunker:
    def test_single_session(self, tmp_path):
        md = tmp_path / "working-memory.md"
        md.write_text(
            "### 2026-01-15 (Session 1)\n"
            "Worked on feature X. Implemented authentication flow.\n"
        )
        chunks = chunk_working_memory_archive(md)
        assert len(chunks) == 1
        assert chunks[0]["metadata"]["session_number"] == 1
        assert chunks[0]["metadata"]["session_date"] == "2026-01-15"
        assert chunks[0]["metadata"]["chunk_type"] == "session"

    def test_multiple_sessions(self, tmp_path):
        md = tmp_path / "working-memory.md"
        md.write_text(
            "### 2026-01-15 (Session 1)\n"
            "First session content.\n\n"
            "### 2026-01-16 (Session 2)\n"
            "Second session content.\n\n"
            "### 2026-01-17 (Session 3)\n"
            "Third session content.\n"
        )
        chunks = chunk_working_memory_archive(md)
        assert len(chunks) == 3
        session_numbers = [c["metadata"]["session_number"] for c in chunks]
        assert session_numbers == [1, 2, 3]

    def test_content_preserved(self, tmp_path):
        md = tmp_path / "working-memory.md"
        md.write_text(
            "### 2026-02-01 (Session 5)\n"
            "Investigated the OAuth redirect bug.\n"
            "Root cause: missing state parameter.\n"
        )
        chunks = chunk_working_memory_archive(md)
        assert "OAuth redirect bug" in chunks[0]["content"]
        assert "state parameter" in chunks[0]["content"]

    def test_empty_file_returns_empty(self, tmp_path):
        md = tmp_path / "empty.md"
        md.write_text("")
        chunks = chunk_working_memory_archive(md)
        assert chunks == []

    def test_no_sessions_returns_empty(self, tmp_path):
        """File with content but no session headers → no chunks."""
        md = tmp_path / "notes.md"
        md.write_text("Just some notes with no session headers.\n")
        chunks = chunk_working_memory_archive(md)
        assert chunks == []
