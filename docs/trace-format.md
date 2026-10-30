# Luciole trace format (schema version 1)

A trace is what one run of a program did: every call of project code (the call tree), how long
each took, the exceptions, and at level 1 the arguments and return values. It is one SQLite file
per process, written by the recorder ([`src/luciole/tracer.py`](../src/luciole/tracer.py)) and
read by `luciole trace` and the Python API in [`src/luciole/trace.py`](../src/luciole/trace.py),
which later tools (the MCP server, the timeline in VS Code) build on. Node ids are the graph's
(see [graph-format.md](graph-format.md)), so a trace and a graph of the same code line up.

## Recording

```sh
luciole run --trace run.sqlite [--level 0|1] script.py [args...]   # or -m module
luciole run --trace traces/ ...          # a directory: a new file in it
eval "$(luciole env --trace traces/ --level 1)"   # every Python process of this shell
```

- `--trace FILE` writes the trace to `FILE` (replacing it), or to a new file in it when it is a
  directory. Live lighting (the engine, `--events`) works the same with or without it.
- Auto-start: with the auto-start folder on `PYTHONPATH` (`luciole env`), `LUCIOLE_TRACE=DIR`
  makes every Python process write its own trace in `DIR`, named
  `trace-<date>-<time>-<pid>.sqlite`, whether an engine runs or not. That covers terminal runs,
  test runners and child processes started with `subprocess` or multiprocessing's spawn.
- `luciole.start(trace=..., level=...)` from the program itself.
- A forked child writes its own file: a new one in the directory, or `<name>-<pid>.sqlite` next
  to a trace file.

Environment: `LUCIOLE_TRACE` (file or directory; the default of `--trace`), `LUCIOLE_TRACE_LEVEL`
(0 or 1), `LUCIOLE_TRACE_MAX_FRAMES` (see Limits).

**Levels.**

| Level | Records |
|---|---|
| 0 (default) | the call tree of project code: one row per call, with its caller, call site, thread and times; exceptions |
| 1 | level 0, plus each call's arguments and return value, as capped snapshots |

Levels 2 (every assignment in chosen functions, a `writes` table) and 3 come later (see the
roadmap); they will add tables, not change these.

What counts as project code is the recorder's rule (see "Live events" in
[graph-format.md](graph-format.md)): `.py` files under the root, outside hidden and virtual
environment folders and outside Luciole itself. Library code is not recorded, but a project
function called back by a library still gets the nearest project frame below it as its caller.

## Schema

The file's `meta` table holds `format = "luciole-trace"` and `schema_version`, also in
`PRAGMA user_version`. `schema_version` changes only when a table or column changes meaning or
goes away; adding a table or a column does not change it, so readers must ignore what they do not
know. Readers refuse a newer version than theirs.

<!-- schema: the same text as trace.SCHEMA, checked by tests/test_trace.py -->
```sql
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
    truncated INTEGER NOT NULL DEFAULT 0
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
    locals TEXT
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
CREATE INDEX frames_by_node ON frames (node, start_ns);
CREATE INDEX frames_by_caller ON frames (caller);
CREATE INDEX frames_by_time ON frames (start_ns);
CREATE INDEX frames_by_exception ON frames (exception) WHERE exception IS NOT NULL;
CREATE INDEX exceptions_by_frame ON exceptions (frame);
CREATE INDEX exceptions_by_time ON exceptions (time_ns);
```

Times (`*_ns`) are nanoseconds since the run started, from a monotonic clock
(`time.perf_counter_ns`); `started_at` and `ended_at` are Unix times in seconds.

### `runs`

One row (`id` 1): the process (`pid`, `parent_pid`, `argv` as a JSON list, the project `root`),
`python` and `luciole` versions, the `level`, and how it ended:

- `ended_at`, `duration_ns`: `NULL` while recording, or when the process died without writing its
  end (SIGKILL, a crash). Everything written up to its last write (every 0.2 s) is still there.
- `ended_by`: `stop` (the program ended, or `luciole.stop()`) or `sigterm`.
- `exit_status`: the program's exit code under `luciole run`; `NULL` when unknown (auto-start
  cannot see it).
- `frames_total` calls seen, `frames_stored` rows kept, `truncated` 1 when some were not kept.

### `threads`

One row per thread that ran project code: `ident` (`threading.get_ident()`), `native_id`, and the
thread's `name` when it first ran project code.

### `frames`

One row per call of project code: a function, method, module body or class body. `node` is the
graph's node id of the code (`pkg.mod:Class.method`, `pkg.mod` for a module body). Lambdas and
comprehensions have no node, and are not recorded.

- `caller`: the frame of the nearest project code below it on the thread's stack, even across
  library code; `NULL` for a root (the first project code of a thread, or code called while no
  project frame is running). `line` is the line of the caller that was running: the call site.
- `origin`: who started it, when that is not its caller. For a thread's first frame, the frame
  that called `Thread.start`. For an asyncio task's coroutine (whose caller is whoever runs the
  event loop), the frame that created the coroutine, when that frame is still running or
  suspended (a coroutine gathering tasks); `NULL` when it already returned.
- `thread`: `threads.id`.
- A generator or coroutine is one frame however many times it is resumed: `start_ns` is its first
  start, `end_ns` its end, the time suspended included.
- `end_kind`:
  - `return`;
  - `raise`: it exited with an exception raised in it (or in library code it called);
  - `unwind`: it exited with an exception raised in a project frame below it;
  - `close`: a generator or coroutine closed before it finished (`GeneratorExit` ran in it);
  - `suspended`: a generator or coroutine suspended (at a `yield` or `await`) when recording
    stopped, or abandoned: dropped, or closed where it had nothing to run, which some Python
    versions do without an event (others report it, as `close`). `end_ns` is when it last
    suspended;
  - `running`: still running when recording stopped (a daemon thread); `end_ns` is `NULL`;
  - `lost`: it left without an event the recorder saw (rare: recording paused, stacks switched by
    a library such as greenlet).
- `exception`: for `raise` and `unwind`, the `exceptions.id` it exited with.
- `args` (level 1): a JSON object `{"name": snapshot id}` of the arguments as the call received
  them (including `*args` and `**kwargs`, as one tuple and one dict); `{}` when it has none.
  `NULL` at level 0.
- `result` (level 1): snapshot id of the return value, for `return` frames (a generator's is the
  value it returns at the end, usually `None`).
- `locals` (level 1): for `raise` and `unwind` frames, a JSON object `{"name": snapshot id}` of
  the frame's local variables as the exception left it: what a post-mortem debugger shows. For a
  module body, its globals. Names starting with `__`, modules, classes and functions are left out
  (definitions, not state); secret names are redacted as arguments are; at most 50 per frame (then
  `"…"`). Kept for 10,000 frames per run at most, so code that raises in a loop stays cheap.
  `NULL` otherwise, and in traces written before this column existed.

### `exceptions`

One row per exception, when it is raised in project code or first reaches it (from library code).
`StopIteration`, `StopAsyncIteration` and `GeneratorExit` are control flow and are not recorded.

- `frame`, `line`: where it was raised, or where it reached project code (the call's line).
- `type`: `ValueError` for builtins, `module.Qualname` otherwise. `message`: `str()` of it, at most
  500 characters.
- `cause`, `chain`: the exception it was chained from, when that one is recorded too: `cause` for
  `raise ... from ...`, `context` for one raised while handling another.
- `handled_in`: the last frame it reached that did not exit with it, where it was caught;
  `NULL` when it ended the program or thread (or was still propagating when recording stopped).

### `snapshots`

A value as it was at one moment (level 1): `type` (its class's qualified name), `repr` (at most
200 characters), `items` for a `list`, `tuple`, `set`, `frozenset` or `dict` (exact types, not
subclasses): JSON `{"len": n, "items": [...]}` with the reprs of its first 8 items, at most 60
characters each (dict items as `[key, value]`), and `truncated` 1 when anything was cut. Equal
snapshots are stored once, so ids repeat.

- `self`, and objects whose class has no `__repr__` of its own, are stored as their identity
  (`<pkg.Cart object at 0x7f…>`) without calling any `__repr__`: cheap, safe in `__init__`, and
  the same object gives the same snapshot (while it lives: addresses are reused once it is freed).
- A dataclass shows its fields (`Scope(name='x', items=[… 12 items], parent=Scope(…))`), not its
  generated repr, which shows every field in full however big.
- Items of containers, and fields: short builtin values and small containers of them in full;
  other containers as their size (`[… 12 items]`); other objects by their repr.
- Other values use the same repr as the recorder's `paused` messages (`reprlib`, which runs the program's own
  `__repr__`, in the program's thread). A failing `__repr__` gives `<repr failed: …>`.
- Immutable values (`int`, `float`, `complex`, `bool`, `str`, `bytes`, `None`) and identities are
  snapshotted later, by the writer thread; others when the event happens.

**Redaction.** The value of an argument whose name looks like a secret is stored as
`<redacted>`: names containing (case-insensitive, camelCase split into words) `password`,
`passwd`, `secret`, `token`, `api_key`/`apikey`, `credential`, `cookie`, `session`, a word
starting with `auth` (`auth_header`, `authorization`; not `author`), or the word `key` (`key`,
`sort_key`, `key_file`; not `keyboard`). Dict items under such keys, and dataclass fields of such
names, are redacted the same way. A secret inside another object's own repr is not.

### Limits

- Per value: the repr and items caps above.
- Per run: at most 2,000,000 frames (`LUCIOLE_TRACE_MAX_FRAMES`). Past it, new calls are counted
  (`frames_total`) but not stored, nor their exceptions, and the run is marked `truncated`.

## How it is written

The recorder's monitoring callbacks keep everything in the thread where the event happens: each
thread its own stack of frames and its own buffer (a deque of plain lists and tuples). A frame's
end is written into its own entry, by its thread. The callbacks never touch SQLite and never read
other threads' Python frames (see "Live events" in [graph-format.md](graph-format.md)). One writer
thread, a thread of the recorder's own (debuggers neither list nor pause it), drains the buffers
every 0.2 s and inserts them in one transaction; a frame that ended by then is inserted once,
complete, the others are updated when they end. Live lighting is unchanged: the same callbacks
feed it, and the engine and `--events` get the same lines with or without a trace.

The file is in WAL mode while the run goes on, so it can be read meanwhile (without the indexes,
which are made when the run ends: inserting is faster without them), and back to a single file
(`journal_mode = DELETE`) once it ends. It is written with `synchronous = OFF`: a process that
dies keeps everything written so far, an operating system crash may not.

The last write happens when recording stops: at the program's end (or `luciole.stop()`), when
SIGTERM ends the process (unless the program has its own handler; see "Starting the recorder"),
and in forked children that end with `os._exit`.

If the file cannot be written (a full disk, say), tracing stops with a warning and the program
runs on: what was written so far stays (without the run's end, as after a crash), and the
recorder keeps nothing more in memory.

## Reading

```sh
luciole trace run.sqlite summary [--top 10]      # the run, counts, top functions by calls and by time
luciole trace run.sqlite calls NODE [--limit 20] [--offset 0]   # each call: caller, args, result or exception, duration
luciole trace run.sqlite tree [--from FRAME] [--depth 4]         # call tree, repeated calls collapsed
luciole trace run.sqlite exceptions [--limit 50]                 # where raised, chained from, handled where
luciole trace run.sqlite traceback [EXCEPTION]                   # the frames it left, with their locals then
```

`NODE` is a node id, or its end (`Cart.add` for `shop:Cart.add`). `--json` prints the same
results as JSON. In Python:

```python
from luciole.trace import Trace

with Trace("run.sqlite") as trace:
    trace.summary(top=10)     # {"run", "frames", "exceptions", "threads", "by_calls", "by_time"}
    trace.calls("Cart.add", limit=20, offset=0)   # {"nodes", "total", "calls": [...]}
    trace.tree(start=None, depth=4)               # [{"frame", "node", "count", "duration_ns", "end", "children", "more"}]
    trace.exceptions(limit=50, offset=0)          # {"total", "exceptions": [...]}
    trace.traceback(exception_id=None)            # {"exception", "frames", "handled_in"}: innermost first
    trace.frame(7)            # one call, like an item of calls()
    trace.snapshot(3)         # {"type", "repr", "items"?, "truncated"?}
```

In `tree`, consecutive calls of one function with the same subtree are one entry with a `count`;
`more` counts the calls below an entry that are not shown. In `traceback`, each frame (like an
item of `calls()`, plus `locals`) has `at_line`, the line it was at when the exception left it;
by default it is the last exception nothing handled, else the last one. In `summary`, a function's `total_ns`
leaves out calls made while another call of it runs (recursion), and `self_ns` leaves out its
callees (but not the threads and tasks it runs concurrently).

## Worked example

`luciole run --root examples --trace shop.sqlite --level 1 examples/shop.py`, then
`SELECT id, node, caller, line, end_kind, args FROM frames`:

<!-- example: shop frames, checked by tests/test_trace.py -->
```json
[
  [1, "shop", null, null, "return", "{}"],
  [2, "shop:Cart", 1, 11, "return", "{}"],
  [3, "shop:main", 1, 31, "return", "{}"],
  [4, "shop:Cart.__init__", 3, 23, "return", "{\"self\": 2}"],
  [5, "shop:Cart.add", 3, 24, "return", "{\"self\": 2, \"price\": 3}"],
  [6, "shop:Cart.add", 3, 25, "return", "{\"self\": 2, \"price\": 4}"],
  [7, "shop:Cart.total", 3, 26, "return", "{\"self\": 2}"],
  [8, "shop:with_tax", 7, 19, "return", "{\"amount\": 6}"]
]
```

The module body (frame 1) runs the class body of `Cart` (line 11) and `main` (line 31). The four
method calls share one `self` snapshot (id 2: the same `Cart`), `price` is `10` then `5`
(snapshots 3 and 4), and `with_tax` receives `15` and returns `18.0`:

```
$ luciole trace shop.sqlite calls with_tax
1 calls of shop:with_tax
  #8 with_tax(amount=15) -> 18.0   6.2 µs, from shop:Cart.total:19 (#7), MainThread
```

## Cost

Tracing runs inside the program's own callbacks and writer thread, under the same GIL, so its
cost is per call of project code. Measured with Python 3.12 on WSL2 (best of 5 runs, wall time,
including startup), on small workloads written for it and on a real one: a copy of Luciole's
analyzer building the graph of Luciole's sources.

| Workload | Project calls | Plain | `luciole run` | Level 0 | Level 1 |
|---|---|---|---|---|---|
| recursion (`fib`, Hanoi with a list argument) | 281,000 | 0.02 s | 0.44 s | 1.26 s | 3.08 s |
| small methods on objects | 180,000 | 0.02 s | 0.26 s | 0.81 s | 1.67 s |
| JSON and strings (dict arguments) | 100,000 | 0.11 s | 0.30 s | 0.61 s | 1.80 s |
| the analyzer on Luciole's sources | 288,000 | 0.16 s | 1.44 s | 2.57 s | 5.70 s |
| this repository's test suite (`pytest -q`) | 3,000 | 17.6 s | 17.7 s | 17.9 s | 17.9 s |

- Level 0 adds about 3 µs per call to live mode (a third in the callbacks, the rest in the
  writer: about 100 bytes per frame in the file).
- Level 1 adds 5 to 12 µs more per call, depending on the arguments: plain values are almost
  free (snapshotted by the writer, and stored once per distinct value), containers cost a few
  microseconds (their first 8 items), objects with a `__repr__` of their own cost what that
  `__repr__` costs. About 200 bytes per frame.
- The test suite runs mostly in subprocesses and in Luciole itself, so little of it is project
  code in the recording process: what tracing costs there is the work it has to do, nothing more.

Ways to make it cheaper, for later: record level 1 only in chosen functions (`sys.monitoring`
can turn events on per code object), and sample very hot functions.
