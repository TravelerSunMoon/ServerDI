# Modules

Every Python function also has a docstring. This page explains how the pieces fit together.

## `di_server.py`: the server

- **`Store`**: persistent data in `--data`:
  - `users.json` holds the accounts (`user`, `user_by_token`, `save_users`);
  - `store/` is the generic path → body store, with one `.bin` file and one `.type` file per request
    path (`get`, `put`, `delete`);
  - `logs/requests.jsonl` is the request log (`log`).
  
  All access goes through one re-entrant lock, and files are written to a temporary name and then
  renamed, so a crash never leaves half a file.
- **`Handler`**: one instance per HTTP request. `_handle` reads the body (plain or chunked), calls
  `route`, feeds change detection (`watch.request`), logs the request and prints one console line.
  `route` dispatches by path prefix:
  - `h_config`: the service directory;
  - `h_did`: Disney ID;
  - `h_save`: cloud saves;
  - `h_moderation`, `h_cdn`;
  - `h_lobby` and `h_social`: hosts, presence, invites, join requests;
  - `h_chat`: text chat (last 200 messages in memory, `data/logs/chat.jsonl`);
  - `/_status` and `/_changes`;
  - anything else goes to `h_store`.
- **`Server`**: a `ThreadingHTTPServer`. `main()` attaches the shared state to it: `store`, `watch`,
  `lobby`, `presence`, `invites`, `requests`, plus the options.
- **`tail`, `tail_changes`**: print the end of the request log or the change log.
- **Helpers**:
  - `lan_ip` finds this PC's LAN address without sending a packet;
  - `zones_arg` validates a level-id fingerprint;
  - `avatar_text`, `now_iso`.

Presence, hosts, invites and requests live in memory and expire on their own (35 s, 120 s, 10 min),
so a crashed game disappears from the lists by itself.

## `gamewatch.py`: change detection

See [change-detection.md](change-detection.md). It has these classes:

- `ChangeLog` stores, prints and appends events.
- `ClientTracker` tracks each player's helper version and level-id fingerprint.
- `EndpointTracker` notices new routes.
- `GameWatcher` takes snapshots of the game folder and compares them.
- `Watch` is the facade the server uses, and it persists `watch_state.json`.

## `patch_exe.py`: the game copy

Writes `DisneyInfinity3_local.exe` and never modifies `DisneyInfinity3.exe`.

- `patch_bytes` rewrites every `https://api.disney.com…` and `https://cdninfinity-a.akamaihd.net…` string
  in place to `--base` (plain http). The new url can't be longer than the old prefix, so it has
  22 characters at most.
- `patch_code` applies the code patches known for the supported build. The original bytes are checked
  first. Today there is one patch: the local copy accepts the server's responses without the old
  service's signature, which the server can't produce.
  `--no-signature-bypass` keeps the check.
- `--dinet` (an older install method, without CrabeLoader): `patch_import` renames the copy's
  `bink2w32.dll` import to `dinet_bk.dll`, and the helper forwards every Bink export to the real DLL.
  `write_dinet_ini` creates or updates `di_net.ini` and keeps the player's own settings.
- It also writes `steam_appid.txt` (541670), so Steam doesn't relaunch the original exe, and
  `di_local_patch.txt`, a list of what was changed.

## `patch_ui.py`: the online menus

The Gold Edition hides its online menus behind the Lua global `UI_IsTOGOOfflineGame()`. This tool
unpacks the menus archive with `tool/ditool.py`, edits the compiled Lua 5.1 scripts directly at the
bytecode level, repacks the archive and verifies it.

- `luac_parse` / `luac_write` read and write the game's big-endian Lua 5.1 bytecode.
- `inject_override` + `redirect_global` make each script that asks `UI_IsTOGOOfflineGame` use a local
  copy that returns `false`. Nothing else changes: the store and purchase checks are untouched.
- Options:
  - `--login-user` (`inject_did_autologin`): the Disney ID screen presses "Log In" by itself with that
    user name;
  - `--friends-list` (`inject_ui_funcs`): embeds the compiled `dinet/ui_funcs.luac` so the friends
    and invites screens list the server's players;
  - `--mp-buttons`, `--friends` (experimental), `--autologin` (not useful on the Gold Edition).
- `--out FILE` writes the result somewhere else, such as the CrabeLoader mod folder, and leaves the
  game's file untouched. `--restore` puts back the original kept as `presentation.zip.orig` (older
  in-place installs only).

## `install_crabe_mod.py`: one-step client install

Runs `patch_exe.py` (without `--dinet`, so the copy still loads CrabeLoader) and copies the client
mod from `../ServerDI_Client/dinet_online` (or `--client`) to `<game>/mods/dinet_online`. It then
writes `di_net.ini` and builds the mod's `presentation/presentation.zip` with `patch_ui.py --out`.
`--remove` uninstalls.

## `read_zones.py`: level ids

The game numbers its levels when it starts, in an order that changes when mods add levels. The client
helper gives every unmodified level its vanilla id, taken from `dinet/zones_vanilla.h`.

- `--pid <process id>` reads the id table of a running game (read only, through
  `ReadProcessMemory`).
- `--header` regenerates `zones_vanilla.h` from `zones_vanilla.txt`, a dump of the unmodified game.

## `compile_ui_lua.py`: menu helper bytecode

Compiles `dinet/ui_funcs.lua` with `lupa` (Lua 5.1) and converts the dump to the game's format
(big-endian, 4-byte `size_t`). The result is `dinet/ui_funcs.luac`, which `patch_ui.py` embeds. You
only need it after editing `ui_funcs.lua`.

## `dinet/`: client helper source

| File | Purpose |
|---|---|
| `dinet.c` | `dinet_bk.dll`: sign-in hooks, save protection, Net-Z host/join, lobby polling, the `#dinet:` bridge for the menus, canonical level ids, and the text chat (key-to-text with the player's keyboard layout, a thread that sends and fetches messages). |
| `bink_forwards.h` | Export forwards to `bink2w32.dll`, for the `--dinet` import method. |
| `build.bat` | Build with Visual Studio 2022 (x86, `/MT`). |
| `ui_funcs.lua` / `.luac` | Friends and invites list code embedded in the menu scripts. |
| `dinet.lua` | Experimental screens script (`patch_ui.py --friends`). |
| `zones_vanilla.txt` / `.h` | Level ids of the unmodified game. |

The CrabeLoader mod that loads the helper is documented in
[`../ServerDI_Client/README.md`](../../ServerDI_Client/README.md).
