# Luciole graph format (version 1)

The Python analyzer and the VS Code extension share only this format. The machine-checkable
definition is [`schema/graph.schema.json`](../schema/graph.schema.json). A complete hand-made
example is [`examples/shop.graph.json`](../examples/shop.graph.json), built from
[`examples/shop.py`](../examples/shop.py).

## Top level

```json
{
  "format": "luciole-graph",
  "format_version": 1,
  "root": "examples",
  "generator": { "name": "luciole", "version": "0.0.1" },
  "nodes": [],
  "edges": []
}
```

`format_version` is an integer. It changes only on breaking changes. Adding optional fields does
not change it, so readers must ignore fields they don't know.

## Principles

1. **Containment is a field, not an edge.** Every node has a `parent` (`null` for modules). This
   maps directly onto Cytoscape compound nodes and gives one source of truth.
2. **Edges point where things move.** `calls` goes from caller to callee. Data edges go from where a
   value comes from to where it ends up (`reads` goes from variable to function, `writes` from
   function to variable).
3. **One edge per (kind, source, target).** Repeated occurrences go into the edge's `sites` list.
   This keeps large graphs small and still allows jumping to every occurrence.
4. **Variables are graph nodes, including the return value.** Each function with a `return <expr>`
   gets a pseudo-variable named `return`. A program slice of a variable is then a plain graph
   traversal over `flows_to`, `passes_arg` and `returns` edges, across function boundaries.
5. **Ids are stable qualified names.** Runtime events and saved UI state (what is expanded or
   selected) refer to nodes by id, so ids must not change between runs while the code stays the same.

## Node ids

| Kind | Pattern | Example |
|---|---|---|
| module | dotted module name | `pkg.shop` |
| class / function in a module | `<module>:<name>` | `pkg.shop:Cart`, `pkg.shop:main` |
| class / function nested in a class or function | `<parent>.<name>` | `pkg.shop:Cart.add` |
| variable | `<scope>#<name>` | `pkg.shop:Cart.add#price`, `pkg.shop#TAX_RATE` |
| return value | `<function>#return` | `pkg.shop:with_tax#return` |

`return` is a Python keyword, so it can never collide with a real variable. When a name is defined
twice in the same scope (conditional definitions, `@property` plus a setter), the second definition
gets the suffix `~2`, the third `~3`, and so on (`pkg.shop:Cart.value~2`).

## Nodes

Every node has these fields: `id`, `kind`, `name`, `parent`, `file` (relative to `root`, with `/`
separators), and `loc`.

`loc` is `{line, col, end_line, end_col}` with 1-based lines and 0-based columns, exactly as
`ast` reports them. VS Code positions are 0-based for lines, so the extension subtracts 1.

| Kind | Parent | Extra fields | `loc` covers |
|---|---|---|---|
| `module` | `null` | — | whole file |
| `class` | module, class or function | `bases`, `decorators` (source text) | `class` statement, without decorators |
| `function` | module, class or function | `body_line`, `is_async`, `decorators` (source text) | `def` statement, without decorators |
| `variable` | module, class or function | `role`, and `param_index` when `role` is `param` | first binding |

Variable roles:

| `role` | Meaning | Parent |
|---|---|---|
| `param` | function parameter, including `self`, `*args` and `**kwargs` | function |
| `local` | name bound inside a function | function |
| `global` | module-level name | module |
| `class_attr` | name bound in a class body | class |
| `instance_attr` | `self.<name>` assigned in any method | class |
| `return` | the function's return value | function |

## Edges

```json
{ "kind": "calls", "source": "shop:main", "target": "shop:Cart.add",
  "sites": [{ "line": 24, "col": 4 }, { "line": 25, "col": 4 }] }
```

| Kind | Source → target | Meaning | Site points at |
|---|---|---|---|
| `calls` | module/function → function/class/module | source calls target (or imports the module, which runs its code) | the call expression, or the import |
| `reads` | variable → module/function | code reads the variable | the name |
| `writes` | module/function → variable | code assigns the variable | the assignment target |
| `flows_to` | variable → variable | source's value is used to compute target | the assignment target |
| `passes_arg` | caller variable → callee `param` | value passed as an argument | the argument expression |
| `returns` | callee `return` → caller variable | call result lands in target | the call expression |

`provenance` is `"static"` (the default, usually omitted) or `"runtime"`. Runtime edges come from
dynamic calls such as `getattr`, callbacks or monkey-patching that the static analysis couldn't see.
They may have no `sites`.

`reads` and `writes` are emitted for every access, including a function touching its own locals.
Renderers can hide edges whose variable is a child of the function, since nesting already shows
that relationship. The edges are still useful for `sites` (jumping to each use) and for runtime
lighting.

## Analysis rules

The analyzer ([`src/luciole/analyzer.py`](../src/luciole/analyzer.py)) implements these rules.
They decide what ends up in the graph.

**Calls**

- **Calls to classes** target the class's `__init__` when it is defined in the project (including on
  a base class), or the class node otherwise. This way static edges line up with what the runtime
  recorder sees.
- **Method calls** `obj.m(a)` also produce `passes_arg` from `obj` to `m`'s first parameter
  (`self`), so an object can be traced across its methods. Static methods take no receiver. Class
  methods skip `cls`. `Class.m(obj, a)` passes `obj` to `self` like any other argument.
- **Arguments** map to parameters by position, then by keyword. Extra ones go to `*args` or
  `**kwargs`. Positions after a `*iterable` argument are unknown, so those arguments go to `*args`.
- **`obj.m()` resolves** when the analyzer knows `obj`'s class: `self` and `cls`; a variable or
  attribute assigned from a constructor (`c = Cart()`) or from a call whose return type is plain (a
  return annotation, or `return Cart(...)` / `return self` in the function); a parameter, variable
  or class attribute annotated with a project class (`Cart`, `"Cart"`, `Cart | None`,
  `Optional[Cart]`); `with make() as c` (what `__enter__` returns); and receivers that are calls
  themselves (`make().total()`). Methods are looked up through base classes too.
- **Overrides**: `obj.m()` may run the `m` of any subclass of `obj`'s class, so each override gets
  a `calls` edge too (30 at most), as does a method defined only in subclasses (`self.render()` in a
  base class). `super().m()` resolves through the bases of the method's class, without overrides.
- **Properties**: reading `obj.p` calls `p`'s getter (`@property`, `@cached_property`), and the
  getters overriding it; a `@p.setter` never hides the getter.
- **Special methods** run by operations on a value of known class get `calls` edges: `x[i]`
  (`__getitem__`, `__setitem__`, `__delitem__`), `with x` (`__enter__`/`__exit__`, async too),
  `for … in x` (`__iter__`), `in`, comparisons, arithmetic operators, f-strings (`__format__` or
  `__str__`), `len`/`str`/`repr`/`iter`/`next`/`bool`/`hash`/`abs`, calling an instance
  (`__call__`), and `type(x)(…)` / `x.__class__(…)` (the class's `__init__`).
- **Functions held in variables**: `parse = staticmethod(parse_date)` (or `= parse_date`), then
  `self.parse(v)` calls `parse_date`.
- **By name**: when the receiver's class is unknown, `x.m()` (or reading the property `x.m`)
  resolves to the project's only method named `m`, if there is exactly one and no built-in type has
  a method of that name (`split`, `get`, `close`… never match). The edge gets `"by_name": true`:
  likely, not certain. Anything else is unresolved.
- **Unresolved calls** (builtins, third-party code, unknown receivers), like `y = round(x, 2)` or
  `t = s.upper()`, produce no `calls` edge. Their arguments and receiver `flows_to` the assignment
  target directly, which is a conservative approximation.
- **Resolved calls** `y = f(x)` produce no direct `x → y` flow. The value goes
  `passes_arg` x → `f#param`, then `returns` `f#return` → y.
- **Imports** run the imported module, and the packages around it first: `import a.b` and
  `from a.b import c` give `calls` edges to the project modules `a` and `a.b` (and `a.b.c` when it
  is a submodule). `from m import *` makes `m`'s public names resolvable in the importing module.
- **Decorators** that are project functions get a `calls` edge from the code that defines the
  decorated function.

**Variables**

- **`return f(x)`** treats `#return` as the assignment target.
- **`obj.x`**, when `x` is a known attribute (like `self.x` in a method, or a module's global),
  reads `x` only, not `obj`. `self` is never counted as read. That also holds when `self` is passed
  as a receiver.
- **`a.x = v` and `a[i] = v`**, when `x` isn't a tracked attribute, count as a write to `a`, with
  `v flows_to a`.
- **Augmented assignment** `x += y` is a read and a write of `x`, and `y flows_to x`. It doesn't
  produce a `flows_to` from `x` to itself, because self-loops are never emitted.
- **Tuple unpacking** `a, b = value` sends every source to every target.
- **Default values** `def f(x=LIMIT)` produce `LIMIT flows_to f#x`.
- **Literals** create no edges (`add(10)` has no `passes_arg`).
- **`global` / `nonlocal`** declarations create no new node. Accesses point at the outer variable.
- **Comprehension and lambda variables** are not tracked. In `ys = [f(x) for x in xs]`, `xs` flows
  to `ys`, and `x` has no node.
- **Class bodies** run as the enclosing module or function: that's the source of their `calls`,
  `reads` and `writes`.

**Structure**

- **Imported names** resolve to the node they import, when that node is in the project. Relative
  imports are supported.
- **Module names** come from the package structure: walk up while the directory has an
  `__init__.py`. So `src/luciole/cli.py` is `luciole.cli`. Directories starting with `.`, and
  `__pycache__`, `venv`, `site-packages` and `node_modules` are skipped, and so are the
  directories the top-level `.gitignore` lists by name or simple glob (`runs/`, `/data`,
  `*.egg-info`; not used when it has a `!` pattern).
- **`@overload` stubs** are left out: they never run, and the implementation keeps the plain id.
- **Files that don't parse** are skipped with a warning.

## Worked example: where does `due` come from?

In `examples/shop.py`, following edges backwards from `shop:main#due` gives this chain:

```
shop:main#due
  ← returns     shop:Cart.total#return
  ← returns     shop:with_tax#return
  ← flows_to    shop:with_tax#total
  ← flows_to    shop:with_tax#amount, shop#TAX_RATE
amount ← passes_arg  shop:Cart#subtotal
subtotal ← flows_to  shop:Cart.add#price
```

This is the "trace" view: one variable's path across four functions and a class, with nothing else
drawn.

## Live events

`luciole run [--root DIR] [--port N] [--events FILE] script.py [args...]` (or `-m module`) runs a
program under the recorder ([`src/luciole/recorder.py`](../src/luciole/recorder.py), Python
3.12+, `sys.monitoring`). Only code under `--root` is reported, by the same node ids as the graph.
Every 50 ms the recorder sends one JSON line, to the port and/or the file:

```json
{"t": 1.25,
 "stacks": [{"thread": "MainThread", "stack": ["app", "app:main"]},
            {"thread": "worker-1", "stack": ["app:crunch"]}],
 "calls": {"app:checksum": 12}, "links": {"app:crunch>app:checksum": 12},
 "spawns": {"app:start_workers>app:crunch": 2}, "waiting": {"app:download": 3}}
```

- `t`: seconds since the program started.
- `stacks`: each thread's project frames, outermost first, main thread first, with the thread's name.
- `calls`: calls per function (or module code, or class body) since the previous line.
- `links`: calls per `caller>callee`, the caller being the nearest project frame, even across
  library code (a function passed to `map` is called by whoever called `map`).
- `spawns`: work started elsewhere, per `origin>function`, since the previous line:
  - a thread: `origin` is the project function that called `Thread.start` (also for
    `threading.Thread` subclasses and thread pools, whose workers are started on first use);
  - an asyncio task: `origin` is the project function that created the task's coroutine, as
    recorded by `sys.set_coroutine_origin_tracking_depth`. A task's coroutine is run by the event
    loop, so it gets a spawn instead of a link from whoever started the loop.
- `waiting`: project coroutines suspended on an `await` right now, per function. A coroutine
  awaiting another one counts too (`main` waiting on `crawl` waiting on `download`).

A line is only sent when something changed. The last line is `{"done": true}`.

### The engine

`luciole engine` ([`src/luciole/engine.py`](../src/luciole/engine.py)) is the one local process
that recording Python processes connect to and viewers (the VS Code extension; later agents and
other editors) subscribe to. The extension starts it when none runs; it exits after 10 minutes
with nobody connected. It writes its port to `~/.luciole/engine-v1.json` (`LUCIOLE_HOME` moves
the folder), where recorders and viewers find it; `--port` or `LUCIOLE_LIVE_PORT` name one
explicitly. An engine starting while another answers there exits (with status 0): engines started
together check and write the file in turn, so one remains.

Every connection starts with a hello, answered before anything else
([`src/luciole/protocol.py`](../src/luciole/protocol.py)):

```json
{"hello": {"role": "recorder", "protocol": 1, "version": "0.1.0", "pid": 4242,
           "argv": ["python", "-m", "pytest"], "root": "/work/app"}}
{"hello": {"role": "viewer", "protocol": 1, "version": "0.1.0", "name": "vscode", "roots": ["/work/app"]}}
{"welcome": {"protocol": 1, "version": "0.1.0"}}
{"refused": "this engine speaks protocol 1 (Luciole 0.1.0), not 2"}
```

A starting engine checks that the engine in the file runs with `{"hello": {"role": "probe", ...}}`,
which is welcomed, then closed.

`protocol` changes when a message changes meaning; the engine's file is per protocol, so engines
of two protocols can run side by side. The engine numbers each recorder and tells the viewers that
show its project (one's root contains the other's, symbolic links resolved; a viewer without roots
shows all) about it, then passes on everything it sends with its number:

```json
{"process": {"id": 3, "pid": 4242, "argv": ["python", "-m", "pytest"], "root": "/work/app"}}
{"from": 3, "t": 1.25, "stacks": [...], "calls": {...}}
{"exit": 3}
```

Viewers' commands (below) go to one recorder with `"to": 3`, or to every recorder the viewer shows
without it. When a viewer leaves, the engine resumes the programs no remaining viewer shows (a
paused program would stay frozen otherwise).

### Starting the recorder

- `luciole run`, as above. Without `--port`, it reports to the running engine, if any.
- `luciole.start(root=None, port=None, events=None)` and `luciole.stop()`, from the program
  itself (a server, a notebook). `root` defaults to `LUCIOLE_ROOT`, else the working directory, and
  `port` to the running engine's. Nothing is recorded, and `start` returns `False`, when nothing
  would see the events: no engine runs and there is no events file.
- Auto-start: with the folder `luciole/_autostart` on `PYTHONPATH`, its `sitecustomize.py` starts
  the recorder in any Python process when the engine runs, and does nothing otherwise (not even
  importing the recorder). Child processes inherit the environment; forked children connect again
  as processes of their own. The VS Code switch ("Luciole: on" in the status bar, setting
  `luciole.autoStart`) sets `PYTHONPATH` and `LUCIOLE_ROOT` in new terminals; `luciole env` prints
  them for a shell (`--off` to undo). The `sitecustomize` it hides still runs, and `luciole run`
  processes are left to their own recorder. A program that imports a `luciole` of its own (another
  version, or this repository's tests) gets its own: auto-start keeps its copy out of
  `sys.modules`, and `luciole.start()` from another copy leaves the process to it.

The recorder uses `sys.monitoring` tool id 4 (or 3): the debugger, coverage and profiler ids stay
free.

The recorder sends aggregates rather than one event per call. That keeps the stream small however
fast the program runs (pytest makes about 30,000 calls per second through it), which is all the
lighting needs. Variable reads and writes, and per-call replay, are not recorded yet.

All the state is kept by the monitoring callbacks, in the thread where each event happens: each
thread has its own project stack, pushed on `PY_START`/`PY_RESUME`, popped on
`PY_RETURN`/`PY_YIELD`/`PY_UNWIND`. The background thread that sends the lines never reads other
threads' frames: `sys._current_frames()` breaks `torch.export` tracing.

### Pausing and variables

The connection also carries commands from viewers to the recorder, one JSON object per line:
`{"cmd": "pause"}`, `{"cmd": "resume"}`, `{"cmd": "expand", "ref": 7}` and
`{"cmd": "step", "thread": 1401, "mode": "over"}` (`mode`: `over`, `into` or `out`). On pause, the
recorder turns on `LINE` events and each thread stops at its next line of project code. It sends
its own project frames, innermost first, with their variables, to the view only (not to
`--events`):

```json
{"paused": {"thread": "MainThread", "id": 1401, "frames": [
  {"node": "app:tick", "name": "tick", "file": "/abs/app.py", "line": 14,
   "vars": [{"name": "step", "type": "int", "value": "7"},
            {"name": "cart", "type": "Cart", "value": "<app.Cart object at 0x7f…>", "ref": 3},
            {"name": "(globals)", "value": "2 names", "ref": 4}]}]}}
```

A variable with a `ref` has children. `expand` asks for them, and the thread that owns the value
answers (its `repr` may run the program's code) with `{"children": {"ref": 3, "vars": [...]}}`:
dict items, sequence items or object attributes, 300 at most. Refs are valid until `resume`.
A thread reads only its own frames, as for the stacks above. Closing the socket resumes the
program.

A step resumes every thread. The stepping thread stops at:

- `into`: its next line of project code, wherever it is;
- `over`: the next line of the frame it was paused in, or the next project line once that frame
  returns, raises, or (a generator) yields; a coroutine suspended on an `await` stops when it
  continues;
- `out`: the next project line once that frame returns, raises or yields.

Then every thread stops again, and new `paused` messages come.

With several processes connected, `pause` and `resume` go to all of them, and a step resumes only
the stepping thread's process.

Not covered yet: event loops in threads other than the main one (their tasks still show as
calls, without their spawn origin).
