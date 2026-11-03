"""Fault localization answers as text (localize.py makes them), for `luciole trace` and `luciole mcp`.

`hints` says how the next-step lines name a query: "cli" (`luciole trace RUN ...`) or "mcp"
(the tools, with their arguments)."""

from __future__ import annotations

from luciole.localize import normal
from luciole.trace_text import outcome_text, plural, short_value

SOURCE = {"recorded": "outcomes recorded by pytest --luciole", "inferred": "outcomes inferred from the test functions' calls"}


def short(node: str | None) -> str:
    return node.rsplit(":", 1)[-1] if node else "?"


def call_text(call: dict, chars: int = 60) -> str:
    """`#12 total(items=[…]) -> 28`, values cut short."""
    args = ", ".join(f"{name}={short_value(value, chars)}" for name, value in (call.get("args") or {}).items())
    head = f"#{call['frame']} {short(call['node'])}({args})" if "args" in call else f"#{call['frame']} {short(call['node'])}"
    outcome = outcome_text(call, chars)
    return head if outcome == "returned" else f"{head} {outcome}"


def render_suspects(found: dict, hints: str = "cli", run: str = "RUN") -> str:
    tests = f"{plural(found['failed'] + found['passed'], 'test')}" + (f" in {plural(found['processes'], 'process')}" if found["processes"] > 1 else "")
    counts = f"{found['failed']} failed, {found['passed']} passed"
    if found["method"] == "no_tests":
        return "no tests in this run: no test outcomes recorded and no test functions (test* in test files) ran"
    if found["method"] == "no_failing":
        return f"{tests} ({counts}; {SOURCE[found['source']]}): no test failed, nothing to localize"
    lines = [f"suspects from {tests} ({counts}; {SOURCE[found['source']]})"]
    if found["method"] == "no_passing":
        lines.append("  no passing test: Ochiai cannot tell functions apart (every function the failing tests ran scores the same);")
        lines.append("  ranked by failing tests that ran it, then fewest calling functions (the most specific first)")
        lines.append(f"  {'rank':>4}  {'failed':>6}  {'callers':>7}  function")
    else:
        lines.append("  Ochiai: failed(f) / sqrt(failed tests × (failed(f) + passed(f))); failed/passed: tests that ran it")
        lines.append(f"  {'rank':>4}  {'score':>5}  {'failed':>6}  {'passed':>6}  function")
    multi = found["processes"] > 1
    for item in found["suspects"]:
        place = f"  {item['file']}:{item['line']}" if item.get("file") else ""
        if found["method"] == "no_passing":
            lines.append(f"  {item['rank']:>4}  {item['failed']:>3}/{found['failed']:<2}  {item['callers']:>7}  {item['node']}{place}")
        else:
            lines.append(f"  {item['rank']:>4}  {item['score']:5.2f}  {item['failed']:>3}/{found['failed']:<2}  {item['passed']:>3}/{found['passed']:<2}  {item['node']}{place}")
        if item.get("lines"):
            ranked = ", ".join(f"{line['line']} ({line['score']:.2f})" for line in item["lines"])
            lines.append(f"          lines assigning a variable, by the same score (level 2): {ranked}")
        example = item.get("example")
        if example:
            lines += [f"          {text}" for text in example_text(example, multi)]
    if found["ties"] > 1:
        lines.append(f"  {found['ties']} functions share the top {'score' if found['method'] == 'ochiai' else 'count'}: the tests do not tell them apart")
    top = found["suspects"][0]["node"] if found["suspects"] else None
    if top:
        if hints == "mcp":
            lines.append(f"next: `compare` sets the first failing test beside the closest passing one; `calls` function={top} lists its calls with their values")
        else:
            lines.append(f"next: luciole trace {run} compare (the first failing test beside the closest passing one); luciole trace {run} calls {top}" if found["processes"] == 1 else f"next: luciole trace {run} compare (the first failing test beside the closest passing one)")
    return "\n".join(lines)


def example_text(example: dict, multi: bool) -> list[str]:
    def where(side: dict) -> str:
        return f"{side['test']}" + (f", pid {side['pid']}" if multi else "")

    failing, passing = example["failing"], example["passing"]
    if example["kind"] == "like_passing":
        return [f"its calls in failing tests were all made in passing tests too, e.g. {call_text(failing['call'])}"]
    if example["kind"] == "no_passing_calls":
        return [f"in a failing test ({where(failing)}): {call_text(failing['call'])}"]
    head = "same arguments, another result" if example["kind"] == "same_arguments" else "a call no passing test made"
    return [
        f"{head}: {call_text(failing['call'])}   in {where(failing)}",
        f"  in a passing test: {call_text(passing['call'])}   in {where(passing)}",
    ]


KINDS = {
    "result": "returned something else",
    "raised": "raised on one side only",
    "ending": "ended differently",
    "call_path": "calls differ",
}


def render_compare(found: dict, hints: str = "cli", files: tuple[str, str] = ("FILE_A", "FILE_B")) -> str:
    """`files`: the traces of A and B, for the next-step lines of `luciole trace`."""
    a, b = found["a"], found["b"]
    test_a, test_b = found.get("test_a"), found.get("test_b")

    def side(call: dict, test: dict | None) -> str:
        if test:
            return f"#{call['frame']} {test['nodeid']} ({test['outcome']}, pid {test['pid']})"
        return f"#{call['frame']} {call['node']} {outcome_text(call, 60)}"

    lines = [f"A: {side(a, test_a)}", f"B: {side(b, test_b)}" + (f"  ({found['picked']})" if found.get("picked") else "")]
    if a.get("args") and b.get("args"):
        changed = [name for name in a["args"] if normal(b["args"].get(name)) != normal(a["args"][name])]
        if changed:
            lines.append("inputs: " + ", ".join(f"{name}={short_value(a['args'][name], 40)} vs {short_value(b['args'].get(name), 40)}" for name in changed[:4]))
    divergence = found["divergence"]
    if divergence is None:
        lines.append(f"no divergence: the {plural(found['compared'], 'call')} compared match, arguments, results and calls" + (" (cut at the limit)" if found["cut"] else ""))
        return "\n".join(lines)
    kind = divergence["kind"]
    if kind == "call_path" and "side" not in divergence:
        lines.append(f"different functions at the top: {a['node']} vs {b['node']}")
        return "\n".join(lines)
    lines.append(f"first divergence ({plural(found['compared'], 'call')} compared): {short(divergence['node'])} {KINDS[kind]}")
    if kind == "call_path":
        mine, other = ("A", "B") if divergence["side"] == "a" else ("B", "A")
        call = divergence["call_a" if divergence["side"] == "a" else "call_b"]
        instead = divergence.get("call_b" if divergence["side"] == "a" else "call_a")
        lines.append(f"  {mine} made {call_text(call)}")
        lines.append(f"  {other} " + (f"made {call_text(instead)} at that point" if instead else "made no further call there"))
    else:
        if divergence.get("same_arguments"):
            lines[-1] += ", with the same arguments"
        elif divergence.get("same_arguments") is False:
            lines[-1] += ", its arguments differ too"
        lines.append(f"  A {call_text(divergence['call_a'])}")
        lines.append(f"  B {call_text(divergence['call_b'])}")
    path = divergence["path"]
    if path:
        steps = []
        for item in path:
            text = f"{short(item['node'])} #{item['a']}/#{item['b']}"
            if item["arguments_differ"]:
                text += f" ({', '.join(item['arguments_differ'][:3])} differ{'s' if len(item['arguments_differ']) == 1 else ''})"
            for key, count in (item.get("extra_calls") or {}).items():
                which, node = key.split(":", 1)
                text += f" ({which.upper()} made {plural(count, 'more call')} of {short(node)})"
            steps.append(text)
        lines.append("path (A/B frames): " + " > ".join(steps))
    first = found.get("first_arguments")
    if first and kind != "call_path" and first["a"] != divergence["a"]:
        lines.append(f"arguments first differ at {short(first['node'])} #{first['a']}/#{first['b']}: " + ", ".join(
            f"{name}={short_value(first['call_a']['args'].get(name), 40)} vs {short_value(first['call_b']['args'].get(name), 40)}" for name in first["path"][-1]["arguments_differ"][:3]
        ))
    on_a = bool(divergence.get("call_a"))
    call = divergence["call_a"] if on_a else divergence["call_b"]
    frame, name = call["frame"], "A" if on_a else "B"
    variable = "return" if kind == "result" else "NAME"
    if hints == "mcp":
        pid = ((test_a if on_a else test_b) or {}).get("pid") or found.get("pid_a" if on_a else "pid_b")
        where = f" process={pid}" if pid else ""
        run = found.get("run_a" if on_a else "run_b")  # two runs compared: say which
        if run:
            where = f" run={run}" + where
        lines.append(f"next: `origin` frame={frame}{where} variable={variable} shows where {name}'s value came from; `frame` frame={frame}{where} shows the call in full")
    else:
        file = files[0] if on_a else files[1]
        lines.append(f"next: luciole trace {file} origin - {variable} --frame {frame}; luciole trace {file} frame {frame}")
    return "\n".join(lines)
