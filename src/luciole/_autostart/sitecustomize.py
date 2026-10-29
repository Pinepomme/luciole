"""Start Luciole's recorder in every Python process whose environment turns it on.

This folder goes on PYTHONPATH (Luciole's switch in VS Code, or `luciole env`), so Python imports
this file at startup, as it would the environment's own sitecustomize. When the Luciole engine
runs (its port in LUCIOLE_LIVE_PORT, else in its file, see luciole/protocol.py), the process
reports its calls to it; when no engine answers, nothing is started, unless LUCIOLE_TRACE names
a directory to write a trace of each process in (see docs/trace-format.md). Turning Luciole off
is removing the folder from PYTHONPATH.

Programs must run the same with Luciole on: Luciole's errors are swallowed, and the sitecustomize
this file hides (Ubuntu's Python ships one, for instance) still runs.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def run_hidden_sitecustomize():
    """Run the sitecustomize module that would have been imported without this folder."""
    from importlib.machinery import PathFinder
    from importlib.util import module_from_spec

    path = [entry for entry in sys.path if os.path.abspath(entry or os.curdir) != HERE]
    spec = PathFinder.find_spec("sitecustomize", path)
    if spec is not None and spec.loader is not None:
        spec.loader.exec_module(module_from_spec(spec))


def drop_working_directory():
    """VS Code's switch prepends this folder and a separator to PYTHONPATH. When PYTHONPATH was
    empty, that leaves an empty entry, which Python reads as the working directory: remove it."""
    value = os.environ.get("PYTHONPATH", "")
    if value.endswith(os.pathsep) and os.path.abspath(value[: -len(os.pathsep)]) == HERE:
        os.environ["PYTHONPATH"] = value[: -len(os.pathsep)]
        index = sys.path.index(HERE) + 1 if HERE in sys.path else len(sys.path)
        if index < len(sys.path) and sys.path[index] == os.getcwd():
            del sys.path[index]


def is_luciole_run():
    """`luciole run` (or `python -m luciole run`) starts its own recorder, with its options."""
    argv = sys.orig_argv
    return any(os.path.basename(arg).removesuffix(".exe") == "luciole" and next_arg == "run" for arg, next_arg in zip(argv, argv[1:]))


# Same as luciole/protocol.py, which is not imported to read it: see import_bundled().
PROTOCOL = 1


def engine_port():
    """Port of the running engine, from its file (luciole.protocol.engine_port); None when none."""
    import json

    home = os.environ.get("LUCIOLE_HOME") or os.path.join(os.path.expanduser("~"), ".luciole")
    try:
        with open(os.path.join(home, f"engine-v{PROTOCOL}.json"), encoding="utf-8") as file:
            return int(json.load(file)["port"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def import_bundled(name):
    """Import luciole.<name> from the package around this folder (the same version as the view
    that turned auto-start on), without leaving it as `luciole` in sys.modules: the program may
    import a luciole of its own (another version, or the luciole repository's tests), which must
    be the one it gets. The modules imported keep working: they hold their own references."""

    def ours(key):
        return key == "luciole" or key.startswith("luciole.")

    saved = {key: module for key, module in sys.modules.items() if ours(key)}
    for key in saved:
        del sys.modules[key]
    package_parent = os.path.dirname(os.path.dirname(HERE))
    sys.path.insert(0, package_parent)
    try:
        from importlib import import_module

        return import_module(f"luciole.{name}")
    finally:
        sys.path.remove(package_parent)
        for key in [key for key in sys.modules if ours(key)]:
            del sys.modules[key]
        sys.modules.update(saved)


def start_luciole():
    if sys.version_info < (3, 12) or is_luciole_run():
        return
    import socket

    trace = os.environ.get("LUCIOLE_TRACE")  # a directory: one trace file per process
    port = os.environ.get("LUCIOLE_LIVE_PORT") or engine_port()
    connection = None
    if port:
        # Connect before importing the recorder: with no engine, starting costs nothing more.
        try:
            connection = socket.create_connection(("127.0.0.1", int(port)), timeout=2)
            connection.settimeout(None)
        except (OSError, ValueError):
            connection = None
    if connection is None and not trace:
        return
    import_bundled("recorder").start(port=port if connection else None, quiet=True, connection=connection, trace=trace)


drop_working_directory()
run_hidden_sitecustomize()
try:
    start_luciole()
except Exception:
    pass
