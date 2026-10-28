"""Start Luciole's recorder in every Python process whose environment turns it on.

This folder goes on PYTHONPATH (Luciole's switch in VS Code, or `luciole env`), so Python imports
this file at startup, as it would the environment's own sitecustomize. With LUCIOLE_LIVE_PORT set
and a Luciole view listening on that port, the process reports its calls to the view; when nothing
listens, nothing is started. Turning Luciole off is removing the folder from PYTHONPATH.

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


def start_luciole():
    port = os.environ.get("LUCIOLE_LIVE_PORT")
    if not port or sys.version_info < (3, 12) or is_luciole_run():
        return
    import socket

    # Connect before importing the recorder: with nothing listening, starting costs nothing more.
    try:
        connection = socket.create_connection(("127.0.0.1", int(port)), timeout=2)
    except (OSError, ValueError):
        return
    connection.settimeout(None)
    # The luciole package around this folder: the same version as the view that set the variables.
    package_parent = os.path.dirname(os.path.dirname(HERE))
    sys.path.insert(0, package_parent)
    try:
        from luciole import recorder
    finally:
        sys.path.remove(package_parent)
    recorder.start(port=port, quiet=True, connection=connection)


drop_working_directory()
run_hidden_sitecustomize()
try:
    start_luciole()
except Exception:
    pass
