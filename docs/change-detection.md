# Change detection (`gamewatch.py`)

The server can only serve the game well if everyone runs compatible pieces: the same game build,
an up-to-date client helper, and the same mods that add levels. `gamewatch.py` notices when any of
these change. It reports each change in three places:

- on the **server console**: `-- change: ...` for information, `!! change: ...` for warnings;
- in **`data/logs/changes.jsonl`**, one JSON event per line (`python di_server.py --changes 40` prints the
  last 40);
- over **HTTP**: `GET /_changes?since=<id>` returns the events, and `GET /_status` lists the current
  `warnings`.

Its state (known players, User-Agents, routes and the last game snapshot) is kept in
`data/watch_state.json`, so changes made while the server was stopped are reported at the next start.

## Event format

```json
{"id": 7, "t": "2026-10-15T10:31:02", "kind": "mod_updated", "level": "info",
 "message": "mod monstersu_port updated: 0.1.0 -> 0.2.0 (players need the same version to share levels)",
 "mod": "monstersu_port"}
```

`id` increases within one run of the server. `level` is `info` or `warning`. Some kinds add extra
fields such as `player`, `mod`, `old`, `new`, `route`, `status` or `md5`.

## Detectors

### 1. Clients (always on, passive)

This detector reads the traffic every game already sends: the presence and host announcements of the
client helper, and the `User-Agent` of every request.

| Kind | Level | When |
|---|---|---|
| `player_new` | info | A player name is seen for the first time. |
| `client_version_changed` | info | A player's helper version (`User-Agent: dinet_bk/<n>`) changed. |
| `client_outdated` | warning | A player's helper is older than the newest one any player has used. |
| `zones_changed` | info | A player's level-id fingerprint changed, because mods that add levels were installed, updated or removed on that PC. |
| `agent_new` | info | A User-Agent never seen before: another game build, a new helper, or a tool such as curl. |

Current warnings in `/_status` cover the players online now: outdated helpers, and players whose
fingerprints differ ("joins between groups load the wrong level").

### 2. Routes (always on, passive)

| Kind | Level | When |
|---|---|---|
| `endpoint_new` | info, or warning if the server answered ≥ 400 | The game used a route the server has never seen. |

A route is the method plus the first five path segments. Segments that look like ids (numbers, long
hex strings, GUIDs) are folded into `*`. After a game update, a new route is usually the first sign of
a service the server doesn't handle yet; the request itself is in `requests.jsonl`. On a server with no
route history (the first run), routes are learned silently, so the first session doesn't flood the
log.

### 3. Game folder (`--game`, active, read only)

At start-up and then every `--watch-interval` seconds (default 30; `0` checks only at start-up), the
watcher takes a snapshot of:

- `DisneyInfinity3.exe`: size, time and md5 (the md5 is computed again only when size or time change),
  and the build it belongs to;
- `DisneyInfinity3_local.exe`, the patched copy;
- the CrabeLoader marker: `CrabeLoader.asi` (1.1 and later) or `bink2w32_orig.dll` (1.0);
- `assets/presentation/presentation.zip`;
- every `mods/<id>/mod.json` (name and version) plus a hash of that mod's file names, sizes and times.
  `*.log`, `*.tmp` and `*.bak` files and the `logs`, `di_saves` and `__pycache__` folders are ignored,
  so the client helper's own log never counts as a change;
- `mods/dinet_online/dinet_bk.dll` and `mods/dinet_online/dinet_bk.asi` (the copy CrabeLoader 1.1+ loads).

It compares each snapshot with the previous one, including the one saved before a restart.

| Kind | Level | Meaning / what to do |
|---|---|---|
| `game_watch_start` | info | First snapshot of a folder: build, number of mods, CrabeLoader. |
| `game_exe_changed` | info if the build is known, warning otherwise | The game was updated or replaced. An unknown build means the exe patch and the helper's addresses may not apply. |
| `local_exe_changed` / `local_exe_removed` | info / warning | The patched copy was rebuilt or deleted (run `install_crabe_mod.py` again). |
| `crabeloader_changed` | info / warning | CrabeLoader was installed or removed. Without it the client mod doesn't load. |
| `presentation_changed` | warning | The game's menu archive changed. Rebuild the mod's menus with `install_crabe_mod.py`. |
| `mod_added` / `mod_removed` | info (warning when the client mod is removed) | A CrabeLoader mod appeared or disappeared. |
| `mod_updated` | info | A mod's `version` changed. Players need the same version to share levels. |
| `mod_files_changed` | info | A mod's files changed without a version change. |
| `client_dll_changed` | info | The installed helper DLL changed. |
| `client_dll_mismatch` | warning | The installed helper (`.dll` or `.asi`) isn't the build in `--client-dll` (default: the client folder next to this repository). |

Current warnings for the folder: unknown game build, CrabeLoader missing, client mod missing.

A file rewritten with exactly the same size inside the same file-system timestamp tick (about 15 ms
on Windows) isn't noticed. Hashing the large game files on every check would cost far more than this
case, which never happens in practice.

## Using it from code

```python
import gamewatch
w = gamewatch.Watch("data", game_dir=r"<game folder>")
w.start(interval=30)           # background thread; events are printed and saved
w.player("ana", "dinet_bk/2", "AAAAAAAA-0")
print(w.warnings(), w.log.since(0))
```

`Watch` is the facade the server uses. `ChangeLog`, `ClientTracker`, `EndpointTracker` and
`GameWatcher` also work on their own (see the tests).
