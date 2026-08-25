# admin/ roadmap

Ideas for extending the `mc` toolkit, ordered by leverage. Two findings from
probing the live server shape several of these:

- **spark is installed but unusable over RCON.** `spark tps` / `spark health`
  return an empty body, and holding the socket open for 15s catches no late
  packet either -- spark replies asynchronously to the command source and the
  RCON session is already closed. Any TPS/MSPT collection needs a different
  transport.
- **backups are ~99% redundant.** `world/` is 6.4G and every `mc-*.tar.zst` is
  2.8G, taken hourly (~67G/day). Consecutive archives two hours apart differ by
  ~1500 bytes of *compressed* size.
- **RCON latency is not a tick-health proxy.** `list` answers in 0.4ms off a
  cached player list, not from the server thread.

## Tier 1 -- useful

### 1. `mc gc` -- heap sizing from measured data [DONE]
Implemented in `mcadmin/core/gclog.py` (rendering in `mcadmin/ui/gc.py`). `mc gc` analyses the most recent JVM
run, `mc gc --all` summarises every run in the logs, `--json` for scripting.

What it found on the first real read: live set **955M against a 12G heap** (13x
headroom), allocation 107M/s, promotion 530K/s, p99 pause 28.7ms, GC overhead
0.02%, zero full GCs. Suggested heap is 3G. Two interactions the raw log does
not show on its own:

- `InitiatingHeapOccupancyPercent=15` would start concurrent marking at 461M on
  a 3G heap -- *below* the live set, so marking would never stop. Shrinking the
  heap requires raising IHOP (~60%) in the same change.
- `AlwaysPreTouch` commits all 12G at startup, so RSS tracks `-Xmx` rather than
  real use. That matters here because the box also runs a Minecraft client
  (14.5G + 12.5G RSS of 60G).

Still open: the recommendation reflects the observed load (2 players, no
pregen). Re-run `mc gc` after a busy session before committing to a smaller
heap.

### 2. Dedup backups (restic)  [DONE]
Replace the tar.zst pipeline with restic against the same directory. Content-
defined chunking over region files means an hourly snapshot costs only the
chunks that actually changed -- realistically 50-100x less. Keep
`mc backup list/info/restore` as the interface and swap only the engine.
`restic check` also gives real verification, which tar can't do cheaply.

### 3. `mc backup verify` -- prove a restore works
An untested backup is not a backup. Extract the newest archive to a temp dir,
boot the server against it on a scratch port, wait for `Done (Xs)!`, RCON
`list`, shut down, report. Weekly from cron.

### 4. `mc logs digest` -- noise-cancelling log distillation  [DONE]
Implemented in `mcadmin/core/logs.py` (parsing) and `mcadmin/core/digest.py`
(fingerprint baseline in SQLite at `admin/.log-baseline.db`).

Measured across all 11 log files: **14,226 lines, 4,834 of them warnings or
errors, collapsing to 173 distinct patterns.** After the first run establishes
the baseline, a digest reports only patterns it has never seen, plus the
session summary -- playtime, deaths, advancements, chat, disconnect reasons.

`mc logs` is now a group: `mc logs tail` (what `mc logs` used to do) and
`mc logs digest`. Useful flags: `--since 6h`, `--current` (this session only),
`--reset` (forget the baseline), `--no-learn` (report without recording).

Details worth remembering:

- Numbers, UUIDs, hex ids and colour codes are normalised away before hashing,
  so `chunk [224, 87]` and `chunk [65, -86]` are one pattern -- but the
  exception type from an attached stack trace *is* folded in, so two failures
  sharing a message stay distinct.
- Deaths are only counted for names seen joining, which is what keeps the 42
  villager deaths out of the 22 player deaths.
- A restart never emits "left the game", so `SERVER_STOP`/`SERVER_START` close
  open sessions. Without that, playtime ran to the present -- one player showed
  34h56m inside a 26h window.

Still open: nothing pushes the digest anywhere (see item 7); it has to be run
by hand.

### 5. Metrics + graphing (without spark)  [DONE]
Took the process-level path: `mcadmin/core/metrics.py`, SQLite at
`admin/.metrics.db`, rendered as terminal sparklines by `mcadmin/ui/metrics.py`.

- `mc metrics sample` -- one sample: state, players, RSS, cumulative CPU, heap
  used (tailed from gc.log, not a full parse), world size. **0.14s per run**,
  world walk included, so cron takes one a minute.
- `mc metrics show --since 6h` -- now/mean/peak plus a sparkline per metric.
- `mc metrics record` / `mc gc --record` -- copies the GC analysis into the
  store, keyed by run start so re-recording a live run refreshes it.
- `mc metrics gc` -- recorded runs, **including ones whose logs have rotated**.

This is the answer to "are those one-off?": `mc gc` and `mc logs digest`
recompute from logs that either roll away (GC, ~50M cap) or are not backed up
at all (`logs/` is in neither the tar nor the restic include set). The store is
where anything worth comparing across weeks now lives.

Two details worth remembering:

- CPU is cumulative in `/proc`, so a rate needs two samples, and a JVM restart
  resets the counter -- a negative delta is dropped rather than plotted.
- Sparklines flatten any series varying by less than 1% of its peak. Without
  that floor, min-max scaling turned a rounding-level wobble in a 14G RSS into
  a full-height cliff that read as a real event.

Still open: no MSPT or TPS. Neither is reachable without a mod, since spark's
output does not survive RCON (see the top of this file). Add a Prometheus
exporter jar only if the tick numbers turn out to be missed.

### 6. `mc mods check` -- Modrinth update/compat scan  [DONE]
Implemented in `mcadmin/core/mods.py`, over a shared bulk client in
`mcadmin/core/modrinth.py`. `mc mods check [--target 26.3] [--client]`.

Uses the bulk endpoints (`/version_files`, `/version_files/update`,
`/projects`), so auditing 162 jars is **3 requests, not 162** -- the rate limit
stops being something to design around. `mc mrpack` was refactored onto the
same client: it used to make one request per jar with a 0.12s politeness delay,
about 20s of pure waiting for 148 client jars.

First real run: **155 current, 7 outdated, 0 unknown** -- Biomes O' Plenty,
C2ME, Fabric API, JEI, Moog's Structure Lib, ScalableLux, Xaero's World Map.

**Correcting an earlier claim in this file:** the note that "8 animalgarden
mods are still on 26.1.x builds" was wrong. It came from reading filenames.
12 jars have `26.1.x` in the name, but Modrinth reports their builds as
`game_versions=['26.1','26.1.1','26.1.2','26.2']` -- they declare 26.2
explicitly. The filename records the build's origin, not its compatibility
ceiling, so the scanner uses the declared versions and correctly leaves them
alone. There is a test pinning exactly this case.

`--target` works in both directions. Since 26.2 is the newest release, the
useful direction today is *down*: `mc mods check --target 26.1` reports
50 compatible / 13 upgrade / 93 downgrade / 6 missing. Direction comes from
`date_published`, because version strings are not comparable across projects,
and "compatible" is checked first -- 50 mods declare several Minecraft
versions at once and need no change whatever else Modrinth offers.

`--urls` prints copy-pasteable download and project-page links.
`mc mods fetch` downloads into `fetch-mods/`, verifying sha512 then sha1
before anything touches the disk, and writes a manifest. `mc mods install`
then acts on that manifest: it refuses while the server is live, archives each
replaced jar into `fetch-mods/replaced/`, and clears staging only for jars that
installed cleanly.

**Server vs client is decided by where the jar already lives, not by Modrinth.**
The obvious signal -- `client_side` / `server_side` -- does not work: across 60
of this server's mods, 37 declare "required" on *both* sides and only 10 are
unambiguous. So `fetch` scans `mods/` and `client-install/mods/` separately and
records the exact file each download replaces. `--side server|client|both`.

One consequence found by running it: 5 mods (Fabric API, JEI, BOP, ScalableLux,
Xaero's World Map) are in both sets and share a single download, so install
copies out of staging rather than moving -- otherwise the second destination
would find the file gone.

Two details:

- `BEHIND` (the installed build never claimed this Minecraft version) is kept
  separate from `OUTDATED` (a newer build exists), because they need different
  responses.
- A typo'd `--target` would otherwise report every mod as a blocker, so the
  target is checked against Modrinth's known releases first and warned about.
  26.2 is currently the newest release, so there is nothing to upgrade to yet.

Also fixed while here: `PackSpec` hard-coded `26.2` / `0.19.3`. Both now come
from the launcher jar's filename via `Paths.versions()`.

### 7. Push notifications on failure  [crash-loop guard DONE]
`Supervisor` already refuses to restart after 3 crashes inside 60s each, so the
restart-loop half of this is handled. What is still missing is anyone finding
out: push to ntfy/Discord on crash, on OOM in the GC log, and on backup
failure. `BackupJob` logs failures nobody reads, and cron now appends stderr to
`backups/cron-errors.log` that nobody reads either.

## Tier 2 -- fun

### 8. `mc wrapped` / leaderboards
`world/stats/*.json` is vanilla-rich: playtime, blocks mined, mobs killed,
distance by travel type, deaths. Empty until players are saved off, but it
fills in. Terminal leaderboard or an HTML page for players.

### 9. Death map
Death coordinates and gravestone locations are already in the logs. Scatter-plot
them over a rendered map; hot spots find the ravine everyone keeps falling into.

### 10. Chunk forensics
2918 `.mca` files. Parse region headers for chunk count and size distribution to
track pregen progress and find bloated chunks (usually somebody's entity farm).

### 11. `mc why-slow`
Composite view: correlate the GC log against tick warnings and player join times
to answer "was 6pm yesterday bad, and why".
