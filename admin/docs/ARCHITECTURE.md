# Architecture

`mcadmin` is a CLI wrapped around a running Minecraft server. It has no daemon
of its own: every command reads the server's real state — process table, tmux
session, RCON socket, log files — decides something, and exits.

## The three layers

```
mcadmin/cli/    parse arguments, call core, hand the result to ui
mcadmin/core/   all the logic, and the only layer that touches the outside world
mcadmin/ui/     turn a result into terminal output
```

The rule that keeps this honest: **`core` never imports `ui`, and `ui` never
decides anything.** A function in `ui` that branches on more than "is this
value empty" is usually logic that escaped from `core`.

`cli` modules stay thin on purpose. If a command module grows a conditional
that is about the server rather than about arguments, it belongs one layer
down. `cli/__init__.py` registers the command groups; `snapshot`, `logs`,
`metrics` and `mods` are sub-commands, and the rest are flattened onto the root
so `mc start` does not become `mc server start`.

## Core, by concern

**Lifecycle** — `models.py` holds `Paths` and the JVM options, and is the one
place that knows where anything lives. `tmux.py` and `process.py` are typed
wrappers over the two OS facts the tool depends on. `supervisor.py` launches
the JVM and restarts it when it dies; `controller.py` is the object every
lifecycle command drives. `rcon.py` speaks the Source RCON protocol, and
`properties.py` reads `server.properties` as typed values.

**Snapshots** — `repository.py` wraps restic. Content-defined chunking is why
an hourly snapshot of a multi-gigabyte world costs megabytes; the module's job
is deciding what to include, quiescing the world first when the server is up,
and never letting a restore write somewhere the caller did not mean.

**Observability** — `logs.py` parses the server log into records, `digest.py`
fingerprints them against a SQLite baseline so a digest reports what is *new*
rather than what is frequent. `gclog.py` parses G1 output and sizes the heap
from the measured live set. `metrics.py` keeps the time series that outlives
the logs, which rotate.

**Mods** — `modrinth.py` is a bulk client: every lookup batches, so auditing
163 jars is three requests. `mods.py` turns those answers into a verdict per
jar. `mrpack.py` builds the client pack, and `manifest.py` renders the files
that stand in for jars a repository cannot legally carry.

## Data flow, in one example

`mc mods check`:

1. `cli/mods.py` resolves `Paths` and picks the directory to scan.
2. `core/mods.py` hashes each jar and asks `core/modrinth.py` about all of them
   at once.
3. Each answer becomes a `ModReport` with a `ModStatus` — the whole verdict is
   data, not printing.
4. `ui/mods.py` renders the `ModScan`. Nothing is decided here.

Everything else follows the same shape: a frozen pydantic model comes out of
`core`, and `ui` is a pure function of it. That is what makes the logic
testable without a terminal, and what lets `--json` exist wherever it does.

## Design choices worth knowing

**Models are frozen.** `Paths`, `ModReport`, `PackResult` and friends are
immutable pydantic models. Nothing relocates or mutates mid-run, so a value
that was read once cannot mean something different later in the same command.

**Destructive things stage rather than act.** `mc mods fetch` downloads into
`fetch-mods/` and stops; `mc mods install` is a separate decision, refuses
while the server is live, and archives every jar it replaces. Swapping a mod
under a modded server can corrupt a save, so nothing does it implicitly.

**Nothing is hard-coded that the disk already knows.** The Minecraft and
loader versions come from the launcher jar's filename via `Paths.versions()`.
The server directory is derived from this file's own location, with
`MC_SERVER_DIR` as the override.

**Bytes are verified before they land.** Downloads are checked against sha512
then sha1 before anything is written; a mismatch never reaches disk.

## Tests

`tests/` mirrors `core/` and runs without network — the Modrinth API is faked
by small classes that also count calls, which is how "one bulk request, not
163" stays true. The exception is `test_repository.py`, which drives a real
restic repository in a temp directory and skips cleanly when restic is not
installed; a mocked backup proves nothing about restore.

```bash
mc test              # ruff + pytest
mc test --no-lint    # pytest only
```
