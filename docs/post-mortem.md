# Post-mortem: record test runs, debug failures afterwards

A test that fails in CI, or once in twenty runs on your machine, is the hardest to debug by
running it again. `pytest --luciole` records the whole test run (Python 3.12+), and keeps the
recording when a test fails: every call of project code with its arguments and return value, every
exception with the local variables of each frame it left, and each test's outcome. You, or an
agent, then debug from what actually happened, without running anything again.

## Locally

```sh
pip install luciole            # or: uv add --dev luciole
pytest --luciole               # any pytest arguments: -x, -k, -n 4 (pytest-xdist)...
```

```
luciole: recording kept in ~/.luciole/runs/shop-1a2b3c4d/20261103-101500: luciole trace ~/.luciole/runs/shop-1a2b3c4d/20261103-101500 tests --failed; for an agent: luciole mcp, tool `tests`
```

When every test passes, the recording is deleted (`--luciole-keep=always` keeps it). Then:

```sh
luciole trace RUN_FOLDER tests --failed       # the failures, each with its frame (#N) and exception (!N)
luciole trace RUN_FOLDER/trace-….sqlite traceback 3   # exception !3: each frame it left, with its locals
luciole trace RUN_FOLDER/trace-….sqlite calls total   # every call of a function, with its values
```

The plugin is installed with luciole and does nothing unless `--luciole` is given (or
`LUCIOLE_PYTEST=1` is set): installing luciole never changes a test run. Options:

| Option | |
|---|---|
| `--luciole` | record this run (also `LUCIOLE_PYTEST=1`) |
| `--luciole-level 0\|1\|2` | trace level ([trace-format.md](trace-format.md)); 1 by default: values and failure locals |
| `--luciole-scope F,G` | level 2: also every assignment in these functions (`cart:checkout,Cart.*`) |
| `--luciole-dir DIR` | keep the run folder in `DIR` (a CI artifact) rather than `~/.luciole/runs/<project>-<hash>/` |
| `--luciole-keep failed\|always` | `failed` (default): delete the recording when every test passed |

What is recorded:

- The run is a run folder ([mcp.md](mcp.md), "Where runs are kept") made with `by: pytest`, and
  `command.json`'s `exit_status` is pytest's (1: tests failed). The project root is pytest's
  rootdir. A run folder is kept when pytest's exit status is anything but 0 or 5 (no tests):
  failures, errors, an interrupted or broken run.
- Child processes are recorded too, each in its own trace in the folder, as auto-start records
  them: the plugin turns auto-start on in pytest's environment (`PYTHONPATH`, `LUCIOLE_TRACE`;
  restored at the end). With pytest-xdist, the workers record the tests, and the controller,
  which runs none, records nothing.
- Each test's outcome goes in the `tests` table of the trace of the process that ran it
  ([trace-format.md](trace-format.md)): `passed`, `failed`, `error` (a fixture), `skipped`,
  `xfail`, `xpass`, where it was decided (`setup`, `call`, `teardown`), the message, the test
  function's frame, and the attempt when a flaky test is run again (pytest-rerunfailures).
- With a recorder already running in pytest's process, the plugin adds the outcomes to its trace
  and changes nothing else: `luciole run --trace FILE -m pytest --luciole`, or auto-start with
  `LUCIOLE_TRACE` set (VS Code's Run Live of `pytest --luciole`). The MCP server's `run` sets
  `LUCIOLE_PYTEST=1`, so a pytest run started by an agent records its outcomes in the run folder
  the server made, without `--luciole`. A
  recorder that only lights up the view (VS Code's switch, without a trace) is replaced by one that
  also records, and still lights it up.
- On Python older than 3.12, the session header says so and nothing is recorded.

## In CI

Record the tests, and upload the recording when they fail:

```yaml
# .github/workflows/tests.yml
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -e . pytest luciole
      - run: pytest --luciole --luciole-dir luciole-recordings
      - uses: actions/upload-artifact@v4
        if: failure()
        with:
          name: luciole-recordings
          path: luciole-recordings/
          retention-days: 7
```

`luciole-recordings/` then holds one run folder (`<run id>/`, with `command.json` and a trace per
process). A trace holds argument and return values (capped, secrets redacted as
[trace-format.md](trace-format.md) says, "Redaction"): treat the artifact like logs.

## Debugging a downloaded recording

Download the artifact (`gh run download RUN_ID -n luciole-recordings -D luciole-recordings`) into
your checkout of the same commit, then:

```sh
luciole trace luciole-recordings/*/ tests --failed
luciole trace luciole-recordings/*/trace-….sqlite traceback 3
```

Or let Claude Code (any MCP client) ask, with the MCP server in post-mortem mode: `--runs` points
it at the downloaded folder, and `--no-run` leaves out `run`, so it answers from the recording
only:

```sh
claude mcp add luciole-ci -- luciole mcp --runs luciole-recordings --no-run
```

Then ask: "the tests failed in CI, the recording is in luciole-recordings: why?". The agent
starts with `tests` (the failures, their frames, exceptions and processes), then `traceback`,
`calls`, `frame` and `origin` on them. `find`, `neighbors` and `origin` read the static graph of
the checkout (`--root`, default the current folder), which should be the commit that ran.
`luciole trace ... origin` reads the source at the root recorded in the trace, the CI machine's
path: use the MCP server's `origin` for a downloaded recording.
