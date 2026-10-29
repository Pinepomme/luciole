"""Describe a program's values: for the recorder's pause protocol, and as snapshots in traces.

For a paused program (see recorder.py), each value is `{"name", "type", "value"}`, plus a `ref`
number when it has children (items of a container, attributes of an object) that the engine's
client can ask for later.
For traces (see tracer.py), snapshot() captures a value as it is at one moment, capped in size.
Both use the same repr. Everything here runs in the thread that owns the values: `repr` can run
the program's own code.
"""

from __future__ import annotations

import itertools
import json
import re
import reprlib
import types
from typing import Callable, Iterable

MAX_CHILDREN = 300

LEAVES = (int, float, complex, bool, str, bytes, bytearray, type(None), range)
HIDDEN_GLOBALS = (types.ModuleType, types.FunctionType, types.BuiltinFunctionType, type)

_repr = reprlib.Repr()  # set one by one: Repr's keyword arguments are Python 3.12+
_repr.maxlevel, _repr.maxdict, _repr.maxlist, _repr.maxtuple, _repr.maxset = 2, 8, 10, 10, 10
_repr.maxstring = _repr.maxother = 160


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


# Snapshots, for traces

REPR_CHARS = 200  # characters of a snapshot's repr
ITEMS = 8  # items of a container kept in a snapshot
ITEM_CHARS = 60  # characters of each item's repr
IMMUTABLE = frozenset({int, float, complex, bool, str, bytes, type(None)})  # safe to snapshot later
CONTAINERS = frozenset({list, tuple, set, frozenset, dict})  # not subclasses: they have reprs of their own
REDACTED = "<redacted>"

# Names whose values are secrets: password, token, api_key, auth_header, session_id, a `key` word...
_SECRET = re.compile(r"passw(or)?d|secret|token|api_?key|credential|cookie|session|(^|_)auth(?!ors?(_|$))|(^|_)key(_|$)")
_secret_names: dict[str, bool] = {}


def is_secret(name: str) -> bool:
    try:
        return _secret_names[name]
    except KeyError:
        pass
    # camelCase words become snake_case ones: apiKey, authToken.
    found = bool(_SECRET.search(re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()))
    if len(_secret_names) < 10_000:
        _secret_names[name] = found
    return found


def snapshot(value: object, chars: int = REPR_CHARS, items: int = ITEMS) -> tuple[str, str, str | None, bool]:
    """`(type, repr, items, truncated)`: the type's name, a repr of at most `chars` characters,
    for a list, tuple, set or dict a JSON `{"len": n, "items": [...]}` of its first `items` items
    (dict items as `[key, value]` reprs, values under secret keys redacted), and whether
    anything was cut. Runs on every call at trace level 1: keep it cheap."""
    kind = type(value)
    if kind in CONTAINERS:
        try:
            return (kind.__qualname__, *container(value, chars, items))
        except Exception:  # changed size meanwhile
            pass
    text = plain_repr(value, chars) or safe_repr(value)
    if len(text) > chars:
        return kind.__qualname__, text[: chars - 1] + "…", None, True
    return kind.__qualname__, text, None, kind in (str, bytes) and len(value) > _repr.maxstring


SIMPLE = frozenset({int, float, bool, type(None)})
SUMMARIES = {list: "[… {} items]", tuple: "(… {} items)", set: "{{… {} items}}", frozenset: "frozenset({{… {} items}})", dict: "{{… {} items}}"}


def plain_repr(value: object, chars: int, nested: bool = False) -> str | None:
    """A repr made without reprlib, much faster, when it is sure to be short; None otherwise.

    repr() itself for short builtin values and small containers of them, the identity of objects
    without a repr of their own, a dataclass's fields. `nested` (an item of a container, a field
    of a dataclass): other containers as their size only, dataclasses as their name only."""
    kind = type(value)
    if kind is str:
        return repr(value) if len(value) < chars - 2 else None
    if kind in SIMPLE:
        return repr(value) if kind is not int or -(10**18) < value < 10**18 else None
    if kind in CONTAINERS:
        if len(value) <= 4 and all(map(is_short, value.items() if kind is dict else value)):
            return repr(value)
        return SUMMARIES[kind].format(len(value)) if nested else None
    if kind.__repr__ is object.__repr__:
        return object.__repr__(value)
    params = getattr(kind, "__dataclass_params__", None)
    if params is not None and params.repr:
        return f"{kind.__qualname__}(…)" if nested else dataclass_repr(value, chars)
    return None


def is_short(item: object) -> bool:
    """A value whose repr is short and plain, or a (key, value) pair of them."""
    kind = type(item)
    if kind is tuple and len(item) == 2:  # a dict's item
        return is_short(item[0]) and is_short(item[1])
    return kind in SIMPLE and (kind is not int or -(10**18) < item < 10**18) or kind is str and len(item) < 20


def dataclass_repr(value: object, chars: int) -> str:
    """`Cart(items=[… 3 items], total=3)`, from the fields' short reprs: a dataclass's own repr
    shows every field in full, however big."""
    parts, size = [], 0
    for name, field in type(value).__dataclass_fields__.items():
        if not field.repr:
            continue
        if size > chars:
            parts.append("…")
            break
        part = f"{name}={REDACTED if is_secret(name) else short(getattr(value, name, None))}"
        parts.append(part)
        size += len(part) + 2
    return f"{type(value).__qualname__}({', '.join(parts)})"


def container(value, chars: int, items: int) -> tuple[str, str, bool]:
    length = len(value)
    if type(value) is dict:
        shown = [[short(key), REDACTED if type(key) is str and is_secret(key) else short(item)] for key, item in itertools.islice(value.items(), items)]
        inner = ", ".join(f"{key}: {item}" for key, item in shown)
        truncated = length > items or "…" in inner  # an item was cut (or holds a "…" of its own)
        text = "{" + inner + (", …" if length > items else "") + "}"
    else:
        shown = [short(item) for item in itertools.islice(value, items)]
        inner = ", ".join(shown)
        truncated = length > items or "…" in inner
        inner += "," if type(value) is tuple and length == 1 else ", …" if length > items else ""
        if type(value) is list:
            text = f"[{inner}]"
        elif type(value) is tuple:
            text = f"({inner})"
        elif not length:
            text = f"{type(value).__name__}()"
        else:
            text = "{" + inner + "}" if type(value) is set else f"frozenset({{{inner}}})"
    if len(text) > chars:
        text, truncated = text[: chars - 1] + "…", True
    return text, json.dumps({"len": length, "items": shown}, ensure_ascii=False), truncated


def short(value: object) -> str:
    """An item's or a field's repr, at most ITEM_CHARS characters."""
    kind = type(value)
    if kind is str and len(value) < ITEM_CHARS - 2 or kind is int and -(10**18) < value < 10**18:
        return repr(value)  # the most common items, first
    text = plain_repr(value, ITEM_CHARS, nested=True) or safe_repr(value)
    return text if len(text) <= ITEM_CHARS else text[: ITEM_CHARS - 1] + "…"
