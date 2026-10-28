"""Describe a paused program's values for the variables view, as JSON.

Each value is `{"name", "type", "value"}`, plus a `ref` number when it has children (items of
a container, attributes of an object) that the view can ask for later. Everything here runs in
the paused thread that owns the values: `repr` can run the program's own code.
"""

from __future__ import annotations

import reprlib
import types
from typing import Callable, Iterable

MAX_CHILDREN = 300

LEAVES = (int, float, complex, bool, str, bytes, bytearray, type(None), range)
HIDDEN_GLOBALS = (types.ModuleType, types.FunctionType, types.BuiltinFunctionType, type)

_repr = reprlib.Repr(maxlevel=2, maxdict=8, maxlist=10, maxtuple=10, maxset=10, maxstring=160, maxother=160)


def describe(name: str, value: object, ref: Callable[[object], int]) -> dict:
    item = {"name": name, "type": type(value).__qualname__, "value": safe_repr(value)}
    if has_children(value):
        item["ref"] = ref(value)
    return item


def describe_all(pairs: Iterable[tuple[str, object]], ref: Callable[[object], int]) -> list[dict]:
    items = []
    for index, (name, value) in enumerate(pairs):
        if index == MAX_CHILDREN:
            items.append({"name": "…", "value": "more items not shown"})
            break
        items.append(describe(name, value, ref))
    return items


def safe_repr(value: object) -> str:
    try:
        return _repr.repr(value)
    except Exception as error:  # a broken __repr__ must not break the pause
        return f"<repr failed: {type(error).__name__}: {error}>"


def has_children(value: object) -> bool:
    if isinstance(value, LEAVES) or callable(value) and not isinstance(value, type):
        return False
    if isinstance(value, (dict, list, tuple, set, frozenset)):
        return len(value) > 0
    if isinstance(value, types.ModuleType):
        return False
    return bool(attributes(value))


def children(value: object) -> Iterable[tuple[str, object]]:
    if isinstance(value, dict):
        return ((safe_repr(key), item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return ((f"[{index}]", item) for index, item in enumerate(value))
    if isinstance(value, (set, frozenset)):
        return ((f"[{index}]", item) for index, item in enumerate(value))
    return attributes(value).items()


def attributes(value: object) -> dict[str, object]:
    """The object's own attributes: its `__dict__` and `__slots__` (dunders left out)."""
    found = {}
    try:
        found.update(vars(value))
    except TypeError:
        pass
    for cls in type(value).__mro__:
        for slot in getattr(cls, "__slots__", ()):
            try:
                found.setdefault(slot, getattr(value, slot))
            except AttributeError:
                pass
    return {name: item for name, item in found.items() if not (name.startswith("__") and name.endswith("__"))}


def user_globals(namespace: dict) -> Iterable[tuple[str, object]]:
    """A module's own data: no dunders, imported modules, functions or classes."""
    return ((name, value) for name, value in namespace.items() if not name.startswith("__") and not isinstance(value, HIDDEN_GLOBALS))
