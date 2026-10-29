"""Find the test files that exercise a module: most files of a library are not meant to run on
their own, so "run this file" means running the tests that import it.

A test file is `test_*.py` or `*_test.py` under the project root (pytest's default names). It uses
a module when it imports it, directly (`import pkg.mod`, `from pkg.mod import x`,
`from pkg import mod`, relative imports too) or through one of its submodules when the module is a
package's `__init__.py`.

A module has a dotted name for each directory it can be imported from (the import roots): the
project root, `src/`, and the directories pytest puts test files' packages under. Namespace
packages (directories without `__init__.py`) are part of those names.
"""

from __future__ import annotations

import ast
import os
import sys
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


def import_root(file: Path) -> Path:
    """The directory above a file's regular packages: where pytest imports a test file from."""
    file = file.resolve()
    depth = len(module_name(file).split(".")) - (0 if file.name == "__init__.py" else 1)
    return file.parents[depth]


def import_names(file: Path, roots: set[Path]) -> set[str]:
    """Dotted names `file` can be imported as, from each import root it is under, and through its
    regular packages (`module_name`). That last one is left out when it starts like a standard
    library module and comes from no import root: `import types` is the standard library's, not
    src/ns/types.py's."""
    file = file.resolve()
    names = set()
    short = module_name(file)
    if short.partition(".")[0] not in sys.stdlib_module_names or import_root(file) in roots:
        names.add(short)
    for root in roots:
        if root not in file.parents:
            continue
        parts = list(file.relative_to(root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        if parts and all(part.isidentifier() for part in parts):
            names.add(".".join(parts))
    names.discard("")
    return names


def imported_modules(tree: ast.AST, packages: set[str]) -> set[str]:
    """Every module a file imports, as dotted names; `from a import b` gives both a and a.b.
    Relative imports are resolved from each of `packages`, the names the file's package may have."""
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                bases = set()
                for package in packages:
                    parts = package.split(".") if package else []
                    if node.level - 1 > len(parts):
                        continue
                    parts = parts[: len(parts) - (node.level - 1)]
                    bases.add(".".join([*parts, *([node.module] if node.module else [])]))
            else:
                bases = {node.module or ""}
            for base in bases:
                if base:
                    modules.add(base)
                modules.update(f"{base}.{alias.name}" if base else alias.name for alias in node.names)
    return modules


def tests_for(file: Path, root: Path) -> list[Path]:
    """Test files under `root` that import the module in `file`, sorted."""
    tests = [test for test in find_test_files(root) if test.resolve() != file.resolve()]
    roots = {root.resolve(), (root / "src").resolve()} | {import_root(test) for test in tests}
    modules = import_names(file, roots)
    if not modules:
        return []
    is_package = file.name == "__init__.py"
    lasts = {module.rpartition(".")[2] for module in modules}
    found = []
    for test in tests:
        try:
            source = test.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not any(last in source for last in lasts) and "from ." not in source:
            continue  # cheap filter before parsing
        try:
            tree = ast.parse(source, str(test))
        except SyntaxError:
            continue
        packages = {name.rpartition(".")[0] for name in import_names(test, roots)}
        imports = imported_modules(tree, packages)
        if modules & imports or (is_package and any(name.startswith(module + ".") for name in imports for module in modules)):
            found.append(test)
    return found
