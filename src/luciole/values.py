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

# Names whose values are secrets: password, client_secret, api_key, access_token, session_id,
# auth_header... (camelCase is split into words first: apiKey, authToken).
_SECRET = re.compile(
    r"passw(or)?d|passphrase|secret|credential|cookie|csrf|bearer|(^|_)auth(?!ors?(_|$))|session_?id"
    r"|(api|private|access|signing|encryption|master|client|ssh|license|session|oauth)_?(key|token)"
    r"|(refresh|id|bearer|github|gitlab|slack|jwt)_?token"
)
# Words that name secrets as often as not (a dict loop's `key`, a lexer's `token`, a database
# `session`): their values are redacted when they look like secrets themselves.
_MAYBE = re.compile(r"(^|_)(key|token|session)s?(_|$)")
PLAIN, MAYBE, SECRET = 0, 1, 2
_secrecy: dict[str, int] = {}

# Values that look like secrets: known prefixes (GitHub, Slack, AWS, Google, OpenAI-like keys,
# JWTs, HTTP credentials), or long opaque strings mixing digits and letters of both cases (or hex).
SECRET_PREFIXES = (
    "Bearer ", "Basic ", "Token ", "sk-", "sk_", "pk_", "rk_", "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_",
    "glpat-", "xoxb-", "xoxp-", "xoxa-", "xoxs-", "AKIA", "ASIA", "AIza", "ya29.", "eyJ",
)
_OPAQUE = re.compile(r"[A-Za-z0-9_\-+/=]{20,}")
_HEX = re.compile(r"[0-9a-fA-F-]{32,}")


def secrecy(name: str) -> int:
    """PLAIN, MAYBE (a secret if its value looks like one, see looks_secret) or SECRET."""
    try:
        return _secrecy[name]
    except KeyError:
        pass
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()
    found = SECRET if _SECRET.search(words) else MAYBE if _MAYBE.search(words) else PLAIN
    if len(_secrecy) < 10_000:
        _secrecy[name] = found
    return found


def looks_secret(value: object) -> bool:
    """Whether a string looks like a credential: a known prefix, or a long opaque token."""
    if type(value) is bytes:
        value = value.decode("latin-1")
    if type(value) is not str or not 16 <= len(value) <= 4096:
        return False
    if value.startswith(SECRET_PREFIXES):
        return True
    if _HEX.fullmatch(value):
        return any(c.isdigit() for c in value) and any(c.isalpha() for c in value)
    return bool(_OPAQUE.fullmatch(value)) and any(c.isdigit() for c in value) and any(c.islower() for c in value) and any(c.isupper() for c in value)


def is_secret(name: str, value: object = None) -> bool:
    """Whether the value of a variable, argument or dict key `name` is to be redacted."""
    found = secrecy(name)
    return found == SECRET or found == MAYBE and looks_secret(value)


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
        item = getattr(value, name, None)
        part = f"{name}={REDACTED if is_secret(name, item) else short(item)}"
        parts.append(part)
        size += len(part) + 2
    return f"{type(value).__qualname__}({', '.join(parts)})"


def container(value, chars: int, items: int) -> tuple[str, str, bool]:
    length = len(value)
    if type(value) is dict:
        shown = [[short(key), REDACTED if type(key) is str and is_secret(key, item) else short(item)] for key, item in itertools.islice(value.items(), items)]
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
