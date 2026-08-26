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

### 7. Push notifications  [lifecycle DONE]
Implemented in `mcadmin/core/notify.py`, raised from `mcadmin/core/supervisor.py`.
`mc notify setup` writes the token, `mc notify test` proves the path end to end,
`mc notify status` says what is configured and from where.

Six events, all from the supervisor: starting, up (once RCON answers),
restarting, stopped, crashed, and gave up after the crash-loop guard trips.
The supervisor is the emitter because it is the only thing that sees all of
them — `mc stop` signals it, and `mc restart` deliberately leaves it alone and
lets its loop bring the server back, so any notification attached to the CLI
commands themselves would miss every unattended restart and every crash.

Three things worth remembering:

- **A bot does not need a gateway connection to send.** A bot token can POST to
  `/channels/{id}/messages` directly. The websocket is for *receiving*, so
  broadcast-only costs no dependency and no second daemon; chat relay or slash
  commands would add it later without changing any of this.
- **Exit code is the only thing separating a restart from a crash.** By the
  time the loop sees the JVM gone, an intentional `mc restart` and a crash look
  identical — except that a JVM told to stop shuts down cleanly and exits 0.
  Announcing every exit as a crash would cry wolf on every restart.
- **"The JVM is alive" is not "the server is up".** This mod set spends over a
  minute between the two, and *up* is the message anyone waiting to play wants.
  A side thread polls RCON and announces the boot time; it stays off the main
  loop so that watching for readiness cannot delay noticing a crash mid-boot.

One thing found by running it against a real bot: **`/users/@me/guilds` is
rate-limited far harder than sending is.** A first version checked guild
membership on every `mc notify test` to give a better error, and the check
itself started returning 429 -- so the diagnosis is now only asked for once a
403 has already happened. It is worth asking then, because an uninvited bot
authenticates perfectly and 403s on every channel in existence, which reads
exactly like a channel-permission problem and is not one.

### 7b. Slash commands  [read-only DONE]
`mc listen` (`mcadmin/cli/listen.py`, embeds in `mcadmin/ui/discord.py`) answers
`/status`, `/players`, `/wrapped [player]` and `/deaths` over the gateway.

- **The gateway, not an interactions endpoint URL.** Both receive commands; the
  gateway is an outbound websocket, so nothing on the box running the world has
  to be reachable from the internet. The endpoint URL would have meant inbound
  HTTPS, a signature check on every request, and a reply inside 3 seconds.
- **A daemon, and deliberately not the supervisor's.** Folding it into the
  supervisor would have been free — that process is already long-lived — and
  would have made commands work only while the server was up. The answer you
  most want from a phone is about a server that is down. It gets its own tmux
  session and its own state file for the same reason, and `mc listen
  start/stop/status/console` mirrors `mc start/stop/status/console` rather than
  inventing a second way to manage a process.
- **No intents.** Interactions need none. Reading chat for `!status` would have
  needed the privileged Message Content intent, and given up autocomplete,
  argument validation and Discord's own permission integration to get it.
- **Every command defers.** `/deaths` parses every log file; `/wrapped` walks
  the stats directory. Both are slower than Discord's 3-second acknowledgement
  window, so all four defer and edit the answer in, and `core` is called
  through `asyncio.to_thread` so a slow answer cannot stall the heartbeat.

`/wrapped` and `/deaths` report different death counts for the same player and
both are right: stats files are cumulative, while the death map can only see
the logs that have not rotated away. Same shape as the two playtime figures in
item 8.

### 7c. Charts in the replies  [DONE]
`mcadmin/ui/charts.py` renders PNGs that the embed carries via
`attachment://`, so the plot sits inside the embed rather than under it.
matplotlib is an optional extra (`uv sync --extra charts`); without it the same
commands answer as text, and the embed keeps the fields the chart would have
replaced.

`/status` and `/players` deliberately stay text. They are a handful of
key/value facts, and the form heuristic says that is a stat block, not a plot.

Four things worth remembering, three of them found by rendering and looking:

- **Bar geometry cannot live in data units.** The first version set the corner
  radius and the bar thickness in data coordinates, so the same code drew
  invisible rounding on a leaderboard whose x-axis ran to 12,445 and giant
  lozenges on a card whose x-axis ran 0 to 1. Bars are now a line with a
  point-width, square at the baseline and capped with a round marker at the
  data end.
- **Labels outside the axes need offsets in points, not x-units.** Same failure
  one layer up: values placed at `x * 1.03` collided as soon as the axes got
  narrower.
- **Title spacing in figure fractions is not portable between figures.** A gap
  that clears a 15pt title on a tall figure overlaps it on a short one. The
  offsets are in inches now.
- **A hot spot is drawn at the radius the clustering actually used.** A
  fixed-size ring at the centroid floated over empty ground when the members
  were spread out, which read as "something is here" when nothing was.

The palette is the validated categorical set stepped for a dark surface, and
the slot order is the colourblind-safety mechanism rather than a preference --
checked with the palette validator against the surface actually rendered on,
not eyeballed. The death map faces a real constraint from that: scatter needs
all-pairs separation, where only the first three slots clear the floors, which
is one more reason the dimensions are faceted rather than coloured.

Still open: the privileged half. `/restart`, `/stop` and `/snapshot now` need
`default_member_permissions` on the command *and* a server-side check of the
invoking member's roles, because the guild's Integrations settings can override
the default. Nothing destructive should ship on the client's word.

Still missing: OOM in the GC log, and backup failure. `BackupJob` logs failures
nobody reads, and cron appends stderr to `backups/cron-errors.log` that nobody
reads either. Both now have somewhere to go — they need a caller, not a
transport.

## Tier 2 -- fun

### 8. `mc wrapped` / leaderboards  [DONE]
Implemented in `mcadmin/core/stats.py` (rendering in `mcadmin/ui/stats.py`).
`mc wrapped` is the leaderboard, `mc wrapped <player>` one player's card,
`--board deaths` a single table, `--json` for scripting.

The stats are not in `world/stats/` on this version: 26.x moved them to
`world/players/{stats,advancements,data}/`. `Paths.player_dir()` accepts both,
because the older layout is still what most documentation describes.

First real run, across four players: 20.9h / 17.0h / 9.9h / 5.4h of playtime,
7,332 blocks mined at the top, 101.5km travelled at the top, 24 deaths at the
top. Three numbers needed correcting before any of that was true:

- **`play_time` is in ticks, not seconds.** It only advances 20/second while
  the server keeps up, so it undercounts a laggy evening -- and it will
  disagree with the wall-clock playtime `mc logs digest` computes from join and
  leave lines. Both are right; they measure different things.
- **Damage counters are `round(damage * 10)`, and damage is in half-hearts**,
  so a heart is 20 of them. Read as raw hearts, the top player took 6,854 a
  session; the real figure is 685, which is ~29 hearts per death and matches a
  10-heart bar plus regen between fights.
- **Advancements are mostly noise.** Moksha_ has 1,316 "done" and 30 that a
  player would call advancements: mods grant hundreds of their own, and every
  recipe unlock is an advancement too. The board counts `minecraft:` entries
  that are not `minecraft:recipes/`.

Stats files are only written when the server saves a player off, so every
report carries the file mtime as an "as of" line rather than implying it is
live.

Still open: the HTML page for players. The terminal board is the half that gets
used day to day.

### 9. Death map  [DONE]
Implemented in `mcadmin/core/deaths.py` (rendering in `mcadmin/ui/deaths.py`).
`mc deaths [--since 3d] [--player X] [--dimension the_nether] [--json]`.

Vanilla death messages carry the cause and never the position. The gravestone
mod logs `Placed <player>'s gravestone at (x, y, z) in <dimension>` on the same
second, so the two lines join into a death with both. The join is what makes
this work, and the two ways it fails to join both matter:

- **A placement with no death message is still a death** -- 4 of the 15 on this
  server -- so the location survives even when the cause does not.
- **A death message with no placement** happened where no grave could be
  placed, or before the mod was installed. 18 of 33 deaths here are in that
  group; they are counted and reported, never plotted, and the report says so
  rather than quietly showing 15.

First real run: 33 deaths, 15 located, 3 hot spots. Two nether clusters of 6
apiece (piglins at 18,82,73; skeletons at 119,65,15) and one overworld cluster
of 3, all falls, at 189,124,683. One grave was never collected -- diddy_bot
still has gear at 134,73,22 in the nether.

Details worth remembering:

- Clustering is horizontal only. A ravine is one place at every depth, and
  including y split the fall cluster into three.
- Clustering is per dimension. Nether coordinates are 1:8 to overworld ones, so
  the same numbers in two dimensions are not the same place -- there is a test
  pinning exactly that.
- The plot shrinks its width rather than its height when a region is taller
  than it is wide. Squashing it into the height cap put the two axes on
  different scales, which is a plot that lies about where things are.

### 10. Chunk forensics  [DONE]
Implemented in `mcadmin/core/regions.py` (rendering in `mcadmin/ui/regions.py`).
`mc chunks [-d the_nether] [--kind region|entities|poi] [--timeline] [--json]`.

Only the 8KiB header of each file is read -- 1024 location entries and 1024
timestamps -- so scanning **1,150 region files and 832,604 chunks takes 0.7s**.
Nothing per-chunk is kept: a histogram and a running top-N, and the rest is
discarded as it goes.

First real run: overworld 336,874 chunks / 2.53G, the end 421,201 / 1.62G, the
nether 74,529 / 559M. 113 chunks over 64K, the largest 228K in the nether. 75M
of the overworld is free space *inside* region files -- rewritten chunks leave
their old sectors behind and region files never shrink.

**242 of the 1,150 region files are zero bytes**, which is what the first
version reported as 242 corrupt files. They are not damaged: the server creates
a region file when something touches the area and only writes a header once a
chunk in it is generated. They are counted as empty regions now, which also
keeps the far rarer genuinely-truncated file visible instead of buried.

Sizes are *allocated* sizes, rounded up to the 4KiB sector. The exact
compressed length is in each chunk's own header, which would mean a seek per
chunk -- 580,000 of them for the overworld -- to sharpen a number already right
to within a sector.

The timestamps make `--timeline` a pregen progress view: 72,569 nether chunks
were last written on 08-23, 428 on 08-24, 1,532 on 08-25.

### 11. `mc why-slow`  [DONE]
Implemented in `mcadmin/core/slow.py` (rendering in `mcadmin/ui/slow.py`).
`mc why-slow [--since 24h] [--at "yesterday 18:00"] [--bucket 10m] [--json]`.

Four records of the same minutes, on one clock: overload warnings from the
server log, pauses from the GC log, join/leave events, and Chunky's own
progress lines. The arithmetic that matters is lost tick time against GC pause
time in the *same* window -- that is what rules the heap out.

First real run answered the question it was built for. The worst window in
three days was **08-23 19:15, 319s of tick time lost**, and it was a Chunky
pregeneration: 59,193 chunks in fifteen minutes, peaking at 296 chunks/s, with
1,339 errors alongside it and one player online. Not players, not the heap.

Three things the first version got wrong and now does not:

- **"Not the heap" was being claimed with no GC data.** The GC logs rotate
  after ~50M and had long since rolled past 08-23, so "G1 paused for 0ms"
  actually meant "nothing was measured". Each bucket now records whether any GC
  log covers it, and an uncovered window says the heap can be neither blamed
  nor cleared.
- **Summed lag is a floor, not a total.** The server logs "Can't keep up!" at
  most once every 15 seconds however far behind it falls, so the real figure is
  always higher. The report says "at least".
- **Sessions have to be read from every record, not the window.** Extracting
  events from the window alone reported an empty server for any window that
  opened mid-session.

The timeline strip folds to a fixed width by taking the *worst* bucket per
column rather than the mean. Averaging is exactly the wrong summary for a line
whose job is finding outages.
