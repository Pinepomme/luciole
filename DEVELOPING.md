# Developing Luciole

How to set up the repo, change the tool, try your change, and ship it.
User-facing docs are in [README.md](README.md); the plan is in [docs/roadmap.md](docs/roadmap.md).

## 1. What you are working on

Luciole has two halves, released together under one version number:

| Half | Folder | Language | Published as |
|---|---|---|---|
| Analyzer, recorder, engine (`luciole` CLI) | `src/luciole/` | Python, standard library only | PyPI package `luciole` |
| VS Code extension (the 3D view) | `extension/` | TypeScript | `pinepomme.luciole` on the VS Code Marketplace and Open VSX |

The extension does **not** use a pip-installed `luciole`. It carries its own copy of `src/luciole/`
and puts it on `PYTHONPATH` when it starts Python. So "rebuilding Luciole" means rebuilding the
extension: it picks up your Python changes too.

## 2. One-time setup

You need:

- **git**
- **uv** (Python): `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **Python 3.12 or newer** for live mode (3.10 is enough for the graph alone). uv can install it:
  `uv python install 3.12`
- **Node.js 20** and npm (for the extension)
- **VS Code**, with the `code` command on your `PATH`. On WSL, open the repo from WSL
  (`code .` in the WSL terminal) so the extension runs on the Linux side.

Then:

```sh
git clone git@github.com:Pinepomme/luciole.git
cd luciole
git switch dev                 # work happens on dev, not main (see section 6)

uv sync                        # Python venv in .venv, with the dev tools (pytest, jsonschema, debugpy)
cd extension && npm install && cd ..
```

Check that everything works:

```sh
uv run pytest -q               # Python tests
cd extension
npm run typecheck              # TypeScript types
npm test                       # extension unit tests
cd ..
```

All three should pass on a fresh `dev`. If they don't, fix that first: everything below assumes
they do.

## 3. Try your change in VS Code (the usual loop)

1. Open the repo folder in VS Code.
2. Press **F5** (launch configuration "Run Luciole extension").
   This builds the extension and opens a second VS Code window, titled
   **[Extension Development Host]**, with the extension loaded from `extension/` and the Python
   code from `src/` as it is on disk. Nothing gets installed.
3. In that second window, use Luciole: the graph button in a Python file's editor title bar,
   ⚡ Run Live, and so on. `examples/` has small programs to try (`examples/workers.py` exercises
   threads and asyncio).
4. After a change:
   - **Python** (`src/luciole/`): no rebuild needed. The next graph or run uses the new code.
     If you changed the engine (`engine.py`, `protocol.py`), stop the running engine first
     (see [Troubleshooting](#8-troubleshooting)).
   - **TypeScript** (`extension/src/`): stop the debug session and press F5 again (F5 rebuilds
     first). Or keep `npm run watch` running in `extension/`, which rebuilds on every save, and
     run **Developer: Reload Window** (Ctrl+R) in the second window.

Logs: in the second window, open the **Output** panel and choose **Luciole**. The engine writes
to `~/.luciole/engine.log`. Errors in the extension itself appear in the Debug Console of the
**first** window.

## 4. Try your change from the terminal

You can work on the Python half without VS Code:

```sh
uv run luciole graph examples/                     # summary of a graph
uv run luciole graph examples/shop.py --json       # the graph as JSON
uv run luciole run examples/workers.py             # run under the recorder
uv run luciole run --events run.jsonl examples/workers.py   # record the events to a file
uv run luciole tests src/luciole/analyzer.py       # test files that import a module
```

Always use `uv run luciole`, not `luciole`. A bare `luciole` may be an old copy installed from
PyPI somewhere on your machine.

To see the webview without VS Code (renders it in headless Chromium and saves PNGs to
`extension/out/dev/shots/`):

```sh
cd extension
npm run screenshot -- ../examples                         # a static graph
npm run screenshot -- ../examples --events ../run.jsonl   # replay a recording
npm run screenshot -- ../examples --fps                   # also print frames per second
node out/dev/layout-stats.js graph.json                   # how the region layout turns out
```

On WSL, Chromium needs `libasound.so.2`: `sudo apt install libasound2t64`.

## 5. Use your build every day (optional)

F5 is for testing. To use your current build in your **normal** VS Code windows:

```sh
cd extension
npm run install-local          # builds luciole.vsix and installs it
```

Then reload any open VS Code window (**Developer: Reload Window**). Run it again after every
change you want to keep.

What to know:

- This **replaces** the Luciole extension installed from the Marketplace, if any. They have the
  same ID (`pinepomme.luciole`), so only one can be installed.
- Both may show the same version number, so the Extensions panel won't tell you which one you
  have.
- VS Code auto-updates extensions. When a newer version is published, it replaces your build. To
  stop that, right-click Luciole in the Extensions panel and untick **Auto Update**.
- To go back to the published version: uninstall Luciole in the Extensions panel, then install
  it again from the Marketplace.

## 6. Branches, commits, pull requests

- **`main`** holds released code only. Each release is a tag `vX.Y.Z` on `main`.
- **`dev`** is where work lands.
- To change something:

  ```sh
  git switch dev && git pull
  git switch -c feature/<short-name>
  # ... edit, test (sections 3-4) ...
  uv run pytest -q && (cd extension && npm run typecheck && npm test)
  git push -u origin feature/<short-name>
  ```

  Then open a pull request **into `dev`** (not `main`). CI runs the Python tests on Linux
  (3.10 to 3.14), macOS and Windows, and the extension's typecheck, tests and packaging. Merge
  when it's green.
- **One feature, one branch, one pull request.** Name branches `feature/…`, `fix/…` or `docs/…`.
  Fixes found while reviewing a feature go on that feature's branch, before it merges.
- **Merge with a merge commit** (GitHub's "Create a merge commit"), never squash or rebase: `dev`
  then keeps one merge per feature, with the feature's commits under it. `git log --first-parent
  dev` lists the features in order; `git log --graph` shows each one's own commits. Deleting the
  branch after merging loses nothing.
- **A feature that needs another one not merged yet:** branch from it, and open the pull request
  against that branch. When the first one merges, GitHub retargets the second to `dev`.
- Commits are authored as `Pinepomme <45495853+Pinepomme@users.noreply.github.com>` (set in this
  repo's git config; check with `git config user.email`). Pull requests are opened from the
  Pinepomme GitHub account.

Rules that hold across the codebase:

- The Python package stays **standard library only**. `debugpy` is the one allowed exception.
- No telemetry. Nothing leaves the user's machine.
- Test and measure on **public code only**: copies of the standard library, or open-source
  projects cloned into a scratch folder. Never private or company code, and never name private
  projects in commits, pull requests or comments.
- The graph JSON is the contract between the two halves. If you change it, update
  `docs/graph-format.md` and `schema/graph.schema.json` too; `tests/test_graph_format.py` checks
  `examples/shop.graph.json` against the schema.

## 7. Release a new version

Releases are published by GitHub Actions when you push a tag. Don't publish by hand.

1. On `dev` (through a pull request like any change), set the new version in **both** places:
   - `src/luciole/__init__.py`: `__version__ = "X.Y.Z"`
   - `extension/package.json`: `"version": "X.Y.Z"`

   and add a `## X.Y.Z` section at the top of `CHANGELOG.md` describing the changes.
2. Open a pull request from **`dev` into `main`** and merge it.
3. Tag `main` and push the tag:

   ```sh
   git switch main && git pull
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

4. Watch the **Release** workflow in the repo's Actions tab. It:
   - refuses the tag if it isn't on `main`, if the two versions don't match the tag, or if
     `CHANGELOG.md` has no `## X.Y.Z` section;
   - runs all the tests and builds the Python package and `luciole.vsix`;
   - publishes to **PyPI** (trusted publishing, no token), the **VS Code Marketplace** (secret
     `VSCE_PAT`) and **Open VSX** (secret `OVSX_PAT`). A registry whose secret isn't set is
     skipped, with a notice;
   - creates the GitHub release, with the CHANGELOG section as notes and the built files attached.

   If one publish job fails, re-run just that job from the Actions page.

The `VSCE_PAT` token (Azure DevOps) expires within a year. When the Marketplace job fails with an
authentication error, create a new token and update the secret under the repo's
Settings → Environments → `release`.

## 8. Troubleshooting

**"The engine did not start" / live mode shows nothing.**
Read `~/.luciole/engine.log`. An engine left over from another build may still be running and
speaking an older protocol: list it with `cat ~/.luciole/engine-v*.json` (it holds the port and
pid), stop that process, and try again. The extension starts a new one when needed.

**My Python change has no effect in VS Code.**
In the F5 window, it always uses `src/`. In a normal window, it uses the copy bundled at
`npm run install-local` time: run it again and reload the window.

**My TypeScript change has no effect.**
Reloading the window doesn't rebuild. Stop and press F5 again, or run `npm run watch` in
`extension/` before reloading. Check for build errors in the first window's terminal.

**Live mode does nothing at all.**
It needs Python 3.12 or newer. The extension uses the `luciole.pythonPath` setting, otherwise the
workspace's `.venv`, otherwise `python3`. Check which one with `python3 --version`, or set
`luciole.pythonPath`.

**`code: command not found` during `npm run install-local`.**
In VS Code, run **Shell Command: Install 'code' command in PATH**. On WSL, open a terminal
inside a VS Code window connected to WSL.

## 9. Where things are

```
src/luciole/
  analyzer.py      static graph: functions, classes, variables, calls (ast)
  recorder.py      live recording with sys.monitoring (3.12+); luciole.start()
  engine.py        the local engine that recorders connect to and viewers subscribe to
  protocol.py      protocol version and engine discovery (~/.luciole/engine-v1.json)
  _autostart/      sitecustomize.py: starts the recorder in any Python process when enabled
  values.py        variable snapshots for the Variables view
  testfinder.py    finds the test files that import a module
  cli.py           the `luciole` command
extension/
  src/extension.ts   entry point, commands
  src/live.ts        live runs (terminal + socket); src/engine.ts finds or starts the engine
  src/debugging.ts   following the VS Code debugger
  src/webview/       the 3D view (brain.ts = model, layout.ts = map of regions and bundled
                     links, main.ts = rendering and lighting)
  test/              unit tests (npm test)
  package.mjs        builds luciole.vsix with a copy of src/luciole
tests/             Python tests (uv run pytest)
docs/              graph format, roadmap
examples/          small programs and a reference graph
```
