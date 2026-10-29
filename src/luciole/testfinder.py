"""Find the test files that exercise a module: most files of a library are not meant to run on
their own, so "run this file" means running the tests that import it.

A test file is `test_*.py` or `*_test.py` under the project root (pytest's default names). It uses
a module when it imports it, directly (`import pkg.mod`, `from pkg.mod import x`,
`from pkg import mod`, relative imports too) or through one of its submodules when the module is a
package's `__init__.py`. When no test imports a module directly, the ones that import it through
other project modules are used, closest first.

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

MAX_DEPTH = 10  # project modules followed from a test to the module


def is_test_file(path: Path) -> bool:
    return path.suffix == ".py" and (path.name.startswith("test_") or path.stem.endswith("_test"))


def python_files(root: Path):
    """The .py files under `root`, in a stable order, leaving out the directories the analyzer skips."""
    for directory, subdirectories, files in os.walk(root):
        subdirectories[:] = sorted(d for d in subdirectories if not d.startswith(".") and d not in SKIP_DIRS)
        for name in sorted(files):
            if name.endswith(".py"):
                yield Path(directory, name)


def find_test_files(root: Path):
    return (path for path in python_files(root) if is_test_file(path))


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


def packages_of(file: Path, roots: set[Path]) -> set[str]:
    """Names of the package a file's relative imports start from."""
    names = import_names(file, roots)
    return names if file.name == "__init__.py" else {name.rpartition(".")[0] for name in names}


def tests_for(file: Path, root: Path) -> list[Path]:
    """Test files under `root` that import the module in `file`, sorted. When none does, the
    closest ones that import it through other project modules (`MAX_DEPTH` of them at most)."""
    target = file.resolve()
    files = [path for path in python_files(root) if path.resolve() != target]
    tests = [path for path in files if is_test_file(path)]
    others = [path for path in files if not is_test_file(path)]
    roots = {root.resolve(), (root / "src").resolve()} | {import_root(test) for test in tests}
    parsed: dict[Path, set[str]] = {}

    def imports(path: Path, lasts: set[str]) -> set[str]:
        if path in parsed:
            return parsed[path]
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            source = ""
        if not any(last in source for last in lasts) and "from ." not in source:
            return set()  # cheap filter before parsing, and nothing cached: other names may match
        try:
            tree = ast.parse(source, str(path))
        except SyntaxError:
            tree = None
        parsed[path] = imported_modules(tree, packages_of(path, roots)) if tree else set()
        return parsed[path]

    def users(paths: list[Path], modules: set[str], packages: set[str]) -> list[Path]:
        """The files that import one of `modules`, or a submodule of one of `packages`."""
        lasts = {module.rpartition(".")[2] for module in modules}
        return [
            path
            for path in paths
            if modules & (found := imports(path, lasts)) or any(name.startswith(package + ".") for name in found for package in packages)
        ]

    modules = import_names(file, roots)
    packages = modules if file.name == "__init__.py" else set()
    seen: set[Path] = set()
    for _ in range(MAX_DEPTH):
        if not modules:
            return []
        found = users(tests, modules, packages)
        if found:
            return found
        reached = [path for path in users(others, modules, packages) if path not in seen]
        seen.update(reached)
        modules = {name for path in reached for name in import_names(path, roots)}
        packages = {name for path in reached if path.name == "__init__.py" for name in import_names(path, roots)}
    return []
