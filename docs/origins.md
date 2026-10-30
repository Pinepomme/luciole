# Origin queries: where did this value come from?

A wrong value is rarely wrong where it shows up. An origin query starts from one variable of one
call in a recorded run and walks back through what made it: the assignment that set it, the
caller that passed it, the call that returned it, the code that set a global or an attribute,
then what fed each of those, across functions. Each step has its line, its code and, when the
trace recorded it, its value.

It combines the two halves of Luciole. The static graph ([graph-format.md](graph-format.md))
holds every path a value could take: `flows_to` from the variables an assignment reads,
`passes_arg` from a call's arguments to the callee's parameters, `returns` from a callee's return
value to where the result lands, `writes` from the code that sets a global or an attribute. The
trace ([trace-format.md](trace-format.md)) says which of those happened, in which call, when, and
with which values. Walking both backwards is a dynamic backward slice. The code is in
[`src/luciole/origins.py`](../src/luciole/origins.py).

## Asking

```sh
luciole trace run.sqlite origin checkout due                 # due, at the end of the last call of checkout
luciole trace run.sqlite origin - due --frame 6               # in call #6
luciole trace run.sqlite origin - due --frame 6 --at 10396237 # at that moment (ns since the run started)
luciole trace run.sqlite origin Cart.total self.subtotal --depth 12 --branches 6 [--json]
```

The variable is a local or a parameter (`due`), `return` (what the call returned), an attribute
of `self` (`self.subtotal`), a global the function reads (`RATE`), or a variable's node id
(`shop:Cart#subtotal`). The static graph is built from the trace's recorded root (`runs.root`),
so the source must still be there (and unchanged, for the lines to match).

In VS Code, put the cursor on a variable (or `return`) and run **Luciole: Where Did This Value
Come From?** from the editor's context menu, or click **where from?** on a value in **Show
Values**, or **Where From?** on a call in the **Calls** view. The answer shows in the **Origin**
view, one step per item with the same information as below (a step opens its line and pulses its
function on the brain), and the brain lights the path: the steps' functions stand out, the others
are dimmed, and each move of the value between two functions traces their line in the accent,
from the sources toward the question. A `needs_level_2` step offers to run the same command again
with that function in the scope; a step cut by the depth limit offers to go on from there. The
extension asks through the trace's query server (`luciole trace FILE serve`, query `origin`, which
also gives each step's function's file, and `function_at` and `last_calls` to find the call under
the cursor; see [trace-format.md](trace-format.md)). A live run or a replay starting clears the
path.

From an agent, the MCP server's `origin` tool takes the same (`function`, `variable`, `frame`,
`at`, `depth`, plus `run` and `process`; see [mcp.md](mcp.md)). In Python:

```python
from luciole.analyzer import build_graph
from luciole.origins import origin, render_origin
from luciole.trace import Trace

with Trace("run.sqlite") as trace:
    found = origin(trace, build_graph(root), "checkout", "due", frame=None, time_ns=None, depth=8, branches=4)
    print(render_origin(found))   # or use found["steps"], a JSON-ready tree
```

## Reading the answer

A real answer, on a small program recorded at level 1 (`luciole run --trace run.sqlite --level 1
bank.py`): an account, two deposits that each pay a fee, a global rate changed between them, and a
second account whose deposit comes last:

```
$ luciole trace run.sqlite origin main final
where final in bank:main #3 came from (level 1 trace):

final = 30.0   main #3, line 48: final = account.report()   (its only assignment)
  <- returned by Account.report #16: 30.0   line 26: return shown
    <- shown = 30.0   Account.report #16, line 25: shown = self.balance   (its only assignment)
      <- Account.balance set by Account.deposit #10, line 22: self.balance += amount - fee   (on the same object)
        <- amount = 0   argument of Account.deposit #10, passed by main #3, line 45: account.deposit(parse("-3"))
          <- returned by parse #9: 0   line 8: return value
            <- value = 0   parse #9: assigned on lines 5, 7   (assigned on several lines: which one made this value is in its variable history (level 2))
                 line 5: value = int(text)
                 line 7: value = 0
                 to know which: run again with --scope bank:parse, then ask again
        <- fee = 0.0   Account.deposit #10, line 21: fee = fee_for(amount)   (its only assignment)
          <- returned by fee_for #11: 0.0   line 12: return amount * RATE
            ...
            <- RATE set by setup #8, line 31: RATE = 0.5
        <- before that: Account.balance set by Account.deposit #6, line 22: self.balance += amount - fee   (on the same object)
          ...
```

Each line is a step, its sources indented below it after `<-`; `#N` are frame ids, as in the other
`luciole trace` answers. The kinds of steps (`kind` in JSON):

| Kind | What it says | Its sources |
|---|---|---|
| `write` | the assignment that gave the variable its value | what that line read: other variables (`flows_to`), calls made on it (`returns`) |
| `argument` | a parameter: the call, and the caller's line | the caller's variables passed there (`passes_arg`), a call made on that line (`f(g(x))`), or none: a literal |
| `returned_by` | what a call returned, at its return statement | what that return statement read |
| `global_written_by`, `attribute_written_by` | the last call that set it before then (for `self.x`, on the same object); then any later call that changed it in place (`x.append(...)`) | what the assignment read |
| `literal` | nothing tracked fed it: a literal, an imported name, library calls | |
| `needs_level_2` | a local assigned on several lines: the candidates, and the function to put in a scope | |
| `unknown` | why it cannot go further | |

`before that:` marks the value an augmented assignment (`x += y`) started from. `(followed
above)` marks a step whose sources are already shown elsewhere in the tree: one event is followed
once, which also keeps recursion from looping.

The walk stops after `--depth` steps (8 by default), follows `--branches` sources per step (4),
and makes 200 steps at most; where it stops, the answer says so, with the question to ask next
(`to go on: origin - amount --frame 6`), and lists the sources it did not follow.

## What each answer needs

| Question | Level 0 | Level 1 | Level 2 (function in the scope) |
|---|---|---|---|
| a parameter: its caller and call line | yes | with its value | with its value, and any reassignment |
| a value returned by a call | which return statement, when it can be told | with its value | with its value |
| a local assigned on one line | its line | its line; its value when the line is `x = f(...)`, or `return x` ends the call | its line and value at that moment |
| a local assigned on several lines | the candidates | the candidates | the line that ran, and the value |
| a global, an attribute | the last call that set it (on any object) | the same, on the same object for `self.x` | the same (only locals have a history); a module's own assignments are exact when the module is in the scope |

So a run at level 1, the MCP server's default, already follows values across calls; when an
answer says `needs_level_2`, run the same command again with that function in the scope
(`luciole run --scope bank:parse ...`, or the `run` tool's `scope`) and ask again.

Below level 2 there are no line events, so the return statement that ended a call is inferred:
the one holding the last call the frame made; else the only one consistent with the trace (one
that calls project code did not run if no such call was made from it; one above the frame's last
call or write, outside a loop around both, did not run; one returning a literal, or a parameter
never reassigned, did not if that value differs from the result). When several remain, they are
listed, with the frame's last writes at level 2.

## Limits

Origins are as good as the static edges they follow, and those are approximate:

- **Calls through objects of unknown type** have no `calls` or `returns` edge (see "Analysis
  rules" in [graph-format.md](graph-format.md)): `x = obj.method()` on an untyped `obj` makes the
  receiver and the arguments flow into `x` instead. When the trace shows a call on that line that
  returned exactly the value, the answer follows it (noted); otherwise it follows the receiver.
- **Containers changed in place** (`items.append(x)`, `d[k] = v` on an untracked container) are
  not assignments. At level 2, a local list, dict, set or bytearray whose contents change is
  recorded as written at that line, and the answer follows what that line reads. For globals and
  attributes, the last call that changed it in place is named, but not what it put there.
- **Globals and attributes have no recorded values or lines**: the answer is the last call of a
  function that statically writes them (the last one to start before the moment, or to end, for
  a call still running then), not the exact write. A setter running concurrently, `setattr`,
  `globals()[...]`, a library setting it, or a write through another name (`other.x = ...` from a
  function that is not a method of that object) can be missed or misplaced.
- **Same object** means the same `self` snapshot (`<pkg.Cart object at 0x…>`, level 1): an
  address can be reused once an object is freed, and an attribute read on another object of the
  same class (`other.total` in a method) is taken as `self`'s.
- **Recursion**: the graph has no self-loops, so `f(n - 1)` has no `passes_arg` from `n` to
  itself; the answer follows the caller's `n` when that line reads it (noted).
- **Comprehensions and lambdas** have no frames and their variables no nodes: an argument passed
  from inside a comprehension is followed to what the comprehension iterates over.
- Calls the static graph does not see (callbacks from libraries, `getattr`) still have their
  caller in the trace, but not the expression their arguments came from.
- The source must not have changed since the run: lines are matched against the current files.
