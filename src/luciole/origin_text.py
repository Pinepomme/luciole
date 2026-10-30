"""Origin answers as text (origins.py makes them): the values met on the way, then one line per
step, its sources indented below it with `<-`."""

from __future__ import annotations

import json

VALUE_WIDTH = 80  # characters of a value shown in text
VALUES = 12  # variables in the summary of the values met on the way, and values shown for each


def render_origin(found: dict, rerun: str = "cli") -> str:
    """The answer as a chain to follow: one line per step, its sources indented below it with
    `<-`. `rerun` says how to ask for level 2 in hints: "cli" (`luciole run --scope`) or "mcp"
    (the `run` tool's `scope`). Before the tree, the values each variable took on the way."""
    head = f"where {found['variable']} in {found['node']} #{found['frame']} came from"
    lines = [head + f" (level {found['level']} trace{'' if found['level'] < 2 else ', scope ' + ', '.join(found['scope'] or [])}):", ""]
    if found.get("values"):
        lines.append("values on the way:")
        for entry in found["values"]:
            shown = [clip(value, 24) for value in entry["values"][:VALUES]]
            lines.append(f"  {entry['name']}: {', '.join(shown)}{', …' if len(entry['values']) > VALUES else ''}")
        lines.append("")
    _render(found["steps"], 0, lines, rerun, set())
    return "\n".join(lines)


def rerun_hint(scope: list[str], rerun: str) -> str:
    return f"`run` the same command again with scope: {json.dumps(scope)}" if rerun == "mcp" else f"run again with --scope {','.join(scope)}"


def short(node: str | None) -> str:
    return node.rsplit(":", 1)[-1] if node else "?"


def clip(text: str | None, width: int = VALUE_WIDTH) -> str:
    if text is None:
        return "?"
    return text if len(text) <= width else text[: width - 1] + "…"


def _render(step: dict, level: int, lines: list[str], rerun: str, hinted: set) -> None:
    pad = "  " * level + ("<- " if level else "")
    kind, var = step["kind"], step["variable"]
    where = f"{short(step['node'])} #{step['frame']}"
    at = f", line {step['line']}: {step['code']}" if step["line"] and step["code"] else f", line {step['line']}" if step["line"] else ""
    value = f" = {clip(step['value'])}" if step["value"] is not None else ""
    if kind == "write":
        text = f"{var}{value}   {where}{at}"
    elif kind == "argument":
        caller = step.get("caller")
        text = f"{var}{value}   argument of {where}"
        if caller:
            text += f", passed by {short(caller['node'])} #{caller['frame']}, line {caller['line']}" + (f": {caller['code']}" if caller["code"] else "")
    elif kind == "returned_by":
        text = f"returned by {where}{': ' + clip(step['value']) if step['value'] is not None else ''}   {at[2:] if at else ''}".rstrip()
    elif kind in ("attribute_written_by", "global_written_by"):
        text = f"{var}{value} set by {where}{at}"
    elif kind == "needs_level_2":
        candidates = [str(candidate["line"]) for candidate in step["candidates"]]
        text = f"{var}{value}   {where}: assigned on line{'s' if len(candidates) > 1 else ''} {', '.join(candidates)}"
    elif kind == "literal":
        text = "no variable"
    else:
        text = f"?{' ' + var if var else ''}   {where}{at}"
    if step.get("previous"):
        text = "before that: " + text
    if step.get("note"):
        text += f"   ({step['note']})"
    lines.append(pad + text)
    inner = "  " * (level + 1) + "   "
    if step.get("candidates"):
        for candidate in step["candidates"]:
            ran = "  (a call on it ran)" if candidate.get("ran") else "  (returns exactly this value)" if candidate.get("same_value") else ""
            lines.append(f"{inner}line {candidate['line']}: {candidate['code'] or ''}{ran}")
    changed = step.get("changed_in_place")
    if changed:
        where = f", line {changed['line']}: {changed['code']}" if changed["line"] else f", lines {', '.join(map(str, changed['lines']))}"
        more = f"; {changed['calls']} such calls since it was set" if changed["calls"] > 1 else ""
        lines.append(f"{inner}then changed in place by {short(changed['node'])} #{changed['frame']}{where}   (its contents are not recorded{more})")
    if step.get("locals"):
        lines.append(f"{inner}its last writes: " + ", ".join(f"{found['name']} = {clip(found['value'], 60)} (line {found['line']})" for found in step["locals"]))
    scope = step.get("rerun_scope") or ([step["rerun"]] if step.get("rerun") else None)
    if kind == "needs_level_2" and scope:
        lines.append(f"{inner}to know which: {rerun_hint(scope, rerun)}, then ask again")
    elif scope and tuple(scope) not in hinted:  # once per answer: unknown values are often many
        hinted.add(tuple(scope))
        lines.append(f"{inner}to record its value: {rerun_hint(scope, rerun)}, then ask again")
    if step.get("seen_value"):
        lines.append(f"{inner}(followed above, with the same value, in #{step['seen_value']})")
    elif step.get("seen"):
        lines.append(f"{inner}(followed above)")
    if step.get("cut"):
        why = "depth limit" if step["cut"] == "depth" else "step limit"
        ask = "return" if kind == "returned_by" else step["var"] if kind.endswith("_written_by") else var
        at = step.get("time_ns") if kind == "write" else None
        if rerun == "mcp":
            again = f'`origin` with variable: "{ask}", frame: {step["frame"]}' + (f", at: {at}" if at is not None else "")
        else:
            again = f"origin - {ask} --frame {step['frame']}" + (f" --at {at}" if at is not None else "")
        lines.append(f"{inner}... not followed further ({why}); to go on: {again}")
    for child in step["children"]:
        _render(child, level + 1, lines, rerun, hinted)
    if step.get("more"):
        names = ", ".join(step.get("not_followed", [])[:12])
        lines.append(f"{inner}... {step['more']} more sources not followed (branch limit){': ' + names if names else ''}")
