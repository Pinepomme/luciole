"""Traces as text, for `luciole trace` and `luciole mcp`: what trace.py's queries return, made short
enough for a person or a model to read."""

from __future__ import annotations

import time


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
