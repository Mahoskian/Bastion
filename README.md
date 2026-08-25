# Bastion

*Keep the server standing.*

An operations toolkit for modded Minecraft servers: supervised lifecycle, RCON,
deduplicated restic snapshots, GC-log heap sizing, and TPS/GC metrics — all
from one CLI.

Bastion runs a Fabric server day to day. It supervises the JVM and brings it
back when it crashes, snapshots the world hourly at a few megabytes a run
instead of a few gigabytes, sizes the heap from what the garbage collector
actually did rather than from a guess, and tells you which mods are behind
before an upgrade breaks them.

**You bring the server.** This repository contains no mods, no world, no
configuration and no server jar — you install Fabric, choose your own mods and
let them write their own configs. Bastion attaches to whatever is there. See
[Point it at your server](#point-it-at-your-server).

---

## Requirements

| | |
|---|---|
| OS | Linux |
| Python | 3.12+, installed and run through [uv](https://docs.astral.sh/uv/) |
| `tmux` | the server runs inside a detached session |
| `restic` | snapshots — `sudo apt install restic` |
| Java | whatever your Minecraft version needs |

## Install

```bash
git clone https://github.com/Mahoskian/Bastion.git
cd Bastion/admin
uv sync
```

`admin/mc` is a thin launcher that runs the CLI from anywhere without
activating the venv. Symlink it onto your `PATH` if you like.

> **Note on the command name.** The entry point is currently `mc`, which
> collides with Midnight Commander and the MinIO client. It will be renamed
> to `bastion` before the first tagged release.

## Point it at your server

Bastion needs a directory that already holds a Fabric server — the launcher
jar, `server.properties`, and `mods/` with whatever you chose to run. Setting
one up is the [Fabric installer's](https://fabricmc.net/use/server/) job, not
this tool's.

Point at it with an environment variable:

```bash
export MC_SERVER_DIR=/srv/minecraft
export MC_BACKUP_DIR=/mnt/backups/minecraft   # optional; defaults to $MC_SERVER_DIR/backups
mc status
```

Or skip the variable entirely by cloning so that `admin/` sits *inside* your
server directory — Bastion derives the server root from its own location, one
level above `admin/`:

```
/srv/minecraft/
├── fabric-server-mc.26.2-loader.0.19.3-launcher.1.1.2.jar
├── server.properties
├── mods/
├── world/
└── admin/          <- this repository
```

Two things it expects, and will tell you if they are missing:

- exactly one `fabric-server-*.jar` in the server root — the Minecraft and
  loader versions are read from its filename, so nothing is hard-coded
- `enable-rcon=true` with an `rcon.password` set in `server.properties`, for
  every command that talks to a running server

It assumes one server per machine: the tmux session it supervises is a single
named session, so `MC_SERVER_DIR` relocates the files, not a second instance.

## Commands

`mc --help` groups these into the same sections used below.

```
mc status                 server state, players online, key settings
mc start [--heap 24G]     start supervised, in a detached tmux session
mc stop / restart         stop takes the session down; restart keeps it
mc console                attach to the live console
mc rcon "<command>"       run console commands over RCON
```

**Snapshots** — deduplicated, verifiable, restic-backed.

```
mc snapshot init          create the repository and its password file
mc snapshot now           snapshot; quiesces the world first if the server is up
mc snapshot list          newest first, with the repo's size after dedup
mc snapshot restore <id>  restore in place, or elsewhere with --target
mc snapshot check         verify integrity (--read-data re-reads a 5% sample)
mc snapshot forget        apply retention: 12 hourly, 7 daily, 4 weekly
mc snapshot pause/resume  hold the scheduled runs during maintenance
```

**Performance and health.**

```
mc logs tail|digest       digest shows only problems it hasn't reported before
mc metrics sample|show    time series of load and GC behaviour
mc gc                     size the heap from the GC logs
mc why-slow               why a given hour was bad, from four logs at once
mc chunks                 chunk counts, size distribution, and bloated chunks
```

**For the players.**

```
mc wrapped [player]       leaderboards, or one player's card
mc deaths                 plot deaths and find the spot that keeps killing
```

**Mods and client packs.**

```
mc mods check             outdated mods, and mods that block a Minecraft upgrade
mc mods fetch|install     stage jars, then install them — archiving what they replace
mc mods manifest          regenerate the tracked mod lists (--check to detect drift)
mc mrpack                 build a Modrinth .mrpack for players
```

`mc why-slow` puts the overload warnings, the GC pauses, the join times and the
pregenerator's own progress lines on one clock, then says which of them was big
enough to explain the rest — and, when the GC log has rotated past the window,
says that the heap can be neither blamed nor cleared rather than clearing it on
evidence that no longer exists. `mc chunks` reads only the 8KiB header of each
region file, so counting 832,604 chunks across 1,150 files takes under a
second.

`mc wrapped` and `mc deaths` read what the server already writes down: the
per-player stats files, and — for death positions — the gravestone placements
in the log, joined to the vanilla death message on the same second.

`mc mods check` audits every installed jar against Modrinth in three bulk
requests rather than one per mod. `mc mods manifest` writes a `README.md` into
your `mods/` and `client-install/mods/` folders listing each jar with its exact
version and download url — the file to commit in place of jars you may not
redistribute, and `--check` fails when it has drifted from what is installed.
`mc mrpack` builds an importable Modrinth pack for your players, and writes its
`modrinth.index.json` out beside it for the same reason.

## What a snapshot covers

Minecraft state, and nothing else: `world/`, `config/`, `mods/luckperms/`, the
server JSON files, and the two databases Bastion itself writes. Not the mod
jars — those are re-downloadable, and `mods/README.md` records which build each
one was. Not Bastion's own source either; that is what git is for.

Excluded on purpose: `session.lock` (a runtime lock), LuckPerms `libs/`
(re-downloaded on demand), and the Distant Horizons LOD caches — derived data
that regenerates itself, and roughly a gigabyte of it per world.

The dedup is the point. A 4.9G world that would produce a 2.7G tar archive
every hour instead costs a few megabytes per snapshot, because only changed
chunks are stored.

## Scheduling

```cron
0 * * * *   /path/to/admin/mc snapshot now --scheduled   # hourly snapshot
30 4 * * 0  /path/to/admin/mc snapshot check             # weekly integrity check
* * * * *   /path/to/admin/mc metrics sample --quiet     # ~0.14s per sample
10 * * * *  /path/to/admin/mc metrics record             # save GC analyses before logs rotate
```

`--scheduled` honours the pause flag, so `mc snapshot pause` stops cron runs
without touching manual ones.

## Development

```bash
mc test        # ruff + pytest
```

The suite runs against a real restic repository rather than a mocked one, so
`restic` must be installed for those tests to run; they skip cleanly if it
isn't. Everything else runs without network.

Further reading:

| | |
|---|---|
| [`admin/docs/ARCHITECTURE.md`](admin/docs/ARCHITECTURE.md) | how the CLI is put together, and the rules that keep it that way |
| [`admin/docs/ROADMAP.md`](admin/docs/ROADMAP.md) | what is built, what is next, and what measuring a live server turned up |

## Layout

```
admin/
  mc                    thin launcher — runs the CLI without activating the venv
  mcadmin/core/         logic: lifecycle, snapshots, logs, metrics, mods
  mcadmin/cli/          argument parsing, one module per command group
  mcadmin/ui/           rendering; nothing here decides anything
  tests/                pytest suite, no network
  docs/                 architecture and roadmap
  pyproject.toml        packaging, ruff and pytest configuration
```

`core` never imports `ui`, and `ui` never decides anything — that separation is
what makes the logic testable without a terminal.
[ARCHITECTURE.md](admin/docs/ARCHITECTURE.md) covers the rest.

## What Bastion writes into your server directory

Nothing it cannot rebuild, and nothing it does not announce:

| Path | What it is |
|---|---|
| `admin/.runtime.json` | the supervisor's live state; meaningless once the JVM exits |
| `admin/.metrics.db` | the metrics time series — outlives the GC logs, which rotate |
| `admin/.log-baseline.db` | fingerprints of log entries already reported |
| nothing else | `wrapped`, `deaths`, `chunks` and `why-slow` only read |
| `backups/` | the restic repository and the password that unlocks it |
| `fetch-mods/` | staged jar downloads, and the jars `install` swapped out |
| `logs/gc.log` | written by the JVM flags `mc start` sets |

The shipped `.gitignore` excludes all of it, along with everything else a
Minecraft server leaves lying around. It is an allowlist: it ignores the whole
directory and re-admits only `admin/`, so no world save, secret, player file or
mod jar can reach a remote by accident — including ones that do not exist yet.

## License

MIT — see [LICENSE](LICENSE).

The licence covers Bastion. It says nothing about the mods you run, the configs
they emit, or the world you build with them — none of which are here.
