"""Origin queries: where did a value come from (docs/trace-format.md, "Origins").

The static graph (analyzer.py) holds every path a value could take: `flows_to` from the variables
an assignment reads, `passes_arg` from a call's arguments to the callee's parameters, `returns`
from a callee's return value to where its result lands, `writes` from the code that assigns a
global or an attribute. A trace (trace.py) says which of them happened, when, and with which
values. origin() walks both backwards from one variable of one call, a dynamic backward slice:

    with Trace("run.sqlite") as trace:
        found = origin(trace, build_graph(root), "checkout", "due")
        print(render_origin(found))  # origin_text.py

    due = 12.0   checkout #4, line 34: due = cart.total()
      <- returned by Cart.total #9: 12.0   line 21: return with_tax(base)
        <- returned by with_tax #10: 12.0   line 7: return round(total, 2)
          ...

Each step says what the value was (when recorded) and the line that made it, and its children
what fed that line. How exact it is depends on the trace's level:
- level 2 (functions in the run's scope): each write of a local, with its line and value, so the
  line that made a value is known;
- level 1: arguments and return values, so values that crossed calls are known, and a local
  assigned on one line only is too; a local assigned on several lines is placed from its value
  when the trace has it (an exception's locals, `return x`, `f(x)`), else gives the candidates
  (`needs_level_2`), with what to run to know;
- level 0: the same paths, without values.
Globals and attributes are not recorded at all (only locals are): their origin is the last call
of a function that statically writes them, run before the moment (on the same object, for an
attribute of `self`, when level 1 shows `self`), with the line(s) it writes them at, and the
last call that changed them in place since, if any.

Where the trace has no line events (below level 2), the line a value came from is inferred: a
variable assigned on one line only; a return statement that holds the frame's last call, or the
only one consistent with its calls, writes, arguments and result. Notes say what was inferred.

The walk is bounded (depth, branches per step, steps in all) and never follows one event twice.
"""

from __future__ import annotations

import ast
import json
import re
import sqlite3

from luciole.origin_text import VALUES, clip, render_origin, short  # render_origin: part of this module's API
from luciole.slicing import Graph
from luciole.trace import Trace, in_scope

DEPTH = 8  # steps from the variable asked about
BRANCHES = 4  # sources followed per step; the others are counted
MAX_STEPS = 200  # steps in one answer, whatever the depth
SCAN = 2000  # frames read, newest first, looking for a write on one object
AUGMENTED = r"\s*(\*\*|//|>>|<<|[-+*/%&|^@])="
# Methods that change a list, dict, set or bytearray in place.
MUTATORS = ("append", "extend", "insert", "remove", "pop", "popitem", "clear", "sort", "reverse", "update", "setdefault", "add", "discard", "difference_update", "intersection_update", "symmetric_difference_update")


class OriginError(ValueError):
    """The question cannot be asked: no such function, frame or variable in this run."""


def origin(trace: Trace, graph: dict | Graph, node: str | None, variable: str, frame: int | None = None, time_ns: int | None = None, depth: int = DEPTH, branches: int = BRANCHES) -> dict:
    """Where the value of `variable` in a call of `node` came from.

    `node` is a function's node id or its end (`checkout`, `Cart.total`), as Trace.calls takes
    it; `frame` picks one call (default: the last one), and is enough alone. `variable` is a
    local or a parameter (`due`), `return` (what the call returned), an attribute of `self`
    (`self.subtotal`), a global the function reads (`DISCOUNT`), or a variable's node id. The
    value is the one it had at `time_ns` (nanoseconds since the run started; default: the
    call's end).

    Returns `{"frame", "node", "variable", "var", "time_ns", "level", "scope", "values", "steps"}`:
    `values` are the values each variable met took (see Walker.values_on_the_way), and
    `steps` is the tree: each step has `kind`, `variable` (as written in the code), `var` (its
    node id), `frame` and `node` (the call it is about), `line` and `code` (the line that made
    the value), `value` (a snapshot's repr, or None when not recorded), `note`, `children` (what
    fed it), and when the walk stopped there, `cut` ("depth", "steps"), `seen` (followed
    elsewhere in the tree; `seen_value`, the frame where the same variable with the same value
    was) or `more` and `not_followed` (sources past the branch limit). A local whose value is not
    recorded has `rerun` and `rerun_scope`: the scope that would record it. Kinds:
    - `write`: an assignment in the frame (`time_ns` when level 2 saw it, else `at_ns`, the moment
      its value is about); `previous` marks the value an augmented assignment (`x += y`) started
      from;
    - `argument`: a parameter, with `caller` (`frame`, `node`, `line`, `code` of the call);
    - `returned_by`: what a call returned, at its return statement (`candidates` when several
      may have, with `locals`, its last writes, at level 2);
    - `attribute_written_by`, `global_written_by`: the last call that set it before then, and
      `changed_in_place` when a later call changed it in place (`x.append(...)`);
    - `literal`: nothing tracked fed the line;
    - `needs_level_2`: assigned on several lines (`candidates`, those the trace does not rule
      out): which one made the value is in the variable history of the function `rerun` names
      (`rerun_scope`: the scope to run again with);
    - `unknown`, with why in `note`."""
    graph = graph if isinstance(graph, Graph) else Graph(graph)
    if frame is not None:
        row = trace.db.execute("SELECT * FROM frames WHERE id = ?", (int(frame),)).fetchone()
        if row is None:
            raise OriginError(f"no frame #{frame} in this run")
        if node and not _matches(row["node"], node):
            raise OriginError(f"frame #{frame} is a call of {row['node']}, not {node}")
    else:
        if not node:
            raise OriginError("give a function, or a frame")
        nodes = trace.nodes(node)
        if not nodes:
            raise OriginError(f"no calls of {node} in this run: it did not run, or the name is wrong")
        if len(nodes) > 1:
            raise OriginError(f"several functions match {node}: {', '.join(nodes[:10])}; give one, or a frame")
        row = trace.db.execute("SELECT * FROM frames WHERE node = ? ORDER BY start_ns DESC, id DESC LIMIT 1", (nodes[0],)).fetchone()
    walker = Walker(trace, graph, depth, branches)
    var = graph.resolve(row["node"], variable)
    moment = time_ns if time_ns is not None else walker.end_of(row)
    steps = walker.explain(row["id"], var, moment, 0)
    run = walker.run
    return {
        "frame": row["id"],
        "node": row["node"],
        "variable": variable,
        "var": var,
        "time_ns": moment,
        "level": run["level"],
        "scope": run["scope"],
        "values": walker.values_on_the_way(steps),
        "steps": steps,
    }


def labeled(label: str, make):
    """A source to follow (a callable making its step), named for when it is not followed."""
    make.label = label
    return make


def note(step: dict, text: str) -> None:
    step["note"] = f"{step['note']}; {text}" if step["note"] else text


def _matches(node_id: str, name: str) -> bool:
    return node_id == name or node_id.endswith((f":{name}", f".{name}"))


class Walker:
    """One origin query: the trace, the graph, and what the walk has seen."""

    def __init__(self, trace: Trace, graph: Graph, depth: int, branches: int):
        self.trace, self.graph, self.db = trace, graph, trace.db
        self.depth, self.branches = max(0, depth), max(1, branches)
        self.run = trace.run()
        self.has_writes = bool(self.run["scope"]) and trace._has_table("writes")
        self.seen: set = set()
        self.followed: dict[tuple, int] = {}  # (node, var, kind, line, value) followed: in which frame
        self.steps = 0
        self._frames: dict[int, sqlite3.Row] = {}

    # The trace

    def frame(self, frame_id: int) -> sqlite3.Row:
        row = self._frames.get(frame_id)
        if row is None:
            row = self._frames[frame_id] = self.db.execute("SELECT * FROM frames WHERE id = ?", (frame_id,)).fetchone()
        return row

    def end_of(self, row: sqlite3.Row) -> int:
        return row["end_ns"] if row["end_ns"] is not None else self.run["duration_ns"]

    def recorded(self, node: str) -> bool:
        """Whether level 2 recorded `node`'s local variables."""
        return self.has_writes and in_scope(node, self.run["scope"])

    def value(self, snapshot_id: int | None) -> str | None:
        found = self.trace.snapshot(snapshot_id)
        return found["repr"] if found else None

    def args(self, row: sqlite3.Row) -> dict:
        return json.loads(row["args"]) if row["args"] else {}

    def local_value(self, row: sqlite3.Row, name: str, moment: int) -> str | None:
        """A local's value at the end of a frame that raised (its failure snapshot)."""
        if "locals" in row.keys() and row["locals"] and moment >= self.end_of(row):
            snapshot = json.loads(row["locals"]).get(name)
            return self.value(snapshot) if isinstance(snapshot, int) else None
        return None

    def calls_at(self, frame_id: int, span: tuple[int, int], before: int) -> list[sqlite3.Row]:
        """Calls made by a frame from the lines `span`, that ended by `before`, the last first."""
        return self.db.execute(
            "SELECT * FROM frames WHERE caller = ? AND line BETWEEN ? AND ? AND end_ns <= ? ORDER BY end_ns DESC, id DESC LIMIT 50",
            (frame_id, span[0], span[1], before),
        ).fetchall()

    # Steps

    def step(self, kind: str, var: str | None, row: sqlite3.Row | None, line: int | None = None, value: str | None = None, **extra) -> dict:
        self.steps += 1
        node = row["node"] if row is not None else None
        step = {
            "kind": kind,
            "variable": self.graph.label(var) if var else None,
            "var": var,
            "frame": row["id"] if row is not None else None,
            "node": node,
            "line": line,
            "code": self.graph.code(node, line) if node and line else None,
            "value": value,
            "note": None,
            "children": [],
        }
        step.update(extra)
        return step

    def follow(self, step: dict, depth: int, key, sources: list) -> dict:
        """Fill `step`'s children from `sources` (callables making one step each), unless the walk
        stops here: too deep, too many steps, or this event already followed (`key`)."""
        if not sources:
            return step
        if key is not None and key in self.seen:
            step["seen"] = True  # its sources are shown where it was first followed
            return step
        same = (step["node"], step["var"], step["kind"], step["line"], step["value"]) if step["value"] is not None and step["var"] else None
        if same in self.followed:
            # The same variable of the same function with the same value, followed elsewhere (a
            # recursion, a loop): the branches go to new information first.
            step["seen"], step["seen_value"] = True, self.followed[same]
            return step
        if depth >= self.depth:
            step["cut"] = "depth"
            return step
        if self.steps >= MAX_STEPS:
            step["cut"] = "steps"
            return step
        if key is not None:
            self.seen.add(key)
        if same is not None:
            self.followed[same] = step["frame"]
        for source in sources[: self.branches]:
            step["children"].append(source())
        if len(sources) > self.branches:
            step["more"] = len(sources) - self.branches
            step["not_followed"] = [getattr(source, "label", None) or "?" for source in sources[self.branches :]]
        return step

    def explain(self, frame_id: int, var: str, moment: int, depth: int, expected: str | None = None, passed: str | None = None) -> dict:
        """The value of `var` as frame `frame_id` saw it at `moment`; `expected` is that value
        when known from elsewhere (an argument it fed), `passed` when it is exactly that (an
        argument it was passed as, as is: `f(x)`)."""
        row = self.frame(frame_id)
        role = self.graph.role(var)
        owner = self.graph.owner(var)
        if role == "return" or var == f"{row['node']}#return":
            return self.returned(row, depth, var)
        if role in ("global", "instance_attr", "class_attr"):
            return self.shared(row, var, moment, depth)
        if owner != row["node"]:
            step = self.step("unknown", var, row)
            step["note"] = f"a variable of {owner} (an enclosing function, or code the analysis could not place): not followed"
            return step
        step = self.local(row, var, moment, depth, role == "param", expected, passed)
        if step["kind"] in ("write", "needs_level_2") and step.get("time_ns") is None:
            step["at_ns"] = moment  # the moment its value is about (level 2 has the write's own)
        return step

    def local(self, row: sqlite3.Row, var: str, moment: int, depth: int, param: bool, expected: str | None = None, passed: str | None = None) -> dict:
        name = var.rpartition("#")[2]
        node = row["node"]
        if self.recorded(node):
            write = self.db.execute(
                "SELECT * FROM writes WHERE frame = ? AND name = ? AND time_ns <= ? ORDER BY time_ns DESC, id DESC LIMIT 1", (row["id"], name, moment)
            ).fetchone()
            if write is None:
                if param:
                    return self.argument(row, var, depth)
                step = self.step("unknown", var, row)
                step["note"] = f"not assigned in #{row['id']} before then (its variable history has no write of {name})"
                return step
            step = self.step("write", var, row, write["line"], self.value(write["value"]), time_ns=write["time_ns"])
            sources = self.fed(row, var, write["line"], write["time_ns"], depth, previous=write["time_ns"] - 1, value=self.value(write["value"]))
            return self.follow(step, depth, ("write", write["id"]), sources)
        lines = self.graph.sites("writes", node, var)
        value = self.local_value(row, name, moment)
        if param and not lines:
            return self.argument(row, var, depth)
        if len(lines) == 1 and not param:
            value = value if value is not None else self.value_at_end(row, name, moment)
            step = self.step("write", var, row, lines[0], value)
            sources = self.fed(row, var, lines[0], moment, depth, previous=None, value=value)
            self.follow(step, depth, ("site", row["id"], var, lines[0], value), sources)
            returned = [child for child in step["children"] if child["kind"] == "returned_by"]
            if value is None and len(returned) == 1 and returned[0]["value"] is not None and self.graph.call_assignment(node, lines[0], name):
                step["value"] = returned[0]["value"]  # `x = f(...)`: what f returned
            step["note"] = "its only assignment"
            if step["value"] is None:
                self.unrecorded(step, node)
            return step
        if not lines:
            step = self.step("unknown", var, row, value=value)
            step["note"] = f"the static graph has no assignment of {name} in {node}"
            return step
        if param and expected is not None:
            snapshot = self.args(row).get(name)
            if isinstance(snapshot, int) and self.value(snapshot) == expected:
                step = self.argument(row, var, depth)
                note(step, f"still the value it was passed, though it may be reassigned on line {', '.join(map(str, lines))}")
                return step
        known = f"{name} was {clip(value, 30)} as the exception left #{row['id']}" if value is not None else None
        if value is None and not param:
            value = self.value_at_end(row, name, moment)
            known = f"#{row['id']} returned it: {clip(value, 30)}" if value is not None else None
        if value is None and passed is not None and not param:
            value, known = passed, f"{name} was {clip(passed, 30)} when it was passed on"
        left, reasons = list(lines), []
        if value is not None and not param:
            line, left, reasons = self.pick_line(row, var, lines, value, moment)
            if line is not None:
                step = self.step("write", var, row, line, value)
                sources = self.fed(row, var, line, moment, depth, previous=None, value=value)
                self.follow(step, depth, ("site", row["id"], var, line, value), sources)
                step["note"] = f"assigned on lines {', '.join(map(str, lines))}; inferred: " + "; ".join([known, *reasons])
                return step
        step = self.step("needs_level_2", var, row, value=value, rerun=node, rerun_scope=self.rerun_scope(node))
        ran = {line for line in left if self.calls_at(row["id"], self.graph.span(node, line), moment)}
        step["candidates"] = [{"line": line, "code": self.graph.code(node, line), "ran": line in ran} for line in left]
        history = "in its variable history (level 2)" if self.run["level"] < 2 else f"in its variable history, but {short(node)} is not in this run's scope"
        if not param:
            step["note"] = f"assigned on several lines: which one made this value is {history}" + "".join(f"; {reason}" for reason in reasons)
            return step
        step["note"] = f"reassigned in the function, or still its argument: which, is {history}"
        return self.follow(step, depth, None, [labeled("its argument", lambda: self.argument(row, var, depth + 1))])

    def values_on_the_way(self, top: dict) -> list[dict]:
        """The values each variable met in the walk took, in time order (repeats in a row merged),
        the variables that took several different values first: the culprit is often the odd
        value among them. `[{"name", "node", "variable", "values"}]`, at most VALUES of them."""
        found: dict[str, dict] = {}
        pending = [top]
        while pending:
            step = pending.pop(0)
            pending[:0] = step["children"]
            if step["value"] is None or not step["variable"] or step["frame"] is None:
                continue
            row = self.frame(step["frame"])
            when = step.get("time_ns", step.get("at_ns"))
            if when is None:
                when = row["start_ns"] if step["kind"] == "argument" else self.end_of(row)
            shared = self.graph.role(step["var"]) in ("global", "instance_attr", "class_attr") if step["var"] else False
            name = step["variable"] if shared else f"{short(step['node'])}.{step['variable']}"
            entry = found.setdefault(name, {"name": name, "node": None if shared else step["node"], "variable": step["variable"], "events": set()})
            entry["events"].add((when, step["frame"], step["value"]))
        entries = []
        for order, entry in enumerate(found.values()):
            values: list[str] = []
            for _, _, value in sorted(entry.pop("events")):
                if not values or values[-1] != value:
                    values.append(value)
            entries.append((-len(set(values)), order, {**entry, "values": values}))
        return [entry for *_, entry in sorted(entries, key=lambda item: item[:2])[:VALUES]]

    def rerun_scope(self, node: str) -> list[str]:
        """The scope to run again with, to record `node`'s locals: this run's, and `node`."""
        scope = list(self.run["scope"] or []) if self.run["level"] >= 2 else []
        return scope + [node] if node not in scope else scope

    def unrecorded(self, step: dict, node: str) -> None:
        """Say why a local's value is unknown, and how to record it."""
        level = self.run["level"]
        if level == 0:
            why = "level 0 records no values"
        elif level >= 2:
            why = f"{short(node)} is not in this run's scope: its locals are recorded only as an exception left them, or not at all"
        else:
            why = "level 1 records locals only as an exception left them, or not at all"
        note(step, f"its value is not recorded, as {why}")
        step["rerun"], step["rerun_scope"] = node, self.rerun_scope(node)

    def value_at_end(self, row: sqlite3.Row, name: str, moment: int) -> str | None:
        """A local's value at the end of frame `row`, when the call returned it (`return name`).
        The end's moment less 1 counts: the return statement's sources are asked for just before."""
        if moment < self.end_of(row) - 1 or row["end_kind"] != "return" or row["args"] is None:
            return None
        node = row["node"]
        lines = self.graph.sites("writes", node, f"{node}#return")
        if not lines:
            return None
        result = self.value(row["result"])
        line, candidates = self.return_line(row, lines, result)
        for candidate in [line] if line is not None else [found["line"] for found in candidates]:
            statement = self.graph.statement(node, candidate)
            if not (isinstance(statement, ast.Return) and isinstance(statement.value, ast.Name) and statement.value.id == name):
                return None
        return result

    def pick_line(self, row: sqlite3.Row, var: str, lines: list[int], value: str, moment: int) -> tuple[int | None, list[int], list[str]]:
        """Which of the lines assigning a local made `value` (its value at `moment`, known from
        elsewhere), without line events: not one assigning a literal (or a parameter never
        reassigned) of another value; among those left, the one whose call, the last made on it
        before then, returned exactly `value`. Returns the line (or None), the lines left, and
        the reasons."""
        node, name = row["node"], var.rpartition("#")[2]
        args = self.args(row)
        left, matched, reasons = [], [], []
        for line in lines:
            statement = self.graph.statement(node, line)
            assigned = None
            if isinstance(statement, ast.Assign) and len(statement.targets) == 1 and isinstance(statement.targets[0], ast.Name) and statement.targets[0].id == name:
                assigned = self.expression_value(node, statement.value, args)
            elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name) and statement.target.id == name and statement.value is not None:
                assigned = self.expression_value(node, statement.value, args)
            if assigned is not None and assigned != value:
                reasons.append(f"line {line} assigns {clip(assigned, 30)}")
                continue
            left.append(line)
            if self.graph.call_assignment(node, line, name):
                span = self.graph.span(node, line)
                callees = {edge["source"].rpartition("#")[0] for edge in self.graph.edges_at("returns", var, span)}
                last = next((call for call in self.calls_at(row["id"], span, moment) if call["node"] in callees), None)
                if last is not None and last["end_kind"] == "return" and last["args"] is not None and self.value(last["result"]) == value:
                    matched.append((line, f"{short(last['node'])} #{last['id']}, the last call on line {line}, returned {clip(value, 30)}"))
        if len(left) == 1:
            return left[0], left, reasons + [reason for line, reason in matched if line == left[0]]
        if len(matched) == 1:
            return matched[0][0], left, reasons + [matched[0][1]]
        return None, left, reasons

    def argument(self, row: sqlite3.Row, var: str, depth: int) -> dict:
        name = var.rpartition("#")[2]
        snapshot = self.args(row).get(name)
        value = self.value(snapshot) if isinstance(snapshot, int) else None
        if row["caller"] is None:
            step = self.step("argument", var, row, value=value)
            step["note"] = "called from outside project code (the program's entry, a new thread, or a library calling back)"
            return step
        caller = self.frame(row["caller"])
        span = self.graph.span(caller["node"], row["line"])
        step = self.step("argument", var, row, value=value, caller={"frame": caller["id"], "node": caller["node"], "line": row["line"], "code": self.graph.code(caller["node"], row["line"])})
        step["line"] = None  # the line is the caller's: see `caller`
        passed = []
        for edge in self.graph.edges_at("passes_arg", var, span):
            source = edge["source"]
            if self.graph.role(source) == "return" and self.graph.owner(source) != caller["node"]:
                # f(g(x)): what g returned, a call made from the same line just before
                call = next((call for call in self.calls_at(caller["id"], span, row["start_ns"]) if call["node"] == self.graph.owner(source)), None)
                if call is not None:
                    passed.append(labeled(f"what {short(call['node'])} returned", lambda call=call: self.returned(call, depth + 1)))
            elif self.graph.role(source) in ("global", "instance_attr", "class_attr") or self.graph.owner(source) == caller["node"]:
                passed.append(labeled(self.graph.label(source), lambda source=source: self.explain(caller["id"], source, row["start_ns"], depth + 1, expected=value, passed=value if self._passed_as_is(caller["node"], row, source) else None)))
        if caller["node"] == row["node"] and self._read_at(var, row["node"], span):
            # A recursive call, `f(n - 1)`: the graph has no self-loops, so no `passes_arg` from
            # n to itself; n is read on this line, so it likely fed the argument.
            def itself():
                step = self.explain(caller["id"], var, row["start_ns"], depth + 1)
                note(step, "read where it recursed")
                return step

            passed.append(labeled(self.graph.label(var), itself))
        if not passed:
            # f(x) for x in xs: comprehension variables have no node, so no `passes_arg`; the
            # value is an item of what the comprehension iterates over.
            for iterated in self.graph.iterated(caller["node"], row["line"]):
                def item(iterated=iterated):
                    step = self.explain(caller["id"], iterated, row["start_ns"], depth + 1)
                    note(step, "the comprehension on that line iterates over it (its own variable is not tracked)")
                    return step

                passed.append(labeled(self.graph.label(iterated), item))
        if passed:
            return self.follow(step, depth, ("arg", row["id"], name), passed)
        if any(edge["source"] == caller["node"] for edge in self.graph.edges_at("calls", row["node"], span)):
            literal = self.step("literal", None, caller, row["line"])
            if name == "self" and row["node"].endswith(".__init__"):
                literal["note"] = "the new object, created by this call"
            else:
                literal["note"] = "a literal, an expression of constants, or the default value"
            step["children"].append(literal)
        else:
            unknown = self.step("unknown", None, caller, row["line"])
            unknown["note"] = f"the static graph has no call of {row['node']} here (a callback, or a call through code it cannot follow): the argument's expression is not known"
            step["children"].append(unknown)
        return step

    def returned(self, row: sqlite3.Row, depth: int, var: str | None = None) -> dict:
        """What frame `row` returned, and the return statement that made it."""
        node = row["node"]
        var = var or f"{node}#return"
        value = self.value(row["result"]) if row["end_kind"] == "return" and row["args"] is not None else None
        lines = self.graph.sites("writes", node, var)
        step = self.step("returned_by", var, row, value=value)
        step["variable"] = "return"
        if row["end_kind"] != "return":
            step["note"] = f"it did not return: it ended with {row['end_kind']}"
            return step
        if not lines:
            step["note"] = "no `return` with a value in it: it returned None (or what its generator returned)"
            return step
        if ("return", row["id"]) in self.seen:
            step["seen"] = True
            return step
        line, candidates = self.return_line(row, lines, value)
        if line is None:
            self.seen.add(("return", row["id"]))
            step["candidates"] = candidates
            if self.recorded(node):  # what its locals were when it returned helps to tell
                rows = self.db.execute(
                    "SELECT name, line, value FROM writes WHERE id IN (SELECT MAX(id) FROM writes WHERE frame = ? GROUP BY name) ORDER BY id DESC LIMIT 6", (row["id"],)
                ).fetchall()
                step["locals"] = [{"name": found["name"], "line": found["line"], "value": self.value(found["value"])} for found in rows]
            step["note"] = "several return statements could have made it: which one ran is not recorded"
            return step
        step["line"], step["code"] = line, self.graph.code(node, line)
        if len(lines) > 1:
            step["note"] = f"of {len(lines)} return statements, the only one that fits the trace"
        self.follow(step, depth, ("return", row["id"]), self.fed(row, var, line, self.end_of(row), depth, previous=None, value=value))
        statement = self.graph.statement(node, line)
        if value is not None and isinstance(statement, ast.Return) and isinstance(statement.value, ast.Name):
            for child in step["children"]:  # `return shown`: shown was what it returned
                if child["variable"] == statement.value.id and child["frame"] == row["id"] and child["value"] is None:
                    child["value"] = value
                    if child["note"] and child["note"].startswith("its only assignment;"):
                        child["note"] = "its only assignment"
                        child.pop("rerun", None), child.pop("rerun_scope", None)
        return step

    def return_line(self, row: sqlite3.Row, lines: list[int], value: str | None) -> tuple[int | None, list[dict]]:
        """Which of a function's return statements ended frame `row`, or the ones that may have,
        from what the trace shows of the frame: no line events below level 2, but its calls,
        its writes (level 2), its arguments and its result. A return statement:
        - holding the last call the frame made ran (`return f(x)`);
        - that calls project code did not run if no call was made from it (`return n * f(n - 1)`
          vs `return 1`);
        - above the last thing the frame did (a call, a write), outside a loop around both, did not;
        - that returns a literal, or a parameter never reassigned, did not if that value differs
          from the result."""
        node = row["node"]
        if len(lines) == 1:
            return lines[0], []
        spans = {line: self.graph.span(node, line) for line in lines}
        last = self.db.execute("SELECT line, end_ns AS time_ns FROM frames WHERE caller = ? ORDER BY end_ns DESC, id DESC LIMIT 1", (row["id"],)).fetchone()
        if last is not None:
            for line, span in spans.items():
                if span[0] <= last["line"] <= span[1]:
                    return line, []
        if self.recorded(node):
            wrote = self.db.execute("SELECT line, time_ns FROM writes WHERE frame = ? ORDER BY time_ns DESC, id DESC LIMIT 1", (row["id"],)).fetchone()
            if wrote is not None and (last is None or wrote["time_ns"] > last["time_ns"]):
                last = wrote
        calling = {site["line"] for edge in self.graph.out.get(("calls", node), []) for site in edge.get("sites", [])}
        args = self.args(row)
        left = []
        for line, span in spans.items():
            if any(span[0] <= site <= span[1] for site in calling) and not self.calls_at(row["id"], span, self.end_of(row)):
                continue
            if last is not None and span[1] < last["line"] and not self.graph.in_one_loop(node, line, last["line"]):
                continue
            same = self.returns_value(node, line, value, args)
            if same is False:
                continue
            left.append({"line": line, "code": self.graph.code(node, line), **({"same_value": True} if same else {})})
        if len(left) == 1:
            return left[0]["line"], []
        return None, left or [{"line": line, "code": self.graph.code(node, line)} for line in lines]

    def returns_value(self, node: str, line: int, value: str | None, args: dict) -> bool | None:
        """Whether the return statement at `line` gives `value` (a snapshot's repr): known for a
        literal, or a parameter the function never reassigns; None otherwise."""
        if value is None:
            return None
        statement = self.graph.statement(node, line)
        if not isinstance(statement, ast.Return) or statement.value is None:
            return None
        known = self.expression_value(node, statement.value, args)
        return None if known is None else known == value

    def expression_value(self, node: str, expression: ast.expr, args: dict) -> str | None:
        """The value of an expression in a call of `node` with `args`, as a snapshot's repr, when
        the trace tells it without line events: a literal, or a parameter never reassigned."""
        if isinstance(expression, ast.Name):
            var = f"{node}#{expression.id}"
            if self.graph.role(var) != "param" or self.graph.sites("writes", node, var) or not isinstance(args.get(expression.id), int):
                return None
            return self.value(args[expression.id])
        try:
            return repr(ast.literal_eval(expression))
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            return None

    def fed(self, row: sqlite3.Row, var: str, line: int, moment: int, depth: int, previous: int | None, value: str | None = None) -> list:
        """What fed `var` at `line` of frame `row`: steps to make, as callables. `previous`, for
        an augmented assignment (`x += y`), is the moment to ask for its value before; `value`
        is the value it got, when recorded."""
        node = row["node"]
        span = self.graph.span(node, line)
        sources: list = []
        returns = self.graph.edges_at("returns", var, span)
        flows = [edge["source"] for edge in self.graph.edges_at("flows_to", var, span)]
        calls = self.calls_at(row["id"], span, moment)
        callees = {edge["source"].rpartition("#")[0] for edge in returns}
        if var == f"{node}#return" and any(call["node"] == node for call in calls):
            callees.add(node)  # `return n * f(n - 1)`: the graph has no self-loops, the trace has the call
        if callees:
            matched = [call for call in calls if call["node"] in callees]
            picked: dict[str, sqlite3.Row] = {}
            for call in matched:
                picked.setdefault(call["node"], call)  # the last call of each, before the moment
            for call in picked.values():
                sources.append(labeled(f"what {short(call['node'])} returned", lambda call=call: self.returned(call, depth + 1)))
            if not picked and calls:
                guess = calls[0]

                def unexpected(guess=guess):
                    step = self.returned(guess, depth + 1)
                    note(step, f"the static graph expected {', '.join(sorted(callees))} here")
                    return step

                sources.append(labeled(f"what {short(guess['node'])} returned", unexpected))
            elif not picked:
                def missing():
                    step = self.step("unknown", None, row, line)
                    step["note"] = f"the static graph expects a call of {', '.join(sorted(callees))} here, but none was recorded before then (it raised, or the code that ran is not the project's)"
                    return step

                sources.append(labeled("?", missing))
        if value is not None and not callees:
            # `return make(...).build(x)`: a call the analysis could not resolve, but a call made
            # on that line returned exactly this value: most likely the one that made it.
            same = next((call for call in calls if call["end_kind"] == "return" and call["args"] is not None and self.value(call["result"]) == value), None)
            if same is not None:
                def returned_same(call=same):
                    step = self.returned(call, depth + 1)
                    note(step, "a call on that line that returned this very value")
                    return step

                sources.append(labeled(f"what {short(same['node'])} returned", returned_same))
        for source in flows:
            sources.append(labeled(self.graph.label(source), lambda source=source: self.explain(row["id"], source, moment - 1, depth + 1)))
        if previous is not None and self._augmented(node, line, var):
            def before():
                step = self.explain(row["id"], var, previous, depth + 1)
                step["previous"] = True  # x += y: x before
                return step

            sources.append(labeled(f"{self.graph.label(var)} before", before))
        if not sources and calls:  # a line the analysis has no data edge for: the calls on it
            for call in calls[:1]:
                def guessed(call=call):
                    step = self.returned(call, depth + 1)
                    note(step, "no static edge into it at this line: a call made there")
                    return step

                sources.append(labeled(f"what {short(call['node'])} returned", guessed))
        if not sources:
            reads = [edge["source"] for edge in self.graph.into.get(("reads", node), []) if edge["source"] != var and any(span[0] <= site["line"] <= span[1] for site in edge.get("sites", []))]
            if reads and self._changed_in_place(node, line, var):
                for source in reads:
                    def read(source=source):
                        step = self.explain(row["id"], source, moment - 1, depth + 1)
                        note(step, "read by the line that changed it in place")
                        return step

                    sources.append(labeled(self.graph.label(source), read))
            else:
                def literal():
                    step = self.step("literal", None, row, line)
                    step["note"] = "no tracked variable: a literal, an imported name, or library calls"
                    return step

                sources.append(literal)
        return sources

    def shared(self, row: sqlite3.Row, var: str, moment: int, depth: int) -> dict:
        """A global or an attribute, as frame `row` saw it at `moment`: the last call of the code
        that writes it (static `writes` edges) that ran before, on the same object for an
        attribute of `self` when level 1 recorded `self`."""
        role = self.graph.role(var)
        kind = "attribute_written_by" if role in ("instance_attr", "class_attr") else "global_written_by"
        name = var.rpartition("#")[2]
        identity = self.identity(row) if role == "instance_attr" else None
        best = None  # {"when", "row", "lines", "line", "value", "same"}
        for writer in sorted({edge["source"] for edge in self.graph.into.get(("writes", var), [])}):
            lines = self.graph.sites("writes", writer, var)
            if ":" not in writer and self.recorded(writer):
                # A module body at level 2: its globals are its locals, and so are a class body's
                # attributes (a class body runs as the module). Only writes at its own assignment
                # lines count: a change made by a function shows up at the module's next line.
                bodies = [writer] + ([self.graph.owner(var)] if role == "class_attr" else [])
                found = self.db.execute(
                    f"SELECT w.*, f.id AS fid FROM writes w JOIN frames f ON f.id = w.frame WHERE f.node IN ({','.join('?' * len(bodies))}) AND w.name = ? AND w.time_ns < ?"
                    f" AND w.line IN ({','.join('?' * len(lines))}) ORDER BY w.time_ns DESC, w.id DESC LIMIT 1",
                    (*bodies, name, moment, *lines),
                ).fetchone() if lines else None
                if found is not None and (best is None or found["time_ns"] > best["when"]):
                    best = {"when": found["time_ns"], "row": self.frame(found["fid"]), "lines": lines, "line": found["line"], "value": self.value(found["value"]), "same": None}
                continue
            for candidate in self.db.execute("SELECT * FROM frames WHERE node = ? AND start_ns < ? ORDER BY start_ns DESC, id DESC LIMIT ?", (writer, moment, SCAN)):
                same = None
                if identity is not None and candidate["args"] is not None:
                    theirs = self.args(candidate).get("self")
                    if theirs is not None and self.value(theirs) != identity:
                        continue  # another object
                    same = theirs is not None  # else not a method of it: cannot tell
                # A module body runs first and goes on running: what it sets, it sets on import.
                when = candidate["start_ns"] if ":" not in writer else min(self.end_of(candidate), moment)
                if best is None or when > best["when"]:
                    best = {"when": when, "row": candidate, "lines": lines, "line": lines[0] if len(lines) == 1 else None, "value": None, "same": same}
                break
        if best is None:
            step = self.step("unknown", var, row)
            where = "on this object " if identity else ""
            step["note"] = f"no recorded call set {self.graph.label(var)} {where}before then: set outside project code, or in a way the static graph does not see (setattr, a library)"
            return step
        writer_row, line = best["row"], best["line"]
        step = self.step(kind, var, writer_row, line, best["value"], time_ns=best["when"])
        notes = []
        if identity is not None:
            notes.append("on the same object" if best["same"] else "on some object (which one is not recorded)")
        elif role == "instance_attr":
            notes.append("on any object of its class: " + ("level 0 does not record `self`" if self.run["level"] == 0 else "which one is not known here"))
        if ":" in writer_row["node"] and (writer_row["end_ns"] is None or writer_row["end_ns"] > moment):
            notes.append("it was still running then: it may have set it after")
        if line is None:
            step["candidates"] = [{"line": candidate, "code": self.graph.code(writer_row["node"], candidate)} for candidate in best["lines"]]
            notes.append("it sets it on several lines: which one ran last is not recorded")
        step["note"] = "; ".join(notes) or None
        changed = self.changed_in_place(var, best["when"], moment, identity)
        if changed:
            step["changed_in_place"] = changed
        if line is None:
            return step
        before = best["when"] - 1 if best["value"] is not None else writer_row["start_ns"] - 1
        sources = self.fed(writer_row, var, line, min(self.end_of(writer_row), moment), depth, previous=before, value=best["value"])
        return self.follow(step, depth, ("shared", writer_row["id"], var, line), sources)

    def changed_in_place(self, var: str, after: int, moment: int, identity: str | None) -> dict | None:
        """The last call that changed a global or attribute in place (`x.append(...)`,
        `x.update(...)`) between `after` and `moment`: the static graph sees these as reads.
        Returns `{"frame", "node", "line", "code", "calls"}`, or None."""
        name = re.escape(var.rpartition("#")[2])
        mutation = re.compile(rf"\b{name}\s*\.\s*({'|'.join(MUTATORS)})\s*\(")
        found, calls = None, 0
        for edge in self.graph.out.get(("reads", var), []):
            function = edge["target"]
            lines = sorted({site["line"] for site in edge.get("sites", []) if mutation.search(self.graph.code(function, site["line"]) or "")})
            if not lines:
                continue
            for candidate in self.db.execute("SELECT * FROM frames WHERE node = ? AND start_ns > ? AND start_ns < ? ORDER BY start_ns DESC, id DESC LIMIT ?", (function, after, moment, SCAN)):
                if identity is not None and candidate["args"] is not None:
                    theirs = self.args(candidate).get("self")
                    if theirs is not None and self.value(theirs) != identity:
                        continue
                calls += 1
                if found is None or candidate["start_ns"] > found[0]["start_ns"]:
                    found = (candidate, lines)
        if found is None:
            return None
        row, lines = found
        return {"frame": row["id"], "node": row["node"], "line": lines[0] if len(lines) == 1 else None, "code": self.graph.code(row["node"], lines[0]) if len(lines) == 1 else None, "lines": lines, "calls": calls}

    def identity(self, row: sqlite3.Row) -> str | None:
        """The `self` of frame `row`, as level 1 recorded it: its identity snapshot."""
        snapshot = self.args(row).get("self")
        return self.value(snapshot) if isinstance(snapshot, int) else None

    def _passed_as_is(self, node: str, callee: sqlite3.Row, source: str) -> bool:
        """Whether `source`, a variable of `node`, is read once on the line that called frame
        `callee`, as an argument of a call of that function's name (`deposit(balance, ...)`): the
        argument's value is then the variable's."""
        name, function = source.rpartition("#")[2], callee["node"].rpartition(":")[2].rpartition(".")[2]
        statement = self.graph.statement(node, callee["line"]) if callee["line"] else None
        if statement is None:
            return False
        reads = [found for found in ast.walk(statement) if isinstance(found, ast.Name) and found.id == name and isinstance(found.ctx, ast.Load)]
        if len(reads) != 1:
            return False
        for call in ast.walk(statement):
            if isinstance(call, ast.Call) and (getattr(call.func, "id", None) or getattr(call.func, "attr", None)) == function:
                if any(argument is reads[0] for argument in [*call.args, *(keyword.value for keyword in call.keywords)]):
                    return True
        return False

    def _read_at(self, var: str, node: str, span: tuple[int, int]) -> bool:
        return any(edge["target"] == node and any(span[0] <= site["line"] <= span[1] for site in edge.get("sites", [])) for edge in self.graph.out.get(("reads", var), []))

    def _augmented(self, node: str, line: int, var: str) -> bool:
        code = self.graph.code(node, line) or ""
        name = re.escape(var.rpartition("#")[2])
        return re.match(rf"\s*(?:[\w.]+\.)?{name}{AUGMENTED}", code) is not None

    def _changed_in_place(self, node: str, line: int, var: str) -> bool:
        code = self.graph.code(node, line) or ""
        name = re.escape(var.rpartition("#")[2])
        return re.search(rf"\b{name}\s*(\.|\[)", code) is not None
