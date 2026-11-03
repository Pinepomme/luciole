# Fault localization: `suspects` and `compare`

Two queries over a recorded test run that point at where a bug is likely to be, without running
anything again. Both read the traces of one run folder (one per process, pytest-xdist workers
included) and work the same from the command line and the MCP server.

```sh
luciole trace RUN_FOLDER suspects [--top N] [--json]
luciole trace RUN_FOLDER compare [FRAME_A FRAME_B] [--run-b OTHER] [--process PID] [--process-b PID] [--json]
```

MCP tools: `suspects` (`run`, `top`) and `compare` (`run`, `process`, `frame_a`, `frame_b`,
`run_b`, `process_b`). The code is in `src/luciole/localize.py` (JSON answers) and
`src/luciole/localize_text.py` (text).

## Which tests passed and failed

From the `tests` table the pytest plugin fills (`pytest --luciole`, or any pytest run under the
MCP server's `run`, see [post-mortem.md](post-mortem.md)): `failed` and `error` count as failing,
`passed` and `xpass` as passing; `skipped` and `xfail` are left out. Each attempt of a test run
again (pytest-rerunfailures) counts on its own.

Without that table (pytest run without the plugin, under `luciole run --trace`), outcomes are
inferred from the test functions' calls: a function named `test*` in a test module
(`test_*.py`, `*_test.py`, a `test`/`tests`/`testing` package), not called by another test
function, failed when its call ended by an exception (other than a skip), passed when it returned.
A failure in a fixture is then not seen.

A test ran the functions that started while it ran: between its start and end in the `tests`
table (fixtures included), or within its function's call when inferred. Test code (the modules
above, and `conftest.py`) is never a suspect.

## `suspects`: spectrum-based ranking

For each function f that a failing test ran, `failed(f)` and `passed(f)` count the failing and
passing tests that ran it, and

    Ochiai(f) = failed(f) / sqrt(failed × (failed(f) + passed(f)))

where `failed` is the number of failing tests. 1 means every failing test ran it and no passing
test did. Functions are ranked by score, then by more failing tests, fewer passing tests, the
value hint below, and fewer distinct calling functions (the more specific first); functions with
the same score share a rank, and the answer says how many share the top one.

For the first functions (3), a value hint: the calls of f made in failing tests are compared with
those made in passing tests (arguments and result, memory addresses left out, at most 100 calls a
side):

- `same arguments, another result`: a passing test called it with the same arguments and got
  another result. Within a tie, this comes first.
- `a call no passing test made`: the first such call, beside the passing call with the most
  arguments equal.
- `its calls in failing tests were all made in passing tests too`: f behaved in failing tests as
  in passing ones. Within a tie, this comes last.

At level 2 ([trace-format.md](trace-format.md)), lines of the ranked functions that assigned a
variable are ranked by the same formula, from the `writes` table (`lines assigning a variable`).

When no test passed, Ochiai degenerates (every function the failing tests ran scores
sqrt(failed(f) / failed)): the answer says so and ranks by failing tests that ran the function,
then by fewest calling functions. When no test failed, there is nothing to rank.

## `compare`: the first divergence

Two calls, A and B, usually a failing test and a passing one, are walked side by side. Below two
matched calls, their children are aligned by function: difflib's longest matching blocks over the
two sequences of function names (a near-LCS; with more than a million pairs to compare, by
position). Then, in execution order, the first of:

- `calls differ`: a call made on one side with no counterpart on the other. An extra call of the
  function of the nearest matched call (one more turn of a loop over a longer input) is not a
  divergence: the path notes it (`A made 2 more calls of price_of`), and what it changed shows in
  the results that follow.
- `raised on one side only`, or `ended differently`.
- `returned something else`: values compared as recorded (reprs, addresses left out).

A call's children are compared before its own result, so the divergence is the innermost first
difference. Arguments that differ are not a divergence on their own (a parametrized test's inputs
differ from the top), but the path from the roots lists which arguments differ at each call, the
divergence says whether its arguments were the same (`with the same arguments` is the strongest
lead: same input, different behaviour), and `arguments first differ at` names the first call
below the roots whose arguments differ. Snapshots are cut at their recorded size, so two long
values that differ beyond it compare equal.

Without frames, `compare` takes the first failing test (with a recorded frame) and, as B:

1. with `--run-b`/`run_b`, the same test in the other run (a run before a change, the last green
   CI run); if only the other run has failing tests, A and B are swapped;
2. else the same test function with other parameters (`test_x[1]` for `test_x[2]`);
3. else the passing test whose set of functions run is closest (Jaccard).

Test functions' frames are compared (the call phase), not fixtures.

## Limits

- Spectrum ranking needs tests that tell functions apart: a function every test runs scores the
  same as the bug when only some inputs trigger it. The value hints and `compare` are there for
  that case.
- Coverage is by function, not line (lines only for level 2 scopes), and only for calls recorded:
  a run truncated at its frame limit, C code and code outside the project root are not seen.
- Calls in threads started by a test count for it while it runs; a background thread that
  outlives tests is counted for whichever test is running.
- `compare` stops at 20,000 matched calls (and 300 levels deep); `cut` is then true, and an
  answer with no divergence says it was cut.
