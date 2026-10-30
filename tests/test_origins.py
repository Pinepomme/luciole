"""Origin queries (src/luciole/origins.py): where a value came from, from a trace and the static
graph walked backwards."""

import json
import re
import subprocess
import sys
import textwrap

import pytest

from luciole.analyzer import build_graph
from luciole.origins import MAX_STEPS, OriginError, origin, render_origin
from luciole.trace import Trace
from test_mcp import PYTHON, Client
from test_trace import clean_env, traced

pytestmark = pytest.mark.skipif(sys.version_info < (3, 12), reason="sys.monitoring needs Python 3.12")

# A value made through a helper's return, passed as an argument (also straight from another
# call), stored on self and read back, a global changed by a function, two objects of one class
# (the last deposit before the report is on the other one), a local assigned on two lines, and
# recursion.
BANK = textwrap.dedent("""\
    RATE = 0.25


    def parse(text):
        value = int(text)
        if value < 0:
            value = 0
        return value


    def fee_for(amount):
        return amount * RATE


    class Account:
        def __init__(self, owner):
            self.owner = owner
            self.balance = 0
            self.history = []

        def deposit(self, amount):
            fee = fee_for(amount)
            self.balance += amount - fee
            self.history.append(amount)

        def report(self):
            shown = self.balance
            return shown


    def setup():
        global RATE
        RATE = 0.5


    def fact(n):
        if n <= 1:
            return 1
        return n * fact(n - 1)


    def main():
        account = Account("ann")
        amount = parse("40")
        account.deposit(amount)
        setup()
        account.deposit(parse("-3"))
        other = Account("bob")
        other.deposit(parse("8"))
        final = account.report()
        big = fact(5)
        label = "total: " + str(final)
        log = []
        log.append(final)
        print(label, big)


    main()
""")


def line_of(code):
    """The line of BANK that holds `code`."""
    return next(number for number, text in enumerate(BANK.splitlines(), 1) if code in text)


def steps(step):
    yield step
    for child in step["children"]:
        yield from steps(child)


def ask(path, root, node, variable, **options):
    with Trace(path) as trace:
        return origin(trace, build_graph(root), node, variable, **options)


@pytest.fixture(scope="module")
def recorded(tmp_path_factory):
    """The program recorded at level 1, and at level 2 with a scope: {level: (trace, root)}."""
    found = {}
    for level, args in ((1, ["prog.py"]), (2, ["--scope", "main,parse,Account.*", "prog.py"])):
        root = tmp_path_factory.mktemp(f"level{level}")
        (root / "prog.py").write_text(BANK)
        home = tmp_path_factory.mktemp("luciole-home")  # never an engine running on this machine
        result, path = traced(root, None, *args, level=level, env={"LUCIOLE_HOME": str(home)})
        assert result.returncode == 0, result.stderr
        assert result.stdout == "total: 30.0 120\n"
        found[level] = (path, root)
    return found


def deposits(path):
    with Trace(path) as trace:
        return [call["frame"] for call in trace.calls("Account.deposit")["calls"]]


def test_a_value_through_returns_arguments_attributes_and_globals(recorded):
    path, root = recorded[2]
    found = ask(path, root, "main", "final")
    top = found["steps"]
    assert (found["node"], found["level"], found["scope"]) == ("prog:main", 2, ["main", "parse", "Account.*"])
    assert (top["kind"], top["value"], top["line"], top["code"]) == ("write", "30.0", line_of("final = "), "final = account.report()")
    report = top["children"][0]
    assert (report["kind"], report["node"], report["value"], report["code"]) == ("returned_by", "prog:Account.report", "30.0", "return shown")
    shown = report["children"][0]
    assert (shown["kind"], shown["variable"], shown["value"]) == ("write", "shown", "30.0")

    # The balance: set by the last deposit on the same account, not by the later one on `other`.
    balance = shown["children"][0]
    first, second, third = deposits(path)
    assert (balance["kind"], balance["variable"], balance["frame"], balance["line"]) == ("attribute_written_by", "Account.balance", second, line_of("self.balance +="))
    assert "on the same object" in balance["note"]
    by_kind = {(child["kind"], child["variable"]): child for child in balance["children"]}
    amount = by_kind["argument", "amount"]
    assert amount["value"] == "0" and amount["caller"]["code"] == 'account.deposit(parse("-3"))'
    assert amount["children"][0]["kind"] == "returned_by" and amount["children"][0]["node"] == "prog:parse"
    assert by_kind["write", "fee"]["value"] == "0.0"
    before = next(child for child in balance["children"] if child.get("previous"))  # self.balance += ...
    assert (before["kind"], before["frame"]) == ("attribute_written_by", first)
    assert any(step["kind"] == "attribute_written_by" and step["node"] == "prog:Account.__init__" for step in steps(before))

    # The global, changed by setup() before the second deposit's fee.
    rates = [step for step in steps(top) if step["variable"] == "RATE"]
    assert rates and {(step["kind"], step["node"]) for step in rates} <= {("global_written_by", "prog:setup"), ("global_written_by", "prog")}
    assert ("global_written_by", "prog:setup") in {(step["kind"], step["node"]) for step in rates}

    text = render_origin(found)
    assert text.startswith("where final in prog:main #")
    assert "final = 30.0   main #" in text and "<- returned by Account.report #" in text
    assert "Account.balance set by Account.deposit #" in text and "before that: Account.balance set by" in text


def test_an_attribute_changed_in_place(recorded):
    path, root = recorded[1]
    found = ask(path, root, "Account.report", "prog:Account#history")
    top = found["steps"]
    assert (top["kind"], top["node"], top["code"]) == ("attribute_written_by", "prog:Account.__init__", "self.history = []")
    changed = top["changed_in_place"]
    assert (changed["frame"], changed["code"], changed["calls"]) == (deposits(path)[1], "self.history.append(amount)", 2)  # not other's
    assert "then changed in place by Account.deposit #" in render_origin(found)


def test_level_2_gives_the_line_that_ran(recorded):
    path, root = recorded[2]
    with Trace(path) as trace:
        negative = trace.calls("parse")["calls"][1]["frame"]  # parse("-3")
    found = ask(path, root, None, "value", frame=negative)
    top = found["steps"]
    assert (top["kind"], top["line"], top["value"], top["code"]) == ("write", line_of("value = 0"), "0", "value = 0")
    assert top["children"][0]["kind"] == "literal"
    # The same variable earlier in that call: its first assignment.
    found = ask(path, root, None, "value", frame=negative, time_ns=top["time_ns"] - 1)
    assert (found["steps"]["line"], found["steps"]["value"]) == (line_of("value = int"), "-3")
    assert found["steps"]["children"][0]["kind"] == "argument" and found["steps"]["children"][0]["value"] == "'-3'"


def test_a_local_list_changed_in_place(recorded):
    path, root = recorded[2]
    top = ask(path, root, "main", "log")["steps"]
    assert (top["kind"], top["value"], top["code"]) == ("write", "[30.0]", "log.append(final)")
    final = top["children"][0]
    assert (final["variable"], final["value"], final["note"]) == ("final", "30.0", "read by the line that changed it in place")


def test_without_level_2_it_lists_the_candidates_and_how_to_know(recorded):
    path, root = recorded[1]
    with Trace(path) as trace:
        negative = trace.calls("parse")["calls"][1]["frame"]
    found = ask(path, root, "parse", "value", frame=negative)
    top = found["steps"]
    assert top["kind"] == "needs_level_2" and top["rerun"] == "prog:parse"
    assert [candidate["line"] for candidate in top["candidates"]] == [line_of("value = int"), line_of("value = 0")]
    assert "run again with --scope prog:parse" in render_origin(found)
    assert 'scope: ["prog:parse"]' in render_origin(found, rerun="mcp")

    # What crossed calls is still known, and a local assigned once is too.
    found = ask(path, root, "main", "final")
    top = found["steps"]
    assert (top["kind"], top["value"], top["note"]) == ("write", "30.0", "its only assignment")  # what report() returned
    balance = next(step for step in steps(top) if step["kind"] == "attribute_written_by")
    assert balance["frame"] == deposits(path)[1] and "on the same object" in balance["note"]
    fee = next(step for step in steps(balance) if step["variable"] == "fee")
    assert fee["value"] == "0.0"  # fee = fee_for(amount): the call's result
    shown = next(step for step in steps(top) if step["variable"] == "shown")
    assert (shown["value"], shown["note"]) == ("30.0", "its only assignment")  # return shown: what report() returned
    label = ask(path, root, "main", "label")["steps"]
    assert (label["kind"], label["value"], label["note"]) == ("write", None, "its only assignment; its value is not recorded, as level 1 records locals only as an exception left them, or not at all")
    assert label["rerun_scope"] == ["prog:main"] and "to record its value: run again with --scope prog:main, then ask again" in render_origin(ask(path, root, "main", "label"))
    assert label["children"][0]["variable"] == "final" and label["children"][0]["value"] == "30.0"


def test_an_argument_made_by_another_call_on_the_same_line(recorded):
    path, root = recorded[1]
    found = ask(path, root, None, "amount", frame=deposits(path)[2])  # other.deposit(parse("8"))
    top = found["steps"]
    assert (top["kind"], top["value"], top["caller"]["code"]) == ("argument", "8", 'other.deposit(parse("8"))')
    assert (top["children"][0]["kind"], top["children"][0]["node"], top["children"][0]["value"]) == ("returned_by", "prog:parse", "8")


def test_recursion_is_walked_without_looping(recorded):
    path, root = recorded[1]
    found = ask(path, root, "main", "big", depth=100, branches=10)
    returned = [step for step in steps(found["steps"]) if step["kind"] == "returned_by" and step["node"] == "prog:fact"]
    assert [step["value"] for step in returned] == ["120", "24", "6", "2", "1"]
    assert returned[-1]["code"] == "return 1"  # the other return statement made a call, and none ran
    assert all(step["code"] == "return n * fact(n - 1)" for step in returned[:-1])
    arguments = [step["value"] for step in steps(found["steps"]) if step["kind"] == "argument" and step["variable"] == "n"]
    assert set(arguments) == {"2", "3", "4", "5"}
    assert sum(1 for _ in steps(found["steps"])) <= MAX_STEPS
    assert any(step.get("seen") for step in steps(found["steps"]))  # shared sources are shown once


def test_depth_and_branch_limits_say_what_was_cut(recorded):
    path, root = recorded[2]
    found = ask(path, root, "main", "final", depth=2)
    cut = [step for step in steps(found["steps"]) if step.get("cut")]
    assert cut and all(step["cut"] == "depth" for step in cut)
    text = render_origin(found)
    assert "not followed further (depth limit); to go on: origin - " in text
    assert '`origin` with variable: "' in render_origin(found, rerun="mcp")

    found = ask(path, root, "Account.report", "self.balance", branches=1)
    assert found["steps"]["kind"] == "attribute_written_by" and found["steps"].get("more") == 2
    assert "2 more sources not followed (branch limit)" in render_origin(found)


def test_questions_that_cannot_be_asked(recorded):
    path, root = recorded[1]
    with pytest.raises(OriginError, match="no calls of nope"):
        ask(path, root, "nope", "x")
    with pytest.raises(OriginError, match="no frame #9999"):
        ask(path, root, None, "x", frame=9999)
    with pytest.raises(OriginError, match="is a call of prog:Account.deposit, not main"):
        ask(path, root, "main", "x", frame=deposits(path)[0])
    found = ask(path, root, "main", "nope")
    assert found["steps"]["kind"] == "unknown"


def test_the_command_line(recorded):
    path, root = recorded[2]
    command = [sys.executable, "-m", "luciole", "trace", str(path), "origin", "main", "final"]
    result = subprocess.run(command, env=clean_env(), capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("where final in prog:main #") and "<- returned by Account.report #" in result.stdout
    result = subprocess.run([*command[:-2], "-", "amount", "--frame", str(deposits(path)[2]), "--json"], env=clean_env(), capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["steps"]["value"] == "8"
    result = subprocess.run([*command[:-2], "nope", "x"], env=clean_env(), capture_output=True, text=True, timeout=60)
    assert result.returncode == 1 and "no calls of nope" in result.stderr


def test_the_mcp_tool(tmp_path):
    (tmp_path / "prog.py").write_text(BANK)
    client = Client(tmp_path)
    client.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}})
    try:
        assert "`origin`" in client.request("initialize", {"protocolVersion": "2025-06-18"})["result"]["instructions"]
        client.ok("run", command=f"{PYTHON} prog.py")
        text = client.ok("origin", function="main", variable="final")
        assert text.startswith("where final in prog:main #") and "Account.balance set by Account.deposit #" in text
        negative = re.search(r"#(\d+) parse\(text='-3'\)", client.ok("calls", function="parse")).group(1)
        text = client.ok("origin", variable="value", frame=int(negative))
        assert 'to know which: `run` the same command again with scope: ["prog:parse"]' in text
        client.ok("run", command=f"{PYTHON} prog.py", scope="parse")
        text = client.ok("origin", function="parse", variable="value")
        assert text.split("\n\n")[2].startswith("value = 8   parse #")  # after the values on the way
        text, error = client.tool("origin", function="nope", variable="x")
        assert error and "no calls of nope" in text and "`find`" in text
        text, error = client.tool("origin", variable="x")
        assert error and "missing argument: function (or frame)" in text
    finally:
        client.close()


# The user's case: a negative amount slips through parse, and the balance ends wrong.
DEPOSITS = textwrap.dedent("""\
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

    main()
""")


@pytest.fixture(scope="module")
def deposits_recorded(tmp_path_factory):
    """DEPOSITS recorded at level 1, and at level 2 with parse and main in the scope."""
    found = {}
    for level, args in ((1, ["prog.py"]), (2, ["--scope", "parse,main", "prog.py"])):
        root = tmp_path_factory.mktemp(f"deposits{level}")
        (root / "prog.py").write_text(DEPOSITS)
        result, path = traced(root, None, *args, level=level, env={"LUCIOLE_HOME": str(tmp_path_factory.mktemp("luciole-home"))})
        assert "ValueError: balance is 20" in result.stderr
        found[level] = (path, root)
    return found


def test_the_line_is_inferred_from_known_values_at_level_1(deposits_recorded):
    path, root = deposits_recorded[1]
    # main's failure locals have balance = 20; line 12 assigns 0, and deposit's last call on
    # line 14 returned exactly 20.
    found = ask(path, root, "main", "balance")
    top = found["steps"]
    assert (top["kind"], top["value"], top["line"]) == ("write", "20", 14)
    assert top["note"].startswith("assigned on lines 12, 14; inferred: balance was 20 as the exception left #")
    assert "line 12 assigns 0" in top["note"] and ", the last call on line 14, returned 20" in top["note"]
    assert (top["children"][0]["kind"], top["children"][0]["value"]) == ("returned_by", "20")
    # And on back through the loop, from the values passed to deposit: the -3 shows up.
    assert any(step["kind"] == "argument" and step["variable"] == "amount" and step["value"] == "-3" for step in steps(top))
    assert {"name": "deposit.amount", "node": "prog:deposit", "variable": "amount", "values": ["5", "-3", "20"]} in found["values"][:4]

    # parse returned 20 with `return value`, its only return: line 2 assigns 0, so line 4 made it.
    with Trace(path) as trace:
        last = trace.calls("parse")["calls"][-1]["frame"]
    top = ask(path, root, None, "value", frame=last)["steps"]
    assert (top["kind"], top["value"], top["line"], top["code"]) == ("write", "20", 4, "value = int(text)")
    assert "inferred: #" in top["note"] and "returned it: 20; line 2 assigns 0" in top["note"]


def test_level_2_answers_say_what_is_out_of_the_scope(deposits_recorded):
    path, root = deposits_recorded[2]
    found = ask(path, root, "main", "balance")
    top = found["steps"]
    assert (top["kind"], top["value"], top["line"], top.get("note")) == ("write", "20", 14, None)  # recorded, not inferred
    assert any(entry["values"] == ["5", "-3", "20"] for entry in found["values"][:4])
    fee = next(step for step in steps(top) if step["variable"] == "fee")
    assert "deposit is not in this run's scope: its locals are recorded only as an exception left them, or not at all" in fee["note"]
    assert fee["rerun_scope"] == ["parse", "main", "prog:deposit"]
    text = render_origin(found)
    assert text.count("to record its value: run again with --scope parse,main,prog:deposit, then ask again") == 1
    assert text.splitlines()[2] == "values on the way:" and "-3" in "\n".join(text.splitlines()[3:7])
    # The same variable with the same value is not followed twice: the branches go to new values.
    again = [step for step in steps(top) if step.get("seen_value")]
    assert again and all(step["value"] == "0" and step["variable"] == "balance" for step in again)
    assert "(followed above, with the same value, in #" in text
