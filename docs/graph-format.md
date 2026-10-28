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
| `function` | module, class or function | `is_async`, `decorators` (source text) | `def` statement, without decorators |
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
| `calls` | module/function → function/class | source calls target | the call expression |
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
- **`obj.m()` resolves** only when the analyzer knows `obj`'s class. That's the case for `self`,
  for a variable assigned from a constructor (`c = Cart()`), and for a parameter annotated with a
  project class. Methods are looked up through base classes too. Anything else is unresolved, until
  roadmap step 4 adds a real type checker.
- **Unresolved calls** (builtins, third-party code, unknown receivers), like `y = round(x, 2)` or
  `t = s.upper()`, produce no `calls` edge. Their arguments and receiver `flows_to` the assignment
  target directly, which is a conservative approximation.
- **Resolved calls** `y = f(x)` produce no direct `x → y` flow. The value goes
  `passes_arg` x → `f#param`, then `returns` `f#return` → y.
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
  `__pycache__`, `venv`, `site-packages` and `node_modules` are skipped.
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

The recorder sends aggregates rather than one event per call. That keeps the stream small however
fast the program runs (pytest makes about 30,000 calls per second through it), which is all the
lighting needs. Variable reads and writes, and per-call replay, are not recorded yet.

All the state is kept by the monitoring callbacks, in the thread where each event happens: each
thread has its own project stack, pushed on `PY_START`/`PY_RESUME`, popped on
`PY_RETURN`/`PY_YIELD`/`PY_UNWIND`. The background thread that sends the lines never reads other
threads' frames: `sys._current_frames()` breaks `torch.export` tracing.

### Pausing and variables

The socket also carries commands from the view to the recorder, one JSON object per line:
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

Not covered yet: other processes (`multiprocessing`, Python subprocesses), and event loops in
threads other than the main one (their tasks still show as calls, without their spawn origin).
