"""Find the test files that exercise a module: most files of a library are not meant to run on
their own, so "run this file" means running the tests that import it.

A test file is `test_*.py` or `*_test.py` under the project root (pytest's default names). It uses
a module when it imports it, directly (`import pkg.mod`, `from pkg.mod import x`,
`from pkg import mod`, relative imports too) or through one of its submodules when the module is a
package's `__init__.py`.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

from luciole.analyzer import SKIP_DIRS, module_name


def is_test_file(path: Path) -> bool:
    return path.suffix == ".py" and (path.name.startswith("test_") or path.stem.endswith("_test"))


def find_test_files(root: Path):
    for directory, subdirectories, files in os.walk(root):
        subdirectories[:] = sorted(d for d in subdirectories if not d.startswith(".") and d not in SKIP_DIRS)
        for name in sorted(files):
            path = Path(directory, name)
            if is_test_file(path):
                yield path


def imported_modules(tree: ast.AST, package: str) -> set[str]:
    """Every module a file imports, as dotted names; `from a import b` gives both a and a.b."""
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".") if package else []
                parts = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
                base = ".".join([*parts, *([node.module] if node.module else [])])
            else:
                base = node.module or ""
            if base:
                modules.add(base)
            modules.update(f"{base}.{alias.name}" if base else alias.name for alias in node.names)
    return modules


def tests_for(file: Path, root: Path) -> list[Path]:
    """Test files under `root` that import the module in `file`, sorted."""
    module = module_name(file)
    if not module:
        return []
    is_package = file.name == "__init__.py"
    last = module.rsplit(".", 1)[-1]
    found = []
    for test in find_test_files(root):
        if test.resolve() == file.resolve():
            continue
        try:
            source = test.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if last not in source and "from ." not in source:
            continue  # cheap filter before parsing
        try:
            tree = ast.parse(source, str(test))
        except SyntaxError:
            continue
        test_module = module_name(test)
        package = test_module.rsplit(".", 1)[0] if "." in test_module else ""
        imports = imported_modules(tree, package)
        if module in imports or (is_package and any(name.startswith(module + ".") for name in imports)):
            found.append(test)
    return found
