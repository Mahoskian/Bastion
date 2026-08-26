# Architecture

`mcadmin` is a CLI wrapped around a running Minecraft server. It has no daemon
of its own: every command reads the server's real state — process table, tmux
session, RCON socket, log files — decides something, and exits.

Two commands are the exception, and both are exceptions for the same reason —
something outside has to be *waited on*. `mc supervise` owns the JVM for as
long as it runs. `mc listen run` holds a websocket open so Discord can send
slash commands in. Both are launched detached into a tmux session by a command
that then exits (`mc start`, `mc listen start`), both publish a state file while
they run, and both are stopped by signalling the process rather than by deleting
that file. Everything else still reads state and exits.

The two daemons are independent. `listener.py` mirrors `controller.py` rather
than joining it, and the listener has a session name of its own, because tying
the bot to the server's lifecycle would take it offline at the moment a
question about the server is most worth asking.

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

Flattening costs the grouping a name hierarchy would have given, so
`cli/__init__.py` carries it in one table instead — `LAYOUT` names the help
panel each command sits in, and the order the panels and their contents print
in. Nothing else decides it: typer would order the root help by registration,
which always sinks every sub-command group below every flat command whatever
the two are about. A command missing from `LAYOUT` falls into typer's default
panel, which prints *above* the named ones, so the failure is loud; a test
pins both directions of the table against the commands that actually exist.

## Core, by concern

**Lifecycle** — `models.py` holds `Paths` and the JVM options, and is the one
place that knows where anything lives. `tmux.py` and `process.py` are typed
wrappers over the two OS facts the tool depends on. `supervisor.py` launches
the JVM and restarts it when it dies; `controller.py` is the object every
lifecycle command drives. `rcon.py` speaks the Source RCON protocol, and
`properties.py` reads `server.properties` as typed values.

**Notifications** — `notify.py` posts lifecycle events to Discord as a bot,
over REST rather than a gateway connection: sending needs only an HTTPS POST,
and the websocket exists to *receive*, which nothing here does. The events are
raised from `supervisor.py` because it is the only process that witnesses every
transition — `mc stop` signals it and `mc restart` deliberately bypasses it, so
a notification hung off either command would miss every unattended restart and
every crash. An unconfigured server gets a `NullNotifier`, a broken config
warns and degrades to one, and every send is wrapped: Discord must never be
able to delay a restart.

One consequence of that split is worth stating: the supervisor outlives every
`mc restart`, so a change to `supervisor.py` — or to anything it holds, like
the notifier — does not take effect until the supervisor process itself is
cycled with `mc stop && mc start`. A restart re-launches the JVM, not the
Python.

**Discord** — `notify.py` speaks out, `cli/listen.py` listens in, and they share
only the config file and the bot identity. Listening is a gateway websocket
because it is outbound: nothing on the box hosting the world becomes reachable
from the internet, which an interactions endpoint URL would have required.
`ui/discord.py` renders core's models as embeds under the same rule as the rest
of `ui` — a pure function of a model — and returns plain dicts rather than
`discord.Embed` objects, so it stays testable without discord.py installed.

`listener.py` is the lifecycle half: start, stop and a `ListenerState` that
distinguishes *started* from *connected*, since the process is up for a moment
before the gateway handshake finishes and again whenever it is resuming. It
does not restart on crash the way the supervisor does — discord.py reconnects
and resumes on its own, so the failure that loop would catch is handled a layer
down.

The listener is the one place where a `cli` module is not thin, because a slash
command *is* a command line: it parses arguments, calls `core`, and hands the
result to `ui`. The two things Discord adds are that `core` is called through
`asyncio.to_thread`, so a slow answer cannot stall the heartbeat and drop the
connection, and that every command defers its reply — Discord wants an
acknowledgement within three seconds, and a cold `/wrapped` is slower than that.

**Snapshots** — `repository.py` wraps restic. Content-defined chunking is why
an hourly snapshot of a multi-gigabyte world costs megabytes; the module's job
is deciding what to include, quiescing the world first when the server is up,
and never letting a restore write somewhere the caller did not mean.

**Observability** — `logs.py` parses the server log into records, `digest.py`
fingerprints them against a SQLite baseline so a digest reports what is *new*
rather than what is frequent. `gclog.py` parses G1 output and sizes the heap
from the measured live set. `metrics.py` keeps the time series that outlives
the logs, which rotate. `slow.py` is the composite: it puts overload warnings,
GC pauses, sessions and pregen progress on one clock and compares them, which
is the only way to rule a cause *out*.

**The world** — `stats.py` reads the per-player counters the server already
keeps, `deaths.py` joins gravestone placements to death messages so a death has
both a cause and a position, and `regions.py` reads region-file headers. All
three are read-only, and none of them needs a running server.

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

**Credentials live outside the tree, and are never optional-by-silence.** The
Discord bot token sits in `admin/.notify.json` at mode 600, ignored by name.
An absent config means notifications are off, which is not an error — but a
config that exists and does not parse *is* one, because silently reading a
typo as "off" is how a server notifies nobody for a month unnoticed.

**Missing evidence is not evidence.** Several reports draw on logs that rotate
away. When the data for a window is simply gone, the report says so rather than
reporting a zero: `why-slow` marks a window the GC log no longer covers as
neither blaming nor clearing the heap, and `deaths` counts a death whose
position was never logged instead of dropping it off the map.

**Aggregates are accumulated, not collected.** A world holds more chunks than
it is sensible to build objects for, so `regions.py` keeps a histogram and a
running top-N and discards the rest as it reads — which is what makes scanning
832,604 chunks a sub-second operation.

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
