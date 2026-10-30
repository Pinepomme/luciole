# Luciole trace format (schema version 1)

A trace is what one run of a program did: every call of project code (the call tree), how long
each took, the exceptions, at level 1 the arguments and return values, and at level 2 every
assignment in the functions you choose (their variables' history). It is one SQLite file
per process, written by the recorder ([`src/luciole/tracer.py`](../src/luciole/tracer.py)) and
read by `luciole trace` and the Python API in [`src/luciole/trace.py`](../src/luciole/trace.py),
which later tools (the MCP server, the timeline in VS Code) build on. Node ids are the graph's
(see [graph-format.md](graph-format.md)), so a trace and a graph of the same code line up.

## Recording

```sh
luciole run --trace run.sqlite [--level 0|1] script.py [args...]   # or -m module
luciole run --trace run.sqlite --scope cart:checkout,Cart.* script.py   # level 2 in those functions
luciole run --trace traces/ ...          # a directory: a new file in it
eval "$(luciole env --trace traces/ --level 1)"   # every Python process of this shell
```

- `--trace FILE` writes the trace to `FILE` (replacing it), or to a new file in it when it is a
  directory. Live lighting (the engine, `--events`) works the same with or without it.
- Auto-start: with the auto-start folder on `PYTHONPATH` (`luciole env`), `LUCIOLE_TRACE=DIR`
  makes every Python process write its own trace in `DIR`, named
  `trace-<date>-<time>-<pid>.sqlite`, whether an engine runs or not. That covers terminal runs,
  test runners and child processes started with `subprocess` or multiprocessing's spawn.
- `luciole.start(trace=..., level=..., scope=...)` from the program itself.
- A forked child writes its own file: a new one in the directory, or `<name>-<pid>.sqlite` next
  to a trace file.

Environment: `LUCIOLE_TRACE` (file or directory; the default of `--trace`), `LUCIOLE_TRACE_LEVEL`
(0, 1 or 2), `LUCIOLE_TRACE_SCOPE` (the default of `--scope`), `LUCIOLE_TRACE_MAX_FRAMES` and
`LUCIOLE_TRACE_MAX_WRITES` (see Limits).

**Levels.**

| Level | Records |
|---|---|
| 0 (default) | the call tree of project code: one row per call, with its caller, call site, thread and times; exceptions |
| 1 | level 0, plus each call's arguments and return value, as capped snapshots |
| 2 | level 1, plus every assignment to the local variables of the functions in a scope (`writes`) |

**Scope (level 2).** Recording every assignment everywhere would be far too slow, so level 2
applies to chosen functions only, and everything else is recorded at level 1. The scope is a list
of patterns (`--scope`, comma-separated): a node id (`cart:checkout`), with `*` wildcards
(`cart:Cart.*`, `cart:*`), or the end of one, as `luciole trace ... calls` takes it (`checkout`,
`Cart.add`, `Cart.*`). A function nested in a scoped one is not scoped by it (`cart:checkout`
does not match `cart:checkout.inner`; `cart:checkout*` does). `--scope` alone means `--level 2`;
level 2 without a scope is refused by `luciole run` and the MCP server, and recorded as level 1,
with a warning, by `luciole.start()` and auto-start. Level 3 (instruction-level provenance) comes
later (see the roadmap); it will add tables, not change these.

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
CREATE INDEX frames_by_node ON frames (node, start_ns);
CREATE INDEX frames_by_caller ON frames (caller);
CREATE INDEX frames_by_time ON frames (start_ns);
CREATE INDEX frames_by_exception ON frames (exception) WHERE exception IS NOT NULL;
CREATE INDEX exceptions_by_frame ON exceptions (frame);
CREATE INDEX exceptions_by_time ON exceptions (time_ns);
CREATE INDEX writes_by_frame ON writes (frame, time_ns);
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
- `frames_total` calls seen, `frames_stored` rows kept, `truncated` 1 when some were not kept
  (frames, or writes at level 2).
- `scope` (level 2): the scope's patterns, a JSON list; `NULL` at other levels (and in traces
  written before this column existed). Only functions it matches have `writes`.
- `writes_total` variable writes seen, `writes_stored` rows of `writes` kept (level 2; `NULL`
  otherwise).

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
  start, `end_ns` its end, the time suspended included. `suspended_ns` is the time it spent
  suspended (at a `yield` or an `await`) between the two: what it did not run. `NULL` for other
  code, and in traces written before this column existed.
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

### `writes`

Level 2: one row per assignment to a local variable of a frame of scoped code (see "Scope"), in
the order they happened.

- `frame`: `frames.id`. `line`: the line that assigned it (a line that runs many times, a loop's,
  has a row each time it writes).
- `name`: the variable. `value`: snapshot id of its new value, under the same rules as arguments
  (`self` as its identity, secret names redacted).
- `time_ns`: when it was seen, at the start of the next line (or as the frame returned, raised or
  yielded).

How writes are seen: at each line of a scoped frame, its locals are compared with those it had at
its previous line. A variable is written when it is newly bound, bound to another object
(identity, so `x = x + 0` with the same small int is not a write), or, for a `list`, `dict`, `set`
or `bytearray` (exact types), when its contents changed, as far as its length and its snapshot
show: a change past the first 8 items (200 bytes of a `bytearray`) that keeps the length is not
seen. Changes inside other objects (an
attribute of `self`, a list inside a dict) are not variable writes. The loop variable of a `for`
is written by the `for` line, a value sent into a generator by its `yield` line. Deleting a
variable is not recorded. Names starting with `__`, and modules, classes and functions (definitions,
not state), are left out, as for `locals`. A function's arguments are in `frames.args`, not here.

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
- Level 2: at most 1,000,000 writes per run (`LUCIOLE_TRACE_MAX_WRITES`). Past it, writes are
  counted (`writes_total`) but not stored, and the run is marked `truncated`.

## How it is written

The recorder's monitoring callbacks keep everything in the thread where the event happens: each
thread its own stack of frames and its own buffer (a deque of plain lists and tuples). A frame's
end is written into its own entry, by its thread. The callbacks never touch SQLite and never read
other threads' Python frames (see "Live events" in [graph-format.md](graph-format.md)). One writer
thread, a thread of the recorder's own (debuggers neither list nor pause it), drains the buffers
every 0.2 s and inserts them in one transaction; a frame that ended by then is inserted once,
complete, the others are updated when they end. Live lighting is unchanged: the same callbacks
feed it, and the engine and `--events` get the same lines with or without a trace.

Level 2 turns on `LINE` events (`sys.monitoring.set_local_events`) for the code of each scoped
function as it first runs, and for nothing else: other code runs as at level 1. At each line, the
thread running the frame compares the frame's locals with those of its previous line (kept in the
frame's entry, so a generator resumed later, even in another thread, goes on from where it was),
and buffers what changed; one last comparison happens when the frame returns, raises or yields.

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
luciole trace run.sqlite history NODE [--var NAME] [--frame N] [--limit 50]   # level 2: assignments, line by line
luciole trace run.sqlite origin NODE VARIABLE [--frame N] [--at NS] [--depth 8]  # where a value came from
```

For tools that browse a trace (VS Code's Calls view and replay, [`src/luciole/browse.py`](../src/luciole/browse.py)):

```sh
luciole trace run.sqlite children [--frame N] [--after ID] [--until ID] [--limit 100] [--no-collapse]
luciole trace run.sqlite frame N                                   # one call: arguments, result or exception, locals
luciole trace run.sqlite frames [--from S] [--to S] [--every MS]  # live frames that replay the run
luciole trace run.sqlite serve                                    # JSON queries on stdin, one per line
```

- `children`: the calls frame `N` made (default: the roots), a page at a time, consecutive calls
  of one function that ended the same way collapsed into one item with a `count`. Each item is a
  call (like an item of `calls`) plus `count`, `last` (the last frame it stands for) and `calls`
  (how many calls it made). Pages go by frame id, so a call with a million children opens as fast
  as one with ten: `next` is the `--after` of the next page (null at the end), and a collapsed
  item's calls, one by one, are `--after FRAME-1 --until LAST --no-collapse`.
- `frames`: what live mode streams ("Live events" in [graph-format.md](graph-format.md)), made
  from the trace, one frame per `--every` ms of the run: `t` (the end of its window, in seconds),
  `stacks` (each thread's project frames then, outermost first), and the `calls`, `links` and
  `spawns` started in its window. Frame ids follow start times, so the stacks at any moment take
  one indexed lookup per frame on them, whatever the trace's size: a replay jumps without reading
  what came before. Frames have no `waiting` (a trace has a coroutine's total time suspended, not
  when).
- `serve`: answers `{"id": 1, "query": "children", "args": {"frame": 3}}` lines with
  `{"id": 1, "result": ...}` (or `"error"`), the queries being `run`, `summary`, `children`,
  `frame`, `exceptions`, `traceback`, `history` and `frames` with the arguments above (`from`,
  `to`, `every` in nanoseconds). The trace is opened again for each request, so a run still
  recording is read as it is now.
`origin` walks a value back through the assignments, arguments, return values, globals and
attributes that made it, across functions, with the static graph: see [origins.md](origins.md),
which also says what each level lets it answer, and its limits.

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
    trace.history("checkout", name=None, frame=None, limit=20, offset=0)
                              # {"nodes", "scope", "recorded", "calls", "total", "frames": [{..., "writes"}]}
    trace.children(frame=None, after=0, until=None, limit=100, collapse=True)   # {"calls", "next"}
    trace.frame(7)            # one call, like an item of calls()
    trace.snapshot(3)         # {"type", "repr", "items"?, "truncated"?}
```

In `tree`, consecutive calls of one function with the same subtree are one entry with a `count`;
`more` counts the calls below an entry that are not shown. In `traceback`, each frame (like an
item of `calls()`, plus `locals`) has `at_line`, the line it was at when the exception left it;
by default it is the last exception nothing handled, else the last one. In `history`, each item
of `frames` is a call (like an item of `calls()`) with its `writes`, `{"line", "name", "value",
"time_ns"}`; `limit` and `offset` count writes, `name` keeps one variable, and `recorded` is false
when the run's scope did not cover the function (no level 2, or another scope): run it again
with `--scope`. In `summary`, a function's `total_ns` leaves out calls made while another call of
it runs (recursion); `active_ns` is the same without the time its generators and coroutines spent
suspended; `self_ns` is its active time minus its callees' active time, so a coroutine awaiting
`asyncio.sleep` is not busy meanwhile. The frame running an event loop (`asyncio.run(main())` in
a module) is the caller of the tasks it runs, on its thread: the union of their spans, suspended
time included, is subtracted from its self time too, as it was only running them or waiting for
one to be ready. Threads run concurrently with the frame that started them: not subtracted.

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

Level 2 costs per line run in the scoped functions, and nothing elsewhere: code outside the scope
gets no `LINE` events at all. Measured the same way, on a loop of 200,000 iterations (three lines
each, two variables written each time, four locals) in a function, timed inside the program:

| The loop's function | Plain | Level 1 | Level 2 |
|---|---|---|---|
| in the scope (600,000 lines, 400,000 writes) | 0.008 s | 0.008 s | 1.00 s |
| not in the scope | 0.008 s | 0.008 s | 0.008 s |

- About 1.3 µs per line to compare the frame's locals with the previous line's (more with more
  locals), and 0.5 µs more per write stored; about 65 bytes per write in the file. A list, dict,
  set or bytearray is compared by its length and first items, not snapshotted, until it changes.
- Calls of unscoped functions cost what they cost at level 1.
- So keep the scope to the few functions in question: a hot loop in a scoped function runs about
  100 times slower.

Ways to make it cheaper, for later: record level 1 only in chosen functions (`sys.monitoring`
can turn events on per code object, as level 2 does), and sample very hot functions.
