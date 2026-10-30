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
[`src/luciole/origins.py`](../src/luciole/origins.py), its text in [`origin_text.py`](../src/luciole/origin_text.py).

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
bank.py`): three deposits, the second of an amount that `parse` should have refused, and a check
that fails at the end.

```python
def parse(text):
    value = 0
    if text.strip().lstrip("-").isdigit():
        value = int(text)
    return value

def deposit(balance, amount):
    fee = 1 if amount < 10 else 0
    return balance + amount - fee

def main():
    balance = 0
    for raw in ["5", "-3", "20"]:
        balance = deposit(balance, parse(raw))
    if balance != 24:
        raise ValueError(f"balance is {balance}")
```

```
$ luciole trace run.sqlite origin main balance
where balance in bank:main #2 came from (level 1 trace):

values on the way:
  main.balance: 4, 0, 20
  deposit.return: 4, 0, 20
  deposit.amount: 5, -3, 20
  deposit.balance: 0, 4, 0
  parse.return: -3, 20
  parse.value: -3, 20
  parse.text: '-3', '20'

balance = 20   main #2, line 14: balance = deposit(balance, parse(raw))   (assigned on lines 12, 14; inferred: balance was 20 as the exception left #2; line 12 assigns 0; deposit #8, the last call on line 14, returned 20)
  <- returned by deposit #8: 20   line 9: return balance + amount - fee
    <- balance = 0   argument of deposit #8, passed by main #2, line 14: balance = deposit(balance, parse(raw))
      <- balance = 0   main #2, line 14: balance = deposit(balance, parse(raw))   (assigned on lines 12, 14; inferred: balance was 0 when it was passed on; deposit #6, the last call on line 14, returned 0)
        <- returned by deposit #6: 0   line 9: return balance + amount - fee
          <- balance = 4   argument of deposit #6, passed by main #2, line 14: balance = deposit(balance, parse(raw))
            ...
                <- balance = 0   argument of deposit #4, passed by main #2, line 14: balance = deposit(balance, parse(raw))
                     (followed above, with the same value, in #8)
            ...
          <- amount = -3   argument of deposit #6, passed by main #2, line 14: balance = deposit(balance, parse(raw))
            <- returned by parse #5: -3   line 5: return value
              <- value = -3   parse #5, line 4: value = int(text)   (assigned on lines 2, 4; inferred: #5 returned it: -3; line 2 assigns 0)
                <- text = '-3'   argument of parse #5, passed by main #2, line 14: balance = deposit(balance, parse(raw))
                     ... not followed further (depth limit); to go on: origin - text --frame 5
          <- fee   deposit #6, line 8: fee = 1 if amount < 10 else 0   (its only assignment; its value is not recorded, as level 1 records locals only as an exception left them, or not at all)
               to record its value: run again with --scope bank:deposit, then ask again
            <- no variable   (no tracked variable: a literal, an imported name, or library calls)
    <- amount = 20   argument of deposit #8, passed by main #2, line 14: balance = deposit(balance, parse(raw))
      ...
```

`values on the way` comes first: each variable the walk met, with the values it took in time order
(a repeat in a row shown once), the ones that took several different values first, 12 at most
(`values` in JSON: `name`, `node`, `variable`, `values`). The odd value is often the culprit, here
the `-3` that `parse` let through; the tree below says where each value came from.

Each line is a step, its sources indented below it after `<-`; `#N` are frame ids, as in the other
`luciole trace` answers. The kinds of steps (`kind` in JSON):

| Kind | What it says | Its sources |
|---|---|---|
| `write` | the assignment that gave the variable its value | what that line read: other variables (`flows_to`), calls made on it (`returns`) |
| `argument` | a parameter: the call, and the caller's line | the caller's variables passed there (`passes_arg`), a call made on that line (`f(g(x))`), or none: a literal |
| `returned_by` | what a call returned, at its return statement | what that return statement read |
| `global_written_by`, `attribute_written_by` | the last call that set it before then (for `self.x`, on the same object); then any later call that changed it in place (`x.append(...)`) | what the assignment read |
| `literal` | nothing tracked fed it: a literal, an imported name, library calls | |
| `needs_level_2` | a local assigned on several lines, and nothing in the trace tells which made the value: the candidates left, and the scope to run again with | |
| `unknown` | why it cannot go further | |

`before that:` marks the value an augmented assignment (`x += y`) started from. `(followed
above)` marks a step whose sources are already shown elsewhere in the tree: one event is followed
once, which also keeps recursion from looping, and so is one variable of one function with one
value (`(followed above, with the same value, in #8)`), so the branches go to new values first.
When a value is not recorded (a local at level 1, or in a function outside a level 2 run's
scope), the note says why, and the answer says once which scope to run again with
(`to record its value: run again with --scope ...`).

The walk stops after `--depth` steps (8 by default), follows `--branches` sources per step (4),
and makes 200 steps at most; where it stops, the answer says so, with the question to ask next
(`to go on: origin - amount --frame 6`), and lists the sources it did not follow.

## What each answer needs

| Question | Level 0 | Level 1 | Level 2 (function in the scope) |
|---|---|---|---|
| a parameter: its caller and call line | yes | with its value | with its value, and any reassignment |
| a value returned by a call | which return statement, when it can be told | with its value | with its value |
| a local assigned on one line | its line | its line; its value when the line is `x = f(...)`, `return x` ends the call, or an exception left it | its line and value at that moment |
| a local assigned on several lines | the candidates | the line, when the values the trace has tell it (below); else the candidates | the line that ran, and the value |
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

The line among several that assigned a local is inferred the same way, from its value when the
trace has it: the locals an exception left the frame with (at its end), the call's result when it
ended with `return x`, or the argument it was passed as is (`f(x)`, at that call). A line
assigning a literal (or a parameter never reassigned) of another value did not make it; of the
lines left, the one whose call, the last made on it before then, returned exactly that value did.
When one line remains, the answer follows it, with a note starting `inferred:` that says how;
values are compared as their recorded `repr`.

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
