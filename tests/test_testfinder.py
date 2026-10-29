"""Finding the tests that import a module, for Run Live on a library file."""

import json
import textwrap

from luciole.cli import main
from luciole import testfinder


def project(tmp_path, files):
    for name, source in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source))
    return tmp_path


FILES = {
    "pkg/__init__.py": "",
    "pkg/core.py": "def f():\n    return 1\n",
    "pkg/util.py": "def g():\n    return 2\n",
    "pkg/tests/__init__.py": "",
    "pkg/tests/test_relative.py": "from ..core import f\n",
    "tests/test_core.py": "from pkg.core import f\n",
    "tests/test_from_package.py": "from pkg import util\n",
    "tests/test_plain_import.py": "import pkg.util as u\n",
    "tests/test_mentions_core.py": "import json\n\nCORE = 'core'\n",
    "tests/test_broken.py": "from pkg.core import (\n",
    "tests/helpers.py": "from pkg.core import f\n",  # not a test file
    "venv/lib/test_core_copy.py": "from pkg.core import f\n",  # skipped directory
    "core_test.py": "import pkg.core\n",
}


def names(paths, root):
    return sorted(str(path.relative_to(root)).replace("\\", "/") for path in paths)


def test_finds_the_tests_that_import_a_module(tmp_path):
    root = project(tmp_path, FILES)
    assert names(testfinder.tests_for(root / "pkg" / "core.py", root), root) == ["core_test.py", "pkg/tests/test_relative.py", "tests/test_core.py"]
    assert names(testfinder.tests_for(root / "pkg" / "util.py", root), root) == ["tests/test_from_package.py", "tests/test_plain_import.py"]


def test_a_package_is_used_through_its_submodules(tmp_path):
    root = project(tmp_path, FILES)
    found = names(testfinder.tests_for(root / "pkg" / "__init__.py", root), root)
    assert found == ["core_test.py", "pkg/tests/test_relative.py", "tests/test_core.py", "tests/test_from_package.py", "tests/test_plain_import.py"]


def test_test_file_names():
    from pathlib import Path

    assert testfinder.is_test_file(Path("test_x.py")) and testfinder.is_test_file(Path("x_test.py"))
    assert not testfinder.is_test_file(Path("testing.py")) and not testfinder.is_test_file(Path("test_x.txt"))


def test_cli_prints_json(tmp_path, capsys):
    root = project(tmp_path, FILES)
    assert main(["tests", str(root / "pkg" / "util.py"), "--root", str(root), "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert [path.replace("\\", "/").rsplit("/", 2)[-2:] for path in printed] == [["tests", "test_from_package.py"], ["tests", "test_plain_import.py"]]


def test_follows_imports_through_project_modules(tmp_path):
    root = project(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/catalog.py": "ITEMS = []\n",
        "pkg/prices.py": "from .catalog import ITEMS\n",
        "pkg/page.py": "from pkg import prices\n",
        "pkg/core.py": "",
        "pkg/app.py": "import pkg.core\nimport pkg.page\n",
        "tests/test_page.py": "from pkg.page import render\n",
        "tests/test_app.py": "import pkg.app\n",
        "tests/test_core.py": "from pkg import core\n",
    })
    # Two modules away, the closest tests first: test_app is three away.
    assert names(testfinder.tests_for(root / "pkg" / "catalog.py", root), root) == ["tests/test_page.py"]
    # Direct importers only, when there are some.
    assert names(testfinder.tests_for(root / "pkg" / "core.py", root), root) == ["tests/test_core.py"]


def test_namespace_packages(tmp_path):
    root = project(tmp_path, {
        "src/ns/types.py": "X = 1\n",  # ns has no __init__.py: a namespace package
        "src/ns/sub/__init__.py": "",
        "src/ns/sub/mod.py": "Y = 2\n",
        "tests/test_stdlib_types.py": "import types\n",
        "tests/test_ns_types.py": "from ns import types\n",
        "tests/test_sub.py": "from ns.sub import mod\n",
    })
    assert names(testfinder.tests_for(root / "src" / "ns" / "types.py", root), root) == ["tests/test_ns_types.py"]
    assert names(testfinder.tests_for(root / "src" / "ns" / "sub" / "mod.py", root), root) == ["tests/test_sub.py"]


def test_skips_directories_the_gitignore_lists(tmp_path):
    root = project(tmp_path, {**FILES, ".gitignore": "runs\n", "runs/test_copy.py": "from pkg.core import f\n"})
    assert "runs/test_copy.py" not in names(testfinder.tests_for(root / "pkg" / "core.py", root), root)
    # A `!` pattern may bring back files under an ignored directory: then the .gitignore is not used.
    (root / ".gitignore").write_text("runs/\n!runs/test_copy.py\n")
    assert "runs/test_copy.py" in names(testfinder.tests_for(root / "pkg" / "core.py", root), root)
