# HTTP API

Everything is plain HTTP/1.1. Every response closes the connection. JSON bodies are UTF-8. The game
treats a key named `code` anywhere in a JSON reply as an error, so successful replies never contain one.

## Game services

The game first asks for the service directory and then uses the urls it gets. Each url points back to
this server, either at the `Host` the game used or at `--public`.

### `GET /coregames/config/v1/infinity3/steam/`

Returns `{"url_<service>": "<base><path>", ...}` for every service. The path for each service is in
`CONFIG_PATHS` in `di_server.py`. The reply also carries `url_radio_disney: "INACTIVE"`,
`domain_cg_natneg` and `big_endians`.

### Disney ID: `/coregames/did/v3/...`

| Call | Answer |
|---|---|
| `POST authenticate…`, `create`, `update`, empty action | Identity reply: `access_token`, `refresh_token`, `swid`, `displayName`, `username`, names, `email`, `date_of_birth`, `ageBand`, `countryCodeDetected`… The account is chosen by Steam assertion (`assertion_platform:assertion_id`), by user name, by refresh token or by bearer token, and is created on first use. `update` also copies the names and email from the request. |
| same, without `--allow-login` | `{"code": "100", "name": "SECURITY.PERMISSION_LOGIN_DENIED", ...}` (sign-in refused) |
| `check` | `{"available": true}` |
| `recovery`, `unlink` | `{}` |
| anything else | Neutral compliance / legal document reply |

### Cloud saves: `/infinity/save/v1/steam/...`

The account is the owner of the `Authorization: BEARER <token>` header. Its data is stored in
`data/saves/<swid>.json` as `{"global": blob, "playsets": {name: {set: {header, body, slots: {slot: {header, body, image}}}}}}`.

| Call | Effect |
|---|---|
| `POST`/`PUT` with `{"global": "..."}` | Stores the global save. |
| `POST`/`PUT` with `{"playset": {"name", "set": {"id", "header", "body", "slot": {"id", "header", "body", "image"}}}}` | Stores a playset set and slot. |
| `GET global` | `{"global": ...}`, or `{}` |
| `GET playset/<name>?set=<id>&slot=<id>` | `{name, id, header, body, slots}`, or `{}` |
| `DELETE playset/<name>` | Removes that playset. |
| `DELETE` (root, `delete`, `all`) | Removes everything. |

### Other services

| Path prefix | Answer |
|---|---|
| `/infinity/entitlement/` | 404: nothing to redeem (no paid content is ever granted). |
| `/infinity/magicband/` | 404 |
| `/infinity/moderation/` | `{"contentAction": "accept"}` |
| `/datatech/`, `/disneynetwork/activitystream/`, `/gxtools/`, `/coregames/sessions/` | 200 `{}` (accepted, discarded) |
| `/infinity3cdn/<file>` | File from `data/cdn/`, or 404. |
| `/`, `/pc-shop…` | A small HTML page (the shop no longer exists). |
| `GET /infinity/ugc/…/private/…` | The stored list, or an empty page `{"items": [], ...}`. |
| anything else | **Generic store**: `PUT`/`POST`/`PATCH` stores the body (and its content type) under the path, `GET`/`HEAD` returns it (404 if missing), `DELETE` removes it. Files are in `data/store/`. |

## Lobby for the client helper: `/dinet/v1/...`

Form-encoded bodies, `text/plain` tab-separated replies, one record per line. Names are cut to 40
characters.

| Call | Purpose |
|---|---|
| `POST host` `name, port, private_ip, world, zones` | This player hosts a Toy Box. Refreshed every 30 s, expires after 120 s. |
| `DELETE host?name=` | Stops hosting. |
| `GET hosts` | `name  ip  private_ip  port` |
| `POST presence` `name, private_ip, avatar, avatar_level, zones` | "I am online". Sent every 10 s, expires after 35 s. |
| `GET players` | `name  hosting(0/1)  ip  private_ip  port  world  character  character_level  zones` |
| `POST invite` / `DELETE invite` `from, to` | Invite a player to the Toy Box you host, or withdraw / decline the invite (expires after 10 min). |
| `GET invites?name=` | Invites for `name`, as player lines. |
| `POST request` / `DELETE request` `from, to` | Ask a host to let you join, or remove that request (expires after 10 min). |
| `GET requests?name=` | Join requests for the host `name`, as player lines. |

`zones` is the level-id fingerprint `<8 hex digits>-<number of levels added by mods>`. Players whose
first part differs number the levels differently. The server prints a warning and lists it under
`/_status` warnings.

### Text chat

| Call | Purpose |
|---|---|
| `POST chat` `name, tag, text` | Say something. `tag` is the sender's in-game gamer tag, which the other games use to find the speaker's avatar. Control characters become spaces and the text is cut to 120 characters. Replies with the message id. |
| `GET chat?name=&since=<id>` | First line `#<last id>`, then `id  name  tag  text` for every message newer than `<id>` that `name` didn't send. If `since` is missing or negative, only the `#<last id>` line comes back, so a game that just started doesn't replay old messages. |

The server keeps the last 200 messages in memory, prints each one on the console, and appends it to
`data/logs/chat.jsonl`. Every game connected to the server shares one chat.

`ip` is the address the server sees. For a player on the server PC itself, it's `--public-ip` or the
LAN address instead.

### Server directory (the in-game list, key F8)

The client's F8 list is switched off for now (`SERVER_LIST = false` in the client's `main.lua`); these
calls stay so it can come back in a future update.

| Call | Purpose |
|---|---|
| `GET hello` | `name  players_online  server_version`. Cheap: the client pings every listed server with it (2 s timeout). |
| `GET servers` | `name  url  description`, one server per line. This server comes first (`--server-name`, `--server-description`, its public url), then the servers in `<data>/servers.json`. |

`servers.json` is optional and read on every request, so servers can be added while the server runs:

```json
[{"name": "Friend's server", "url": "http://203.0.113.7:85", "description": "Europe"}]
```

Entries without an `http://` url, duplicates of an earlier url, and tabs or line breaks in the text are
dropped. The game can only use urls of 22 characters at most (the room in the exe copy); longer ones are
listed but can't be picked.

## Diagnostics

### `GET /_status`

```json
{
  "server": "0.2",
  "users": 3,
  "url": "http://127.0.0.1:8085",
  "players": [{"name": "...", "character": "...", "level": 12, "hosting": false, "world": "", "zones": "..."}],
  "warnings": ["bob runs an outdated client helper (v1 < v2)"],
  "watch": {
    "game": {"folder": "...", "build": "Steam build 1522802", "crabeloader": true,
             "mods": {"dinet_online": "1.0.0"}, "checked": "2026-10-15T10:30:00"},
    "clients": {"ana": {"version": 2, "zones": "AAAAAAAA-0", "last": "..."}},
    "last_change": 7
  }
}
```

`watch.game` is `null` when the server runs without `--game`. `warnings` covers the players online
now and the watched game folder.

### `GET /_changes?since=<id>`

`{"events": [...]}`: the change events with an id greater than `since` (oldest first, at most the last
500 of this run). The event format is in [change-detection.md](change-detection.md). Poll it with the
last id you received to get only the new events.
