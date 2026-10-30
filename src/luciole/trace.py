"""Traces: one SQLite file per recorded run, and the queries over it (docs/trace-format.md).

The recorder writes traces (see tracer.py); this module defines their schema and reads them:

    with Trace("run.sqlite") as trace:
        trace.summary()                  # the run, counts, top functions by calls and by time
        trace.calls("shop:Cart.add")     # each call: caller, arguments, result or exception, duration
        trace.tree(depth=3)              # the call tree, repeated calls collapsed
        trace.exceptions()               # raised, where, chained from, handled where
        trace.history("Cart.add")        # level 2: each call's variable assignments, in order

Every method returns plain dicts and lists (JSON-ready); render() turns them into text for
`luciole trace`. Frames, exceptions and snapshots are referred to by their ids in the file.
"""

from __future__ import annotations

import json
import sqlite3
import time
from fnmatch import fnmatchcase
from pathlib import Path

FORMAT = "luciole-trace"
SCHEMA_VERSION = 1

# The contract: docs/trace-format.md shows SCHEMA (tests/test_trace.py checks).
TABLES = """\
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE runs (
    id INTEGER PRIMARY KEY,
    pid INTEGER NOT NULL,
    parent_pid INTEGER,
    argv TEXT NOT NULL,
    root TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,
    duration_ns INTEGER,
    python TEXT NOT NULL,
    luciole TEXT NOT NULL,
    level INTEGER NOT NULL,
    exit_status INTEGER,
    ended_by TEXT,
    frames_total INTEGER,
    frames_stored INTEGER,
    truncated INTEGER NOT NULL DEFAULT 0,
    scope TEXT,
    writes_total INTEGER,
    writes_stored INTEGER
);
CREATE TABLE threads (
    id INTEGER PRIMARY KEY,
    ident INTEGER NOT NULL,
    native_id INTEGER,
    name TEXT NOT NULL
);
CREATE TABLE frames (
    id INTEGER PRIMARY KEY,
    node TEXT NOT NULL,
    caller INTEGER,
    origin INTEGER,
    thread INTEGER NOT NULL,
    line INTEGER,
    start_ns INTEGER NOT NULL,
    end_ns INTEGER,
    end_kind TEXT,
    exception INTEGER,
    args TEXT,
    result INTEGER,
    locals TEXT,
    suspended_ns INTEGER
);
CREATE TABLE exceptions (
    id INTEGER PRIMARY KEY,
    frame INTEGER,
    line INTEGER,
    type TEXT NOT NULL,
    message TEXT NOT NULL,
    cause INTEGER,
    chain TEXT,
    time_ns INTEGER NOT NULL,
    handled_in INTEGER
);
CREATE TABLE snapshots (
    id INTEGER PRIMARY KEY,
    type TEXT NOT NULL,
    repr TEXT NOT NULL,
    items TEXT,
    truncated INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE writes (
    id INTEGER PRIMARY KEY,
    frame INTEGER NOT NULL,
    line INTEGER NOT NULL,
    name TEXT NOT NULL,
    value INTEGER,
    time_ns INTEGER NOT NULL
);
"""

# Made when the run ends: inserting is faster without them.
INDEXES = """\
CREATE INDEX frames_by_node ON frames (node, start_ns);
CREATE INDEX frames_by_caller ON frames (caller);
CREATE INDEX frames_by_time ON frames (start_ns);
CREATE INDEX frames_by_exception ON frames (exception) WHERE exception IS NOT NULL;
CREATE INDEX exceptions_by_frame ON exceptions (frame);
CREATE INDEX exceptions_by_time ON exceptions (time_ns);
CREATE INDEX writes_by_frame ON writes (frame, time_ns);
"""
SCHEMA = TABLES + INDEXES

END_KINDS = ("return", "raise", "unwind", "close", "suspended", "running", "lost")


class TraceError(Exception):
    """Not a trace, or a trace of a newer schema."""


def create(path: Path) -> sqlite3.Connection:
    """A new, empty trace at `path` (replacing any file there), for the writer thread."""
    for stale in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        stale.unlink(missing_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    db.execute("PRAGMA journal_mode = WAL")  # readers (luciole trace, MCP) can follow a run as it goes
    db.execute("PRAGMA synchronous = OFF")  # a trace is not worth an fsync; WAL keeps it whole if the process dies
    db.executescript("BEGIN;\n" + TABLES + "COMMIT;")
    db.executemany("INSERT INTO meta VALUES (?, ?)", [("format", FORMAT), ("schema_version", str(SCHEMA_VERSION))])
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return db


class Trace:
    """A trace file, read-only."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        if not self.path.is_file():
            raise TraceError(f"no such trace: {self.path}")
        self.db = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True)
        self.db.row_factory = sqlite3.Row
        try:
            self._check()
        except TraceError:
            self.db.close()  # not left open until garbage collection: Windows locks open files
            raise
        self._snapshots: dict[int, dict] = {}

    def _check(self) -> None:
        try:
            meta = dict(self.db.execute("SELECT key, value FROM meta").fetchall())
        except sqlite3.DatabaseError as error:
            raise TraceError(f"not a Luciole trace: {self.path} ({error})") from None
        if meta.get("format") != FORMAT:
            raise TraceError(f"not a Luciole trace: {self.path}")
        if int(meta.get("schema_version", 0)) > SCHEMA_VERSION:
            raise TraceError(f"{self.path} has trace schema {meta['schema_version']}; this Luciole reads {SCHEMA_VERSION} at most")

    def __enter__(self) -> Trace:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.db.close()

    # Queries

    def run(self) -> dict:
        row = self.db.execute("SELECT * FROM runs ORDER BY id LIMIT 1").fetchone()
        run = dict(row)
        run["argv"] = json.loads(run["argv"])
        run["truncated"] = bool(run["truncated"])
        run["scope"] = json.loads(run["scope"]) if run.get("scope") else None  # level 2 only
        if run["duration_ns"] is None:  # still recording, or died without its last write
            run["duration_ns"] = self.db.execute("SELECT MAX(COALESCE(end_ns, start_ns)) FROM frames").fetchone()[0] or 0
        return run

    def summary(self, top: int = 10) -> dict:
        """The run, how much it holds, and the functions with the most calls and the most time.

        `total_ns` counts a function's calls from start to end, leaving out calls made while
        another call of the same function runs (recursion); `active_ns` is the same without the
        time its generators and coroutines spent suspended (at a `yield` or an `await`); `self_ns`
        is its active time minus its callees' active time, and minus the time it waited on the
        asyncio tasks run by the event loop it runs (`asyncio.run(main())` in a module: a task's
        caller is the frame running the loop, on the same thread). That is the union of the tasks'
        spans, their suspended time included: while a task exists, the frame is in the event loop,
        running a task or waiting for one to be ready. Threads it starts run concurrently with it,
        and are not subtracted."""
        run = self.run()
        end = run["duration_ns"]
        counts = self.db.execute("SELECT (SELECT COUNT(*) FROM frames), (SELECT COUNT(*) FROM exceptions), (SELECT COUNT(*) FROM threads)").fetchone()
        # Traces written before frames had `suspended_ns` count suspended time as active.
        own, inner = ("COALESCE(f.suspended_ns, 0)", "COALESCE(suspended_ns, 0)") if self._has_column("frames", "suspended_ns") else ("0", "0")
        # A frame's active time minus its callees' (not the tasks and threads it started).
        busy = f"COALESCE(f.end_ns, :end) - f.start_ns - {own} - COALESCE(k.inner_ns, 0)"
        callees = f"""LEFT JOIN (SELECT caller, SUM(COALESCE(end_ns, :end) - start_ns - {inner}) AS inner_ns FROM frames
                       WHERE caller IS NOT NULL AND origin IS NULL GROUP BY caller) k ON k.caller = f.id"""
        functions = self.db.execute(
            f"""
            SELECT f.node, COUNT(*) AS calls,
                   SUM(CASE WHEN c.node IS f.node THEN 0 ELSE COALESCE(f.end_ns, :end) - f.start_ns END) AS total_ns,
                   SUM(CASE WHEN c.node IS f.node THEN 0 ELSE COALESCE(f.end_ns, :end) - f.start_ns - {own} END) AS active_ns,
                   SUM(MAX(0, {busy})) AS self_ns,
                   SUM(f.end_kind IN ('raise', 'unwind')) AS raised
            FROM frames f
            LEFT JOIN frames c ON c.id = f.caller
            {callees}
            GROUP BY f.node
            """,
            {"end": end},
        ).fetchall()
        functions = {row["node"]: dict(row) for row in functions}
        # Tasks the event loop ran in a frame: started by someone else, called from it, on its thread.
        tasks = self.db.execute(
            """
            SELECT t.caller, t.start_ns, MIN(COALESCE(t.end_ns, :end), COALESCE(p.end_ns, :end)) FROM frames t
            JOIN frames p ON p.id = t.caller WHERE t.origin IS NOT NULL AND t.thread = p.thread ORDER BY t.caller, t.start_ns
            """,
            {"end": end},
        ).fetchall()
        waited: dict[int, int] = {}  # frame: the union of its tasks' spans (gathered tasks overlap)
        reached: dict[int, int] = {}
        for caller, start, stop in tasks:
            begin = max(start, reached.get(caller, start))
            waited[caller] = waited.get(caller, 0) + max(0, stop - begin)
            reached[caller] = max(stop, reached.get(caller, stop))
        if waited:
            frames = ",".join(str(int(frame)) for frame in waited)
            for frame, node, alone in self.db.execute(f"SELECT f.id, f.node, {busy} FROM frames f {callees} WHERE f.id IN ({frames})", {"end": end}):
                functions[node]["self_ns"] -= max(0, alone) - max(0, alone - waited[frame])
        functions = list(functions.values())
        return {
            "run": run,
            "frames": counts[0],
            "exceptions": counts[1],
            "threads": counts[2],
            "by_calls": sorted(functions, key=lambda f: (-f["calls"], f["node"]))[:top],
            "by_time": sorted(functions, key=lambda f: (-f["self_ns"], f["node"]))[:top],
        }

    def nodes(self, name: str) -> list[str]:
        """Node ids that `name` stands for: itself when recorded, else those ending with `:name`
        or `.name` (`Cart.add` for `shop:Cart.add`)."""
        if self.db.execute("SELECT 1 FROM frames WHERE node = ? LIMIT 1", (name,)).fetchone():
            return [name]
        rows = self.db.execute("SELECT DISTINCT node FROM frames WHERE node LIKE ? ESCAPE '\\' OR node LIKE ? ESCAPE '\\'", (f"%:{_like(name)}", f"%.{_like(name)}"))
        return sorted(row[0] for row in rows)

    def calls(self, node: str, limit: int = 20, offset: int = 0) -> dict:
        """Calls of a function (see nodes() for `node`), in order: `{"nodes", "total", "calls"}`."""
        nodes = self.nodes(node)
        marks = ",".join("?" * len(nodes))
        total = self.db.execute(f"SELECT COUNT(*) FROM frames WHERE node IN ({marks})", nodes).fetchone()[0]
        rows = self.db.execute(f"SELECT * FROM frames WHERE node IN ({marks}) ORDER BY start_ns, id LIMIT ? OFFSET ?", [*nodes, limit, offset]).fetchall()
        return {"nodes": nodes, "total": total, "calls": [self._frame(row) for row in rows]}

    def frame(self, frame_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM frames WHERE id = ?", (frame_id,)).fetchone()
        return self._frame(row) if row else None

    def tree(self, start: int | None = None, depth: int = 4, max_frames: int = 5000) -> list[dict]:
        """The call tree from frame `start` (default: every root), `depth` levels deep.

        Each entry is `{"frame", "node", "count", "duration_ns", "end", "children", "more"}`:
        consecutive calls of a function with the same subtree are one entry with their `count`
        (`frame` is the first one, `duration_ns` their sum); `more` counts the calls below an
        entry that are not shown (deeper than `depth`, or past `max_frames` frames read)."""
        end = self.run()["duration_ns"]
        if start is None:
            level = self.db.execute("SELECT * FROM frames WHERE caller IS NULL ORDER BY id").fetchall()
        else:
            level = self.db.execute("SELECT * FROM frames WHERE id = ?", (start,)).fetchall()
        entries = {row["id"]: self._entry(row, end) for row in level}
        roots = [entries[row["id"]] for row in level]
        parents = list(entries.values())
        for _ in range(depth):
            if not parents or len(entries) >= max_frames:
                break
            rows = self._children([entry["frame"] for entry in parents], max_frames - len(entries))
            parents = []
            for row in rows:
                entry = self._entry(row, end)
                entries[row["id"]] = entry
                entries[row["caller"]]["children"].append(entry)
                parents.append(entry)
        # What was not read is counted, not shown.
        counts = self._child_counts(list(entries))
        for entry in entries.values():
            entry["more"] = counts.get(entry["frame"], 0) - len(entry["children"])
        return _collapse(roots)

    def children(self, frame: int | None = None, after: int = 0, until: int | None = None, limit: int = 100, collapse: bool = True, scan: int = 20_000) -> dict:
        """The calls `frame` made (default: the roots), one page at a time, for views that open
        a call tree level by level: `{"calls", "next"}`.

        Each item is a call (like an item of calls()) with `count`, `last` (the frame id of the
        last call it stands for), `duration_ns` (their sum) and `calls` (how many calls it made).
        With `collapse`, consecutive calls of one function that ended the same way are one item,
        `count` of them (its values are the first call's); the calls below it are then those of
        all of them. Pages go by frame id: `after` the last id of the previous page (`next`, None
        at the end), `until` an id not to go past (the calls of a collapsed item: `after` its
        frame - 1, `until` its `last`, without `collapse`). At most `limit` items, from at most
        `scan` calls read."""
        where, params = ("caller = ?", [frame]) if frame is not None else ("caller IS NULL", [])
        if until is not None:
            where, params = where + " AND id <= ?", [*params, until]
        rows = self.db.execute(f"SELECT id, node, end_kind, start_ns, end_ns FROM frames WHERE {where} AND id > ? ORDER BY id LIMIT ?", [*params, after, scan + 1])
        end = None
        groups: list[dict] = []
        last_id, read, more = after, 0, False
        for row in rows:
            if read >= scan:
                more = True
                break
            group = groups[-1] if groups else None
            if collapse and group and group["node"] == row["node"] and group["end"] == row["end_kind"]:
                group["count"] += 1
            elif len(groups) >= limit:
                more = True
                break
            else:
                group = {"frame": row["id"], "node": row["node"], "end": row["end_kind"], "count": 1, "duration_ns": 0}
                groups.append(group)
            if row["end_ns"] is None and end is None:
                end = self.run()["duration_ns"]
            group["last"] = row["id"]
            group["duration_ns"] += (row["end_ns"] if row["end_ns"] is not None else end) - row["start_ns"]
            last_id, read = row["id"], read + 1
        calls = []
        counts = self._child_counts([group["frame"] for group in groups if group["count"] == 1])
        for group in groups:
            call = self.frame(group["frame"])
            call.update(count=group["count"], last=group["last"])
            if group["count"] > 1:
                call["duration_ns"] = group["duration_ns"]
                below = self.db.execute("SELECT COUNT(*) FROM frames WHERE caller BETWEEN ? AND ? AND caller IN (SELECT id FROM frames WHERE " + where + " AND id BETWEEN ? AND ?)", [group["frame"], group["last"], *params, group["frame"], group["last"]]).fetchone()[0]
                call["calls"] = below
            else:
                call["calls"] = counts.get(group["frame"], 0)
            calls.append(call)
        return {"calls": calls, "next": last_id if more else None}

    def exceptions(self, limit: int = 50, offset: int = 0) -> dict:
        """Exceptions in the order they were raised: `{"total", "exceptions"}`. Each has the frame
        and line where it was raised (or where it reached project code), what it was chained from,
        and `handled_in`: the last frame it reached that did not exit with it (none: uncaught)."""
        total = self.db.execute("SELECT COUNT(*) FROM exceptions").fetchone()[0]
        rows = self.db.execute(
            """
            SELECT e.*, f.node AS node, h.node AS handled_node FROM exceptions e
            LEFT JOIN frames f ON f.id = e.frame LEFT JOIN frames h ON h.id = e.handled_in
            ORDER BY e.time_ns, e.id LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        return {"total": total, "exceptions": [dict(row) for row in rows]}

    def traceback(self, exception_id: int | None = None) -> dict | None:
        """The frames an exception left, innermost first, each with the line it was at and (level
        1) its locals then: `{"exception", "frames", "handled_in"}`. Default: the last exception
        nothing handled, else the last one."""
        if exception_id is None:
            row = self.db.execute("SELECT id FROM exceptions WHERE handled_in IS NULL AND frame IS NOT NULL ORDER BY time_ns DESC, id DESC LIMIT 1").fetchone()
            row = row or self.db.execute("SELECT id FROM exceptions ORDER BY time_ns DESC, id DESC LIMIT 1").fetchone()
            if row is None:
                return None
            exception_id = row[0]
        found = self.exceptions_by_id([exception_id])
        if not found:
            return None
        exception = found[0]
        rows = self.db.execute("SELECT * FROM frames WHERE exception = ? ORDER BY start_ns DESC, id DESC", (exception_id,)).fetchall()
        frames, line = [], exception["line"]
        for row in rows:
            frame = self._frame(row)
            frame["at_line"] = line
            line = row["line"]  # the call site, in the frame below
            frames.append(frame)
        handled = self.frame(exception["handled_in"]) if exception["handled_in"] else None
        if handled is not None:
            handled["at_line"] = line
        return {"exception": exception, "frames": frames, "handled_in": handled}

    def history(self, node: str | None = None, name: str | None = None, frame: int | None = None, limit: int = 20, offset: int = 0) -> dict:
        """What a function's local variables were assigned, call by call (level 2, for functions in
        the run's scope): `{"nodes", "scope", "recorded", "calls", "total", "frames"}`. `node` is
        as for calls(), or `frame` one call; `name` keeps one variable. Each item of `frames` is a
        call (like an item of calls()) with its `writes`, `{"line", "name", "value", "time_ns"}`
        in order: the line that assigned it, and the value then. `limit` and `offset` count writes;
        `recorded` says whether the run recorded these functions' variables at all."""
        scope = self.run()["scope"]
        if frame is not None:
            row = self.db.execute("SELECT node FROM frames WHERE id = ?", (frame,)).fetchone()
            nodes = [row[0]] if row else []
        else:
            nodes = self.nodes(node) if node else []
        recorded = bool(scope) and self._has_table("writes") and any(in_scope(found, scope) for found in nodes)
        found = {"nodes": nodes, "scope": scope, "recorded": recorded, "calls": 0, "total": 0, "frames": []}
        if not nodes:
            return found
        if frame is not None:
            where, params = "f.id = ?", [frame]
        else:
            where, params = f"f.node IN ({','.join('?' * len(nodes))})", list(nodes)
        found["calls"] = self.db.execute(f"SELECT COUNT(*) FROM frames f WHERE {where}", params).fetchone()[0]
        if not recorded:
            return found
        if name is not None:
            where, params = where + " AND w.name = ?", [*params, name]
        found["total"] = self.db.execute(f"SELECT COUNT(*) FROM writes w JOIN frames f ON f.id = w.frame WHERE {where}", params).fetchone()[0]
        rows = self.db.execute(
            f"SELECT w.* FROM writes w JOIN frames f ON f.id = w.frame WHERE {where} ORDER BY f.start_ns, f.id, w.time_ns, w.id LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        frames: dict[int, dict] = {}
        for row in rows:
            call = frames.get(row["frame"])
            if call is None:
                call = frames[row["frame"]] = self.frame(row["frame"])
                call["writes"] = []
            call["writes"].append({"line": row["line"], "name": row["name"], "value": self.snapshot(row["value"]), "time_ns": row["time_ns"]})
        found["frames"] = list(frames.values())
        return found

    def exceptions_by_id(self, ids: list[int]) -> list[dict]:
        marks = ",".join("?" * len(ids))
        rows = self.db.execute(
            f"""
            SELECT e.*, f.node AS node, h.node AS handled_node FROM exceptions e
            LEFT JOIN frames f ON f.id = e.frame LEFT JOIN frames h ON h.id = e.handled_in
            WHERE e.id IN ({marks})
            """,
            ids,
        ).fetchall()
        return [dict(row) for row in rows]

    def snapshot(self, snapshot_id: int | None) -> dict | None:
        """A value as recorded: `{"type", "repr"}`, plus `items` (a container's first items, with
        its `len`) and `truncated` when they apply."""
        if snapshot_id is None:
            return None
        try:
            return self._snapshots[snapshot_id]
        except KeyError:
            pass
        row = self.db.execute("SELECT type, repr, items, truncated FROM snapshots WHERE id = ?", (snapshot_id,)).fetchone()
        value = None
        if row:
            value = {"type": row["type"], "repr": row["repr"]}
            if row["items"] is not None:
                value["items"] = json.loads(row["items"])
            if row["truncated"]:
                value["truncated"] = True
        if len(self._snapshots) < 100_000:
            self._snapshots[snapshot_id] = value
        return value

    # Helpers

    def _has_table(self, table: str) -> bool:
        """Whether this trace has `table`: traces written before it was added do not."""
        return self.db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() is not None

    def _has_column(self, table: str, column: str) -> bool:
        return any(row[1] == column for row in self.db.execute(f"PRAGMA table_info({table})"))

    def _frame(self, row: sqlite3.Row) -> dict:
        caller = self.db.execute("SELECT node FROM frames WHERE id = ?", (row["caller"],)).fetchone() if row["caller"] else None
        thread = self.db.execute("SELECT name FROM threads WHERE id = ?", (row["thread"],)).fetchone()
        exception = None
        if row["exception"]:
            found = self.db.execute("SELECT id, type, message, frame FROM exceptions WHERE id = ?", (row["exception"],)).fetchone()
            exception = dict(found) if found else {"id": row["exception"]}
        frame = {
            "frame": row["id"],
            "node": row["node"],
            "caller": {"frame": row["caller"], "node": caller[0] if caller else None, "line": row["line"]} if row["caller"] else None,
            "origin": row["origin"],
            "thread": thread[0] if thread else None,
            "start_ns": row["start_ns"],
            "duration_ns": row["end_ns"] - row["start_ns"] if row["end_ns"] is not None else None,
            "end": row["end_kind"],
            "exception": exception,
        }
        if row["args"] is not None:
            frame["args"] = {name: self.snapshot(value) for name, value in json.loads(row["args"]).items()}
        if row["end_kind"] == "return" and row["args"] is not None:
            frame["result"] = self.snapshot(row["result"])
        if "locals" in row.keys() and row["locals"] is not None:  # traces before 0.3 have no column
            frame["locals"] = {name: self.snapshot(value) for name, value in json.loads(row["locals"]).items()}
        return frame

    def _entry(self, row: sqlite3.Row, end: int) -> dict:
        return {
            "frame": row["id"],
            "node": row["node"],
            "count": 1,
            "duration_ns": (row["end_ns"] if row["end_ns"] is not None else end) - row["start_ns"],
            "end": row["end_kind"],
            "children": [],
            "more": 0,
        }

    def _children(self, frames: list[int], limit: int) -> list[sqlite3.Row]:
        rows = []
        for chunk in _chunks(frames):
            rows += self.db.execute(f"SELECT * FROM frames WHERE caller IN ({','.join('?' * len(chunk))}) ORDER BY id LIMIT ?", [*chunk, limit - len(rows)]).fetchall()
            if len(rows) >= limit:
                break
        return sorted(rows, key=lambda row: row["id"])

    def _child_counts(self, frames: list[int]) -> dict[int, int]:
        counts = {}
        for chunk in _chunks(frames):
            query = f"SELECT caller, COUNT(*) FROM frames WHERE caller IN ({','.join('?' * len(chunk))}) GROUP BY caller"
            counts.update(self.db.execute(query, chunk).fetchall())
        return counts


def in_scope(node: str, patterns: list[str]) -> bool:
    """Whether level 2 records the variables of `node`: a pattern matches its node id, with
    fnmatch wildcards (`cart:checkout`, `cart:Cart.*`, `cart:*`), or the end of it as nodes()
    matches names (`checkout`, `Cart.add`, `Cart.*`)."""
    return any(fnmatchcase(node, pattern) or fnmatchcase(node, f"*:{pattern}") or fnmatchcase(node, f"*.{pattern}") for pattern in patterns)


def _like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _chunks(items: list, size: int = 500):
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _collapse(entries: list[dict]) -> list[dict]:
    """Merge consecutive entries of the same function with the same (collapsed) subtree."""
    result: list[dict] = []
    for entry in entries:
        entry["children"] = _collapse(entry["children"])
        previous = result[-1] if result else None
        if previous and _shape(previous) == _shape(entry):
            previous["count"] += entry["count"]
            previous["duration_ns"] += entry["duration_ns"]
            previous["more"] += entry["more"]
            _merge(previous["children"], entry["children"])
        else:
            result.append(entry)
    return result


def _shape(entry: dict) -> tuple:
    return entry["node"], entry["end"], bool(entry["more"]), tuple((_shape(child), child["count"]) for child in entry["children"])


def _merge(into: list[dict], other: list[dict]) -> None:
    for mine, theirs in zip(into, other):
        mine["duration_ns"] += theirs["duration_ns"]
        mine["more"] += theirs["more"]
        _merge(mine["children"], theirs["children"])


# Text, for `luciole trace`


def duration(ns: int | None) -> str:
    if ns is None:
        return "?"
    if ns < 1_000:
        return f"{ns} ns"
    if ns < 1_000_000:
        return f"{ns / 1_000:.1f} µs"
    if ns < 1_000_000_000:
        return f"{ns / 1_000_000:.2f} ms"
    return f"{ns / 1_000_000_000:.2f} s"


def render_summary(summary: dict) -> str:
    run = summary["run"]
    started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(run["started_at"]))
    status = "still running or killed" if run["ended_at"] is None else f"exit status {run['exit_status']}" if run["exit_status"] is not None else f"ended by {run['ended_by']}"
    lines = [
        f"run: {' '.join(run['argv'])}",
        f"  pid {run['pid']}, Python {run['python']}, luciole {run['luciole']}, level {run['level']}, root {run['root']}",
        f"  started {started}, {duration(run['duration_ns'])}, {status}",
        f"  {plural(summary['frames'], 'frame')}, {plural(summary['exceptions'], 'exception')}, {plural(summary['threads'], 'thread')}",
    ]
    if run.get("scope"):
        lines.append(f"  variable history (level 2) for {', '.join(run['scope'])}: {plural(run.get('writes_stored') or 0, 'write')}")
    if run["truncated"]:
        if (run["frames_total"] or 0) > (run["frames_stored"] or 0):
            lines.append(f"  truncated: {run['frames_stored']} of {plural(run['frames_total'], 'frame')} kept")
        if (run.get("writes_total") or 0) > (run.get("writes_stored") or 0):
            lines.append(f"  truncated: {run['writes_stored']} of {plural(run['writes_total'], 'write')} kept")
    for title, key in (("by calls", "by_calls"), ("by time (self)", "by_time")):
        lines += ["", f"top functions {title}:", f"  {'calls':>8}  {'total':>10}  {'active':>10}  {'self':>10}  function"]
        for f in summary[key]:
            lines.append(f"  {f['calls']:>8}  {duration(f['total_ns']):>10}  {duration(f['active_ns']):>10}  {duration(f['self_ns']):>10}  {f['node']}")
    if any(f["active_ns"] != f["total_ns"] for f in summary["by_calls"] + summary["by_time"]):
        lines += ["", "active: total without the time generators and coroutines spent suspended (yield, await)"]
    return "\n".join(lines)


def value_text(value: dict | None) -> str:
    return "?" if value is None else value["repr"]


def render_calls(found: dict) -> str:
    if not found["nodes"]:
        return "no calls recorded for this function"
    lines = [f"{plural(found['total'], 'call')} of {', '.join(found['nodes'])}"]
    for call in found["calls"]:
        caller = call["caller"]
        where = f"from {caller['node']}:{caller['line']} (#{caller['frame']})" if caller else "root"
        if call["origin"]:
            where += f", started by #{call['origin']}"
        args = ", ".join(f"{name}={value_text(value)}" for name, value in call.get("args", {}).items())
        name = call["node"] if len(found["nodes"]) > 1 else call["node"].rsplit(":", 1)[-1]
        head = f"#{call['frame']} {name}({args})" if "args" in call else f"#{call['frame']} {name}"
        if call["end"] == "return":
            outcome = f" -> {value_text(call['result'])}" if "result" in call else ""
        elif call["exception"]:
            exception = call["exception"]
            outcome = f" !! {exception.get('type', '?')}: {exception.get('message', '')}"
            outcome += "" if call["end"] == "raise" else f" (from #{exception.get('frame')})"
        else:
            outcome = f" [{call['end']}]"
        lines.append(f"  {head}{outcome}   {duration(call['duration_ns'])}, {where}, {call['thread']}")
    shown = len(found["calls"])
    if shown < found["total"]:
        lines.append(f"  ... {found['total'] - shown} more (--limit, --offset)")
    return "\n".join(lines)


def render_tree(entries: list[dict], indent: int = 0) -> str:
    lines = []
    for entry in entries:
        count = f" ×{entry['count']}" if entry["count"] > 1 else ""
        end = "" if entry["end"] == "return" else f" [{entry['end']}]"
        more = f"  (+{plural(entry['more'], 'call')} below)" if entry["more"] else ""
        lines.append(f"{'  ' * indent}{entry['node']}{count}  #{entry['frame']}  {duration(entry['duration_ns'])}{end}{more}")
        if entry["children"]:
            lines.append(render_tree(entry["children"], indent + 1))
    return "\n".join(lines)


def render_traceback(found: dict | None) -> str:
    if found is None:
        return "no exceptions"
    e = found["exception"]
    lines = [f"!{e['id']} {e['type']}: {e['message']}"]
    if e["cause"]:
        lines.append(f"  {'caused by' if e['chain'] == 'cause' else 'while handling'} !{e['cause']}")
    for frame in found["frames"]:
        here = "raised here" if frame["end"] == "raise" else "passed through"
        lines.append(f"  in {frame['node']} (#{frame['frame']}), line {frame['at_line']}: {here}")
        for name, value in frame.get("locals", {}).items():
            lines.append(f"      {name} = {value_text(value)}")
    handled = found["handled_in"]
    if handled:
        lines.append(f"  handled in {handled['node']} (#{handled['frame']}), line {handled['at_line']}")
    elif found["frames"]:
        lines.append("  not handled")
    if found["frames"] and not any("locals" in frame for frame in found["frames"]):
        lines.append("  (no locals: recorded at level 0)")
    return "\n".join(lines)


def render_history(found: dict) -> str:
    if not found["nodes"]:
        return "no calls recorded for this function"
    names = ", ".join(found["nodes"])
    if not found["recorded"]:
        scope = f"its scope was {', '.join(found['scope'])}" if found["scope"] else "it was not recorded at level 2"
        return f"no variable history for {names}: {scope}. Run again with --level 2 --scope {found['nodes'][0]}"
    if not found["total"]:
        return f"{plural(found['calls'], 'call')} of {names}, no variable written"
    lines = [f"{plural(found['total'], 'write')} in {plural(found['calls'], 'call')} of {names}"]
    shown = 0
    for call in found["frames"]:
        args = ", ".join(f"{name}={value_text(value)}" for name, value in call.get("args", {}).items())
        name = call["node"] if len(found["nodes"]) > 1 else call["node"].rsplit(":", 1)[-1]
        lines.append(f"  #{call['frame']} {name}({args})" if "args" in call else f"  #{call['frame']} {name}")
        for write in call["writes"]:
            after = f"+{duration(write['time_ns'] - call['start_ns'])}"
            lines.append(f"    line {write['line']:<4} {write['name']} = {value_text(write['value'])}   {after}")
        shown += len(call["writes"])
    if shown < found["total"]:
        lines.append(f"  ... {plural(found['total'] - shown, 'more write')} (--limit, --offset)")
    return "\n".join(lines)


def plural(count: int, word: str) -> str:
    """`1 call`, `2 calls`, `3 processes`."""
    return f"{count} {word}" if count == 1 else f"{count} {word}es" if word.endswith("s") else f"{count} {word}s"


def render_exceptions(found: dict) -> str:
    if not found["total"]:
        return "no exceptions"
    lines = [f"{plural(found['total'], 'exception')}"]
    for e in found["exceptions"]:
        where = f"{e['node']}:{e['line']} (#{e['frame']})" if e["frame"] else f"line {e['line']}"
        lines.append(f"  !{e['id']} {e['type']}: {e['message']}")
        lines.append(f"      raised in {where} at {duration(e['time_ns'])}")
        if e["cause"]:
            lines.append(f"      {'caused by' if e['chain'] == 'cause' else 'while handling'} !{e['cause']}")
        lines.append(f"      handled in {e['handled_node']} (#{e['handled_in']})" if e["handled_in"] else "      not handled")
    shown = len(found["exceptions"])
    if shown < found["total"]:
        lines.append(f"  ... {found['total'] - shown} more (--limit, --offset)")
    return "\n".join(lines)
