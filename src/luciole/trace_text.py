"""Traces as text, for `luciole trace` and `luciole mcp`: what trace.py's queries return, made short
enough for a person or a model to read."""

from __future__ import annotations

import re
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


# Compact answers: values cut to these many characters (`frame` shows them as recorded).
ARG_CHARS = 80
RESULT_CHARS = 120
MESSAGE_CHARS = 160
IDENTITY = re.compile(r"<(?:[\w.]+\.)?(\w+) object at 0x[0-9a-f]*?([0-9a-f]{1,4})>")
IDENTITY_SHORT = r"<\1 object …\2>"


def short_value(value: dict | None, chars: int = ARG_CHARS) -> str:
    """A value in a line of a compact answer: `<Cart object …7cb0>` for an identity, and at most
    `chars` characters (cut values end with `…`)."""
    return clip(IDENTITY.sub(IDENTITY_SHORT, value_text(value)), chars)


def cut_short(calls: list[dict]) -> bool:
    """Whether a compact answer cut some of these calls' values."""
    return any(
        len(IDENTITY.sub(IDENTITY_SHORT, value_text(value))) > chars
        for call in calls
        for value, chars in [*((value, ARG_CHARS) for value in call.get("args", {}).values()), (call.get("result"), RESULT_CHARS)]
        if value is not None
    )


def clip(text: str, chars: int) -> str:
    return text if len(text) <= chars else text[: chars - 1] + "…"


def first_line(text: str, chars: int = MESSAGE_CHARS) -> str:
    lines = text.strip().splitlines() or [""]
    return clip(lines[0] + (" …" if len(lines) > 1 else ""), chars)


def outcome_text(call: dict, chars: int | None = RESULT_CHARS) -> str:
    """`-> value`, `!! Type: message`, or `[end]`; values whole when `chars` is None."""
    if call["end"] == "return":
        if "result" not in call:
            return "returned"
        return f"-> {value_text(call['result']) if chars is None else short_value(call['result'], chars)}"
    if call["exception"]:
        exception = call["exception"]
        message = exception.get("message", "")
        text = f"!! {exception.get('type', '?')}: {message if chars is None else first_line(message)}"
        return text + ("" if call["end"] == "raise" else f" (from #{exception.get('frame')})")
    return f"[{call['end']}]"


def place(node: str | None, line: int | None) -> str:
    return f"{node}:{line}" if node else "no project caller (a root)"


def render_calls(found: dict, detail: bool = False) -> str:
    """Calls of a function. Compact (the default): grouped by call site, each call's outcome
    first, then the arguments that differ between the calls shown (the others are said once),
    values cut short. `detail`: one line per call, every value as recorded."""
    if not found["nodes"]:
        return "no calls recorded for this function"
    names = ", ".join(found["nodes"])
    head = f"{plural(found['total'], 'call')} of {names}"
    if "caller_nodes" in found:
        head += f" from {', '.join(found['caller_nodes']) or 'nothing recorded'} ({found['all']} in all)"
    if detail:
        lines = [head]
        for call in found["calls"]:
            lines.append(f"  {call_line(call, found)}")
    else:
        lines = compact_calls(found, head)
    left = found["total"] - found.get("offset", 0) - len(found["calls"])
    if left > 0:
        lines.append(f"  ... {left} more (--limit, --offset)")
    return "\n".join(lines)


def call_line(call: dict, found: dict) -> str:
    """One call as recorded: `#7 add(price=3) -> 18.0   6.2 µs, from shop:main:24 (#3), MainThread`."""
    caller = call["caller"]
    where = f"from {caller['node']}:{caller['line']} (#{caller['frame']})" if caller else "root"
    if call["origin"]:
        where += f", started by #{call['origin']}"
    args = ", ".join(f"{name}={value_text(value)}" for name, value in call.get("args", {}).items())
    name = call["node"] if len(found["nodes"]) > 1 else call["node"].rsplit(":", 1)[-1]
    head = f"#{call['frame']} {name}({args})" if "args" in call else f"#{call['frame']} {name}"
    outcome = outcome_text(call, None)
    outcome = "" if outcome == "returned" else f" {outcome}"
    return f"{head}{outcome}   {duration(call['duration_ns'])}, {where}, {call['thread']}"


def compact_calls(found: dict, head: str) -> list[str]:
    calls = found["calls"]
    sites = {(site["node"], site["line"]): site["count"] for site in found.get("callers", ())}
    if len(sites) > 1 and "caller_nodes" not in found:
        head += f", from {plural(len(sites), 'call site')}" + (" (or more)" if len(sites) == 50 else "")
    lines = [head]
    # Arguments with the same value in every call shown are said once.
    same = {}
    if len(calls) > 1 and all("args" in call for call in calls):
        first = calls[0]["args"]
        same = {name: value for name, value in first.items() if all(value_text(call["args"].get(name)) == value_text(value) and name in call["args"] for call in calls[1:])}
        if same:
            lines.append(f"  same in all {len(calls)} calls shown: " + ", ".join(f"{name}={short_value(value)}" for name, value in same.items()))
    groups: dict[tuple, list[dict]] = {}
    for call in calls:
        caller = call["caller"]
        groups.setdefault((caller["node"], caller["line"]) if caller else (None, None), []).append(call)
    several = len(found["nodes"]) > 1
    for key, members in groups.items():
        count = sites.get(key)
        lines.append(f"  from {place(*key)}" + (f" ×{count}" if count and count > 1 else ""))
        runs: list[list] = []  # consecutive calls that read the same: [text, calls]
        for call in members:
            parts = [call["node"]] if several else []
            parts.append(outcome_text(call))
            if "args" in call:
                shown = [f"{name}={short_value(value)}" for name, value in call["args"].items() if name not in same]
                if shown or not same:
                    parts.append(f" ({', '.join(shown)})")
            if call["origin"]:
                parts.append(f", started by #{call['origin']}")
            if call["thread"] not in (None, "MainThread"):
                parts.append(f", {call['thread']}")
            text = " ".join(parts[:2]) + "".join(parts[2:])
            if runs and runs[-1][0] == text:
                runs[-1][1].append(call)
            else:
                runs.append([text, [call]])
        for text, run in runs:
            first, last = run[0], run[-1]
            callers = sorted({call["caller"]["frame"] for call in run if call["caller"]})
            spent = sum(call["duration_ns"] or 0 for call in run)
            if len(run) == 1:
                ident, took = f"#{first['frame']}", duration(first["duration_ns"])
            else:
                ident, took = f"#{first['frame']}..#{last['frame']} ×{len(run)}", f"{duration(spent)} in all"
            caller = f", caller #{callers[0]}" if len(callers) == 1 else f", callers #{callers[0]}..#{callers[-1]}" if callers else ""
            lines.append(f"    {ident} {text}   {took}{caller}")
    others = [(key, count) for key, count in sites.items() if key not in groups]
    if others and "caller_nodes" not in found:
        listed = ", ".join(f"{place(*key)} ×{count}" for key, count in others[:5])
        lines.append(f"  also from {listed}" + (f", and {len(others) - 5} more call sites" if len(others) > 5 else "") + " (not shown here)")
    return lines


def render_tree(entries: list[dict], indent: int = 0) -> str:
    lines = []
    for entry in entries:
        pad = "  " * indent
        if entry.get("folded"):
            names = sorted(entry["nodes"].items(), key=lambda item: -item[1])
            listed = ", ".join(f"{node.rsplit(':', 1)[-1]}" + (f" ×{count}" if count > 1 else "") for node, count in names[:4])
            listed += f", {len(names) - 4} more functions" if len(names) > 4 else ""
            below = f", +{plural(entry['below'], 'call')} below" if entry["below"] else ""
            lines.append(f"{pad}… {plural(entry['count'], 'small call')}: {listed}  ({duration(entry['duration_ns'])} in all{below})")
            continue
        count = f" ×{entry['count']}" if entry["count"] > 1 else ""
        end = "" if entry["end"] == "return" else f" [{entry['end']}]"
        mark = "  <== " if entry.get("match") else ""
        if entry["more"]:
            more = f"  (+{plural(entry['more'], 'other call')})" if entry.get("path") and not entry.get("match") else f"  (+{plural(entry['more'], 'call')} below)"
        else:
            more = ""
        lines.append(f"{pad}{entry['node']}{count}  #{entry['frame']}  {duration(entry['duration_ns'])}{end}{more}{mark}".rstrip())
        if entry["children"]:
            lines.append(render_tree(entry["children"], indent + 1))
    return "\n".join(lines)


def render_focus(found: dict) -> str:
    """The paths to the calls of the functions trace.focus() matched, from the top."""
    if not found["nodes"]:
        return "no function matching that name ran"
    lines = [f"{plural(found['total'], 'call')} of {', '.join(found['nodes'][:5])}" + (f" and {len(found['nodes']) - 5} more" if len(found["nodes"]) > 5 else "")]
    if not found["shown"]:
        return lines[0] + f", none below #{found['start']}"
    below = f"; below #{found['start']}" if found["start"] is not None else ""
    shown = f"the first {found['shown']}" if found["limited"] else plural(found["shown"], "call") if below else "all"
    lines[0] += f"{below}: the paths to {shown}, from the top (<== marks them; other calls are counted, not shown)"
    lines.append(render_tree(found["tree"]))
    return "\n".join(lines)


def render_traceback(found: dict | None) -> str:
    if found is None:
        return "no exceptions"
    e = found["exception"]
    lines = [f"!{e['id']} {e['type']}: {e['message']}"]
    chain = found.get("chain") or []
    outer = None  # the exception raised from this one, if any
    for index, link in enumerate(chain):
        if link["id"] == e["id"] and index:
            outer = chain[index - 1]
    if len(chain) > 1:
        which = "the original error" if chain[-1]["id"] == e["id"] else "the outermost" if chain[0]["id"] == e["id"] else "one"
        lines.append(f"  {which} of a chain of {len(chain)}, outermost first (`traceback` with exception=N shows another):")
        for link in chain:
            where = f"raised in {link['node']}:{link['line']}" if link["frame"] else f"line {link['line']}"
            handled = "not handled" if not link["handled_in"] else f"handled in {link['handled_node']}"
            here = "  <== this one" if link["id"] == e["id"] else ""
            lines.append(f"    !{link['id']} {link['type']}: {first_line(link['message'], 100)}   {where}, {handled}{here}")
    elif e["cause"]:
        lines.append(f"  {'caused by' if e['chain'] == 'cause' else 'while handling'} !{e['cause']}")
    for frame in found["frames"]:
        here = "raised here" if frame["end"] == "raise" else "passed through"
        lines.append(f"  in {frame['node']} (#{frame['frame']}), line {frame['at_line']}: {here}")
        for name, value in frame.get("locals", {}).items():
            lines.append(f"      {name} = {IDENTITY.sub(IDENTITY_SHORT, value_text(value))}")
    handled = found["handled_in"]
    if handled:
        then = ""
        if outer and outer["frame"] == handled["frame"]:
            then = f", which raised !{outer['id']} {'from it' if outer['chain'] == 'cause' else 'while handling it'}"
        lines.append(f"  handled in {handled['node']} (#{handled['frame']}), line {handled['at_line']}{then}")
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
    calls = found["frames"]
    # A variable assigned once per call, on the same line and to the same value in every call
    # shown, is said once (one assigned in a loop is not: its writes are its story).
    same = set()
    if len(calls) > 1:
        each = []
        for call in calls:
            names: dict[str, list] = {}
            for w in call["writes"]:
                names.setdefault(w["name"], []).append((w["line"], w["name"], value_text(w["value"])))
            each.append({writes[0] for writes in names.values() if len(writes) == 1})
        same = set.intersection(*each)
    if same:
        listed = [f"line {line} {name} = {short_value({'repr': value}, RESULT_CHARS)}" for line, name, value in sorted(same)]
        lines.append(f"  same in all {len(calls)} calls shown: " + "; ".join(listed))
    shown, offset = 0, found.get("offset", 0)
    for index, call in enumerate(calls):
        args = ", ".join(f"{name}={short_value(value)}" for name, value in call.get("args", {}).items())
        name = call["node"] if len(found["nodes"]) > 1 else call["node"].rsplit(":", 1)[-1]
        lines.append(f"  #{call['frame']} {name}({args})" if "args" in call else f"  #{call['frame']} {name}")
        for write in call["writes"]:
            if (write["line"], write["name"], value_text(write["value"])) in same:
                continue
            after = f"+{duration(write['time_ns'] - call['start_ns'])}"
            lines.append(f"    line {write['line']:<4} {write['name']} = {short_value(write['value'], RESULT_CHARS)}   {after}")
        shown += len(call["writes"])
        if index < len(calls) - 1 or offset + shown >= found["total"]:  # its writes all shown: how it ended
            lines.append(f"    {end_text(call)}")
    if offset + shown < found["total"]:
        lines.append(f"  ... {plural(found['total'] - offset - shown, 'more write')} (--limit, --offset)")
    return "\n".join(lines)


def end_text(call: dict) -> str:
    """How a call ended, at the line it ended at when known: `line 12   return 'total 6'`."""
    line = call.get("end_line")
    if line is None and call["end"] == "raise" and call["exception"]:
        line = call["exception"].get("line")
    at = f"line {line:<4} " if line is not None else ""
    if call["end"] == "return":
        return f"{at}return {short_value(call['result'], RESULT_CHARS)}" if "result" in call else f"{at}returned"
    if call["exception"]:
        e = call["exception"]
        verb = "raised" if call["end"] == "raise" else f"passed on (raised in #{e.get('frame')})"
        return f"{at}{verb} {e.get('type', '?')}: {first_line(e.get('message', ''))}"
    return f"{at}[{call['end']}]"


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
