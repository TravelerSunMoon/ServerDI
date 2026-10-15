# ServerDI: a local online server for Disney Infinity 3.0 Gold Edition (PC)

Disney's online services for Disney Infinity 3.0 (`api.disney.com`) were shut down. ServerDI runs a
replacement on your own PC or local network, so the game's online features work again:

- Disney ID sign-in
- profile and cloud saves
- the Toy Box store
- friends, invites and join requests
- text chat: press **T** in game, and the text shows above your character's head
- two-player Toy Boxes over the game's own Net-Z networking

It is written in plain Python 3 with only the standard library, and it learns from the game's own
requests.

Version 0.2 adds **change detection**: the server notices when the game install, the installed mods or
the players' client versions change, then reports or warns about it (see
[docs/change-detection.md](docs/change-detection.md)).

The game side is a CrabeLoader mod in a separate folder, [`../ServerDI_Client`](../ServerDI_Client).

> ServerDI never grants paid content (entitlements always answer "nothing to redeem") and never writes
> into the game's `assets/` folder. The patch tools only write a **copy** of the executable and a
> **mod** copy of the menus archive.

## Quick start

1. Install **CrabeLoader** in the game folder.
2. Install the client mod and the patched exe copy:

   ```
   pip install cryptography
   python install_crabe_mod.py --game "<game folder>" --base http://127.0.0.1:8085
   ```

   This needs `tool/ditool.py`; see [tool/README.md](tool/README.md).
3. Start the server and leave the window open:

   ```
   python di_server.py --game "<game folder>"
   ```

   You can also use `start_server.bat`. `--game` is optional and turns on the game-folder watcher.
4. With Steam open, start `DisneyInfinity3_local.exe` from the game folder.

### Playing with other PCs

- Run `start_server_lan.bat` on the server PC. It listens on every network on TCP port 85 and accepts
  sign-ins.
- On every player's PC, run `install_crabe_mod.py --base http://<server address>:85`. The url can be
  22 characters at most.
- To host a Toy Box over the internet, open the Net-Z UDP port (default 9104) on the host's router.
  A VPN such as a virtual LAN also works.
- If somebody also plays on the server PC, write that PC's address (as the other players see it) in
  `public_ip.txt`, or pass `--public-ip`.

## Command line

```
python di_server.py [options]
  --host 127.0.0.1        interface to listen on (0.0.0.0 = every network)
  --port 8085             TCP port
  --public URL            fixed base url given to the game (default: the Host the game used)
  --public-ip IP          address announced to players joining a Toy Box hosted on this PC
  --data DIR              data folder (default: ./data)
  --name Player           display name of the first account
  --country US            country reported at sign-in
  --allow-login           accept Disney ID sign-ins (see "Known limits")
  --game DIR              watch this game folder for changes (read only)
  --watch-interval 30     seconds between two checks of --game (0 = only at start-up)
  --client-dll FILE       reference dinet_bk.dll (default: ../ServerDI_Client/dinet_online/dinet_bk.dll)
  --tail [N]              print the last N logged requests and exit
  --changes [N]           print the last N detected changes and exit
```

## What the server answers

| Service | Behaviour |
|---|---|
| `config` | Service directory. Every service url points back to this server. |
| Disney ID (`coregames/did`) | Accepts any Steam user or user name, creates a local account in `data/users.json`, and returns tokens. Refused unless `--allow-login` is given. |
| Cloud saves (`infinity/save`) | Global save and per-playset sets/slots, one JSON file per account in `data/saves/`. |
| Profile, Toy Box (UGC), leaderboards, prestige… | Generic store: what the game PUTs or POSTs to a path, a later GET on the same path returns. 404 until something is stored. |
| Telemetry, activity stream, sessions, reports | Accepted and discarded. |
| Moderation | Every text is accepted. |
| Entitlement, MagicBand | "Nothing here" (404). |
| CDN (posters, icons) | Files from `data/cdn/`, if you add any. |
| Lobby (`/dinet/v1/...`) | Hosts, presence, friends, invites, join requests and text chat for the client helper. |
| `/_status`, `/_changes` | Diagnostics: players online, warnings, watched game state, change events. |

The full HTTP reference is in [docs/http-api.md](docs/http-api.md).

Every request is logged to `data/logs/requests.jsonl`. `python di_server.py --tail 50` shows the last
50. Lobby polling that succeeds isn't logged.

## Repository layout

| Path | What it is |
|---|---|
| `di_server.py` | The HTTP server: services, lobby, diagnostics. |
| `gamewatch.py` | Change detection: clients, routes, game folder. |
| `patch_exe.py` | Writes `DisneyInfinity3_local.exe`, a copy of the game whose service urls point to the server. |
| `patch_ui.py` | Rebuilds the menus archive with the online menus enabled (Lua 5.1 bytecode patcher). |
| `install_crabe_mod.py` | One-step install of the client mod (uses the two tools above). |
| `read_zones.py` | Reads the level-id table from a running game, and generates `dinet/zones_vanilla.h`. |
| `compile_ui_lua.py` | Compiles `dinet/ui_funcs.lua` to the game's bytecode format (needs `lupa`). |
| `dinet/` | Source code of the client helper `dinet_bk.dll` (C, Visual Studio 2022 x86) and its Lua parts. |
| `tool/` | Place for `ditool.py`, the container tool `patch_ui.py` uses. |
| `tests/` | `python -m unittest discover -s tests -v` |
| `docs/` | [modules.md](docs/modules.md), [http-api.md](docs/http-api.md), [change-detection.md](docs/change-detection.md). |

## Known limits

- **Signing in on the Gold Edition.** Every Disney ID sign-in runs the edition's one-time "save
  migration": it downloads the cloud saves, replaces the local ones and then signs out. The server
  refuses sign-ins by default to protect your saves. The client mod's `block_signout` and `protect`
  options keep the session and restore what the migration deletes. Storing Toy Boxes in the cloud
  still doesn't work.
- Only **Steam build 1522802** is supported by the exe code patch and the client helper. The change
  detector warns when the exe becomes a different build.
- Matchmaking with strangers (the old NAT negotiation service) isn't implemented. Players join each
  other through the lobby.

## Tests

```
python -m unittest discover -s tests -v
```

The tests start the server on a free port with a temporary data folder and a fake game folder. You
don't need the game to run them.
