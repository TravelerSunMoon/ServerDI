# -*- coding: utf-8 -*-
"""
Disney Infinity 3.0 Gold Edition (PC / Steam)  --  local replacement server
==========================================================================

Replaces the shut-down Disney online services (api.disney.com) on your own PC.
Only the Python standard library is used.

    python di_server.py                      (listens on 127.0.0.1:8085)
    python di_server.py --port 8085 --name "Player"

The game must talk to this server instead of api.disney.com: run patch_exe.py
once (it writes a patched COPY of the executable, the original is not touched).

With --game <folder> it also watches the game install for changes (see gamewatch.py):

    python di_server.py --game "<path to Disney Infinity 3.0 Gold Edition>" --watch-interval 30

How the game is served (as observed from the game's own requests):
  1. GET  coregames/config/v1/infinity3/steam/   -> {"url_<service>": "<url>"} for every
     service; here every url points back to this server over plain http.
  2. POST coregames/did/v3/authenticate           -> Disney ID login. Any Steam user / any
     username+password is accepted; the reply carries access_token, refresh_token, swid...
     (a key named "code" anywhere in a reply means "error" to the game).
  3. Profile, save games, Toy Box (UGC), leaderboards, prestige... are kept in a generic
     REST store: what the game PUTs/POSTs to a path is returned by GET on that path,
     404 when nothing was stored (the game then creates it).
  4. Telemetry / logs (datatech, activity stream, sessions/track, reports) are accepted.
  5. Entitlement (bought Sparks / codes) always answers "nothing to redeem" (404):
     this server does not grant paid content.

  6. Lobby, friends, invites and join requests for the client helper dinet_bk.dll live under
     /dinet/v1/ (see h_lobby / h_social).
  7. Diagnostics: GET /_status (players online, warnings, watched game state) and
     GET /_changes?since=<id> (change events detected by gamewatch.py).

Everything the game sends is written to data/logs/requests.jsonl so the answers can be
refined (python di_server.py --tail shows the last requests).
"""

import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gamewatch  # noqa: E402

VERSION = "0.2"
HERE = os.path.dirname(os.path.abspath(__file__))
# client helper shipped with the client mod, used to spot an outdated copy in the watched game folder
DEFAULT_CLIENT_DLL = os.path.join(os.path.dirname(HERE), "ServerDI_Client", "dinet_online", "dinet_bk.dll")

# config key -> path on this server (same paths as the original Steam production services)
CONFIG_PATHS = {
    "url_inf_save": "/infinity/save/v1/steam/",
    "url_inf_ticker": "/infinity/ticker/v1/steam/",
    "url_inf_ugc": "/infinity/ugc/v2/steam/",
    "url_inf_profile": "/infinity/profile/v2/steam/",
    "url_inf_leaderboard": "/infinity/leaderboard/v1/steam/",
    "url_inf_entitlement": "/infinity/entitlement/v1/steam",
    "url_web_disney_shop": "/pc-shop",
    "url_cg_friends": "/coregames/friends/v1/steam",
    "url_cg_matchmaking": "/coregames/matchmaking/v1",
    "url_social_report_player": "/gxtools/report/v2/queue",
    "url_inf_moderation": "/infinity/moderation/v1",
    "url_inf_prestige": "/infinity/prestige/v1",
    "url_inf_videolist": "/infinity/videolist/v1/",
    "url_inf_magic_band": "/infinity/magicband/v1/",
    "url_inf_access": "/infinity/access/v1",
    "url_activity_stream_links": "/",
    "url_cg_did_auth": "/coregames/did/v3/",
    "url_cg_p2p_session": "/coregames/sessions/v1/",
    "url_activity_stream_social": "/disneynetwork/activitystream/v2/",
    "url_cg_bi": "/datatech/log/v1/batch",
    "url_inf_ums": "/UMFrontier/",
    "url_prestige_icons": "/infinity3cdn/prestige",
    "url_cdn_posters": "/infinity3cdn/posters",
    "url_ums_icons": "/infinity3cdn/ums",
    "url_activity_stream_icons": "/infinity3cdn/as",
}
CONFIG_LITERALS = {"url_radio_disney": "INACTIVE", "domain_cg_natneg": "disney.com"}

# accepted and logged, nothing stored
SINK_PREFIXES = ("/datatech/", "/disneynetwork/activitystream/", "/gxtools/", "/coregames/sessions/")
# polled every few seconds by dinet_bk.dll: not printed / logged when they succeed
POLL_PREFIXES = ("/dinet/v1/host", "/dinet/v1/presence", "/dinet/v1/players", "/dinet/v1/invites",
                 "/dinet/v1/requests")


def lan_ip():
    """This PC's address on the local network (no packet is sent)."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def avatar_text(avatar, level):
    """'Hulk (level 12)' for the console."""
    return "%s (level %s)" % (avatar, level) if level else avatar


def zones_arg(form):
    """'<8 hex>-<n>' level-id fingerprint sent by dinet_bk, '' when missing or malformed"""
    z = (form.get("zones") or [""])[0].strip()
    return z if re.match(r"^[0-9A-F]{8}-\d{1,4}$", z) else ""


def now_iso():
    """Current UTC time as 'YYYY-MM-DDTHH:MM:SSZ'."""
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z"


class Store:
    """Persistent data: users and a generic path -> body store (one file per path)."""

    def __init__(self, root):
        """Creates the data folders and loads users.json."""
        self.root = root
        self.lock = threading.RLock()
        for d in ("store", "logs", "cdn"):
            os.makedirs(os.path.join(root, d), exist_ok=True)
        self.users_path = os.path.join(root, "users.json")
        self.users = {}
        if os.path.exists(self.users_path):
            try:
                with open(self.users_path, encoding="utf-8") as f:
                    self.users = json.load(f)
            except (OSError, ValueError):
                self.users = {}

    # ---- users
    def save_users(self):
        """Writes users.json atomically."""
        tmp = self.users_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.users, f, indent=1)
        os.replace(tmp, self.users_path)

    def user(self, key, default_name):
        """Returns the account for `key` (Steam id, user name...), creating it on first use."""
        with self.lock:
            u = self.users.get(key)
            if u is None:
                n = len(self.users) + 1
                u = self.users[key] = dict(
                    swid="{%s}" % str(uuid.uuid4()).upper(),
                    displayName=default_name if n == 1 else "%s%d" % (default_name, n),
                    username=re.sub(r"\W+", "", default_name).lower() + str(n),
                    first_name=default_name, last_name="Local",
                    email="player%d@localhost" % n, date_of_birth="1990-01-01",
                    created=now_iso())
                self.save_users()
            return u

    def user_by_token(self, token):
        """(key, account) owning an access or refresh token, or (None, None)."""
        with self.lock:
            for k, u in self.users.items():
                if token and token in (u.get("access_token"), u.get("refresh_token")):
                    return k, u
        return None, None

    # ---- generic store
    def _file(self, path):
        """File of the generic store that holds the body of a request path."""
        p = path.strip("/")
        parts = [re.sub(r"[^A-Za-z0-9._{}\-]", "_", s) or "_" for s in p.split("/")] if p else ["_root"]
        if path.endswith("/"):
            parts.append("_index")
        return os.path.join(self.root, "store", *parts) + ".bin"

    def get(self, path):
        """(body, content type) stored for a path, or (None, None)."""
        fp = self._file(path)
        with self.lock:
            if not os.path.exists(fp):
                return None, None
            with open(fp, "rb") as f:
                body = f.read()
            ctype = "application/json"
            if os.path.exists(fp + ".type"):
                with open(fp + ".type", encoding="utf-8") as f:
                    ctype = f.read().strip() or ctype
            return body, ctype

    def put(self, path, body, ctype):
        """Stores the body and content type of a path (atomic write)."""
        fp = self._file(path)
        with self.lock:
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            with open(fp + ".tmp", "wb") as f:
                f.write(body)
            os.replace(fp + ".tmp", fp)
            with open(fp + ".type", "w", encoding="utf-8") as f:
                f.write(ctype or "application/octet-stream")

    def delete(self, path):
        """Deletes what is stored for a path; True when something existed."""
        fp = self._file(path)
        with self.lock:
            existed = os.path.exists(fp)
            for x in (fp, fp + ".type"):
                if os.path.exists(x):
                    os.remove(x)
            return existed

    def log(self, entry):
        """Appends one request record to data/logs/requests.jsonl."""
        with self.lock:
            with open(os.path.join(self.root, "logs", "requests.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")


class Handler(BaseHTTPRequestHandler):
    """HTTP request handler: one instance per request; route() picks the service."""
    server_version = "DILocalServer/" + VERSION
    protocol_version = "HTTP/1.1"

    # quiet default logging (we log our own way)
    def log_message(self, fmt, *args):
        """Silences the default per-request logging (_handle prints its own line)."""
        pass

    # ------------------------------------------------------------------ plumbing
    def _read_body(self):
        """Reads the request body (Content-Length or chunked transfer encoding)."""
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            out = bytearray()
            while True:
                line = self.rfile.readline().strip()
                n = int(line.split(b";")[0] or b"0", 16)
                if n == 0:
                    self.rfile.readline()
                    break
                out += self.rfile.read(n)
                self.rfile.readline()
            return bytes(out)
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n > 0 else b""

    def _send(self, status, body=b"", ctype="application/json", headers=None):
        """Sends a complete response; dict/list bodies are sent as JSON."""
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)                     # adds Date (used by the game as server time)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True
        self._status, self._out_len = status, len(body)

    def _json_body(self):
        """The request body parsed as JSON, or {} when it is not JSON."""
        try:
            return json.loads(self.body.decode("utf-8")) if self.body else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def base_url(self):
        # Without --public, answer with the address this client used (Host header), so the
        # host PC (127.0.0.1) and a friend's PC (LAN / public IP) both get reachable urls.
        """Base url the game must use to reach this server (Host header, or --public)."""
        if not self.server.public_fixed:
            host = (self.headers.get("Host") or "").strip()
            if re.match(r"^[A-Za-z0-9.\-]+(:\d+)?$", host):
                return "http://" + host
        return self.server.public_url

    def _handle(self):
        """Entry point of every HTTP method: routes, feeds change detection, logs and prints."""
        t0 = time.time()
        u = urlsplit(self.path)
        self.url_path, self.query = u.path, parse_qs(u.query)
        self.body = self._read_body() if self.command in ("POST", "PUT", "PATCH", "DELETE") else b""
        self._status, self._out_len = 0, 0
        try:
            self.route()
        except Exception as ex:                       # never kill the server
            self._send(500, {"message": "server error: %s" % ex})
            print("  !! %s %s -> %r" % (self.command, self.path, ex))
        try:                                          # change detection: new client software / new routes
            self.server.watch.request(self.command, self.url_path, self._status,
                                      self.headers.get("User-Agent", ""))
        except Exception as ex:
            print("  !! change detection: %r" % ex)
        if self._status == 200 and self.url_path.startswith(POLL_PREFIXES):
            return                                    # periodic lobby traffic: its events are printed
        body_txt = None
        if self.body:
            try:
                body_txt = self.body.decode("utf-8")
            except UnicodeDecodeError:
                body_txt = "base64:" + base64.b64encode(self.body[:65536]).decode()
            if len(body_txt) > 65536:
                body_txt = body_txt[:65536] + "...(truncated)"
        hdrs = {k: v for k, v in self.headers.items()}
        self.server.store.log(dict(t=now_iso(), method=self.command, path=self.path, headers=hdrs,
                                   body=body_txt, status=self._status, out=self._out_len,
                                   ms=int((time.time() - t0) * 1000)))
        print("%s %-6s %-70s -> %d (%d B in, %d B out)" % (time.strftime("%H:%M:%S"), self.command,
                                                          self.path[:70], self._status, len(self.body),
                                                          self._out_len))
        sys.stdout.flush()

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _handle

    # ------------------------------------------------------------------ routing
    def route(self):
        """Dispatches the request to the handler of its service (fallback: generic store)."""
        p = self.url_path
        lp = p.lower()
        if lp.startswith("/coregames/config/"):
            return self.h_config()
        if lp.startswith("/coregames/did/"):
            return self.h_did(lp[len("/coregames/did/"):].split("/", 1)[-1] if "/" in lp[len("/coregames/did/"):] else "")
        if lp.startswith("/infinity/entitlement/"):
            return self._send(404, {"message": "No entitlements on the local server"})
        if lp.startswith("/infinity/moderation/"):
            return self.h_moderation()
        if lp.startswith("/infinity/magicband/"):
            return self._send(404, {"message": "No MagicBand"})
        if lp.startswith(SINK_PREFIXES):
            return self._send(200, {})
        if lp.startswith("/infinity3cdn/"):
            return self.h_cdn()
        if lp in ("/", "/pc-shop") or lp.startswith("/pc-shop"):
            return self._send(200, "<html><body><h1>Disney Infinity local server</h1>"
                                   "<p>The Disney online shop no longer exists.</p></body></html>", "text/html")
        if lp.startswith("/infinity/save/"):
            return self.h_save()
        if lp.startswith("/infinity/ugc/") and self.command == "GET" and "/private/" in lp:
            body, ctype = self.server.store.get(p)
            if body is None:
                return self._send(200, {"items": [], "page": 1, "page_size": 1000, "page_count": 1,
                                        "total_items": 0})
            return self._send(200, body, ctype)
        if lp.startswith("/dinet/v1/"):
            return self.h_lobby(lp[len("/dinet/v1/"):])
        if lp == "/_status":
            now = time.time()
            online = [dict(name=n, character=p.get("avatar", ""), level=int(p["level"]) if p.get("level") else None,
                           hosting=n in self.server.lobby, world=self.server.lobby.get(n, {}).get("world", ""),
                           zones=p.get("zones", ""))
                      for n, p in sorted(self.server.presence.items(), key=lambda i: i[0].lower())
                      if now - p["seen"] <= self.PRESENCE_TTL]
            watch = self.server.watch
            return self._send(200, dict(server=VERSION, users=len(self.server.store.users),
                                        url=self.base_url(), players=online,
                                        warnings=watch.warnings({p["name"] for p in online}),
                                        watch=watch.summary()))
        if lp == "/_changes":
            try:
                since = int((self.query.get("since") or ["0"])[0])
            except ValueError:
                since = 0
            return self._send(200, dict(events=self.server.watch.log.since(since)))
        return self.h_store()

    # ------------------------------------------------------------------ lobby (dinet_bk.dll)
    # The Gold Edition has no friends/matchmaking code left, so the helper DLL asks here.
    #   POST   /dinet/v1/host   name=&port=&private_ip=   a player hosting a Toy Box (refresh every 30 s)
    #   DELETE /dinet/v1/host?name=                       stop hosting
    #   GET    /dinet/v1/hosts                            one host per line: name TAB ip TAB private_ip TAB port
    HOST_TTL = 120

    def _peer_ip(self):
        """Address other players should use to reach this client (LAN/public if it is local)."""
        ip = self.client_address[0]
        if ip.startswith("::ffff:"):
            ip = ip[7:]
        if ip.startswith("127.") or ip == "::1":
            # the host plays on the server PC: tell the others the LAN / public address instead
            ip = self.server.public_ip or lan_ip()
        return ip

    def h_lobby(self, action):
        """Lobby of hosted Toy Boxes for dinet_bk.dll (/dinet/v1/host, /dinet/v1/hosts)."""
        hosts = self.server.lobby
        now = time.time()
        for k in [k for k, h in hosts.items() if now - h["seen"] > self.HOST_TTL]:
            del hosts[k]
        if action == "host" and self.command == "POST":
            f = parse_qs(self.body.decode("utf-8", "replace"))
            name = (f.get("name") or [""])[0].strip()[:40]
            try:
                port = int((f.get("port") or ["0"])[0])
            except ValueError:
                port = 0
            priv = (f.get("private_ip") or [""])[0].strip()
            world = re.sub(r"[^A-Za-z0-9_]", "", (f.get("world") or [""])[0])[:60]   # level the host plays
            if not name or not 0 < port < 65536 or not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", priv):
                return self._send(400, "bad host announcement\n", "text/plain")
            first = name not in hosts or hosts[name].get("world") != world
            zones = zones_arg(f)
            hosts[name] = dict(name=name, ip=self._peer_ip(), private_ip=priv, port=port, world=world,
                               zones=zones or hosts.get(name, {}).get("zones", ""), seen=now)
            if first:
                print("  lobby: %s hosts at %s / %s : %d  (level %s)" % (name, hosts[name]["ip"], priv, port,
                                                                       world or "?"))
            self.server.watch.player(name, self.headers.get("User-Agent", ""), zones)
            return self._send(200, "ok\n", "text/plain")
        if action == "host" and self.command == "DELETE":
            name = (self.query.get("name") or [""])[0]
            if hosts.pop(name, None):
                print("  lobby: %s stopped hosting" % name)
            return self._send(200, "ok\n", "text/plain")
        if action == "hosts":
            lines = ["%s\t%s\t%s\t%d" % (h["name"], h["ip"], h["private_ip"], h["port"])
                     for h in sorted(hosts.values(), key=lambda h: h["name"].lower())]
            return self._send(200, "".join(l + "\n" for l in lines), "text/plain")
        return self.h_social(action, hosts, now)

    # ------------------------------------------------------------------ friends / invites (dinet_bk.dll)
    # Every game with dinet_bk is everybody's "friend" while it is connected to this server.
    #   POST   /dinet/v1/presence  name=&private_ip=     every 10 s while the game runs
    #   GET    /dinet/v1/players                         name TAB hosting(0/1) TAB ip TAB private_ip TAB port
    #                                                    TAB world (the level a host plays; a guest must load
    #                                                    the same level or the host kicks it)
    #                                                    TAB character TAB character level (from the presence)
    #                                                    TAB zone ids: "<fingerprint>-<levels added by mods>"
    #                                                    (games with other fingerprints load other levels)
    #   POST   /dinet/v1/invite    from=&to=             invite a player to the Toy Box you host
    #   DELETE /dinet/v1/invite?from=&to=                withdraw / decline it
    #   GET    /dinet/v1/invites?name=                   from TAB hosting TAB ip TAB private_ip TAB port
    PRESENCE_TTL = 35
    INVITE_TTL = 600

    def _player_line(self, name, hosts):
        """One tab-separated line describing a player for /players, /invites, /requests."""
        p = self.server.presence.get(name, {})
        zones = p.get("zones") or hosts.get(name, {}).get("zones", "")
        who = "\t%s\t%s\t%s" % (p.get("avatar", ""), p.get("level", ""), zones)   # character, its level, zone ids
        h = hosts.get(name)
        if h:
            return "%s\t1\t%s\t%s\t%d\t%s%s" % (name, h["ip"], h["private_ip"], h["port"], h.get("world", ""), who)
        return "%s\t0\t%s\t%s\t0\t%s" % (name, p.get("ip", ""), p.get("private_ip", ""), who)

    def _check_zones(self, name, zones, presence):
        """console warning when this player's level ids differ from another online player's"""
        fp, _, mods = zones.partition("-")
        print("  friends: %s level ids %s%s" % (name, fp, " (%s level(s) added by mods)" % mods
                                                 if mods not in ("", "0") else ""))
        for other, p in presence.items():
            theirs = p.get("zones", "")
            if other != name and theirs and theirs.partition("-")[0] != fp:
                print("  !! %s and %s number the levels differently: joining each other loads the wrong level "
                      "(update the mod package on both PCs)" % (name, other))

    def h_social(self, action, hosts, now):
        """Presence, players list, invites and join requests (/dinet/v1/...)."""
        presence, invites = self.server.presence, self.server.invites
        for k in [k for k, p in presence.items() if now - p["seen"] > self.PRESENCE_TTL]:
            print("  friends: %s is offline" % k)
            del presence[k]
        for to in list(invites):
            invites[to] = {f: t for f, t in invites[to].items() if now - t < self.INVITE_TTL}
            if not invites[to]:
                del invites[to]
        form = parse_qs(self.body.decode("utf-8", "replace")) if self.command == "POST" else self.query
        arg = lambda k: (form.get(k) or [""])[0].strip()[:40]
        if action == "presence" and self.command == "POST":
            name, priv = arg("name"), arg("private_ip")
            if not name:
                return self._send(400, "name missing\n", "text/plain")
            # character played (told by the in-game HUD) and its level; older dinet_bk do not send them
            avatar = re.sub(r"[\t\r\n]", " ", (form.get("avatar") or [""])[0]).strip()[:60]
            level = re.sub(r"\D", "", arg("avatar_level"))[:4]
            old = presence.get(name)
            if old is None:
                print("  friends: %s is online%s" % (name, " as " + avatar_text(avatar, level) if avatar else ""))
            elif avatar and (avatar, level) != (old.get("avatar"), old.get("level")):
                print("  friends: %s plays as %s" % (name, avatar_text(avatar, level)))
            if not avatar and old:                    # keep the last known character
                avatar, level = old.get("avatar", ""), old.get("level", "")
            zones = zones_arg(form)
            if zones and zones != (old or {}).get("zones"):
                self._check_zones(name, zones, presence)
            presence[name] = dict(ip=self._peer_ip(), private_ip=priv, avatar=avatar, level=level,
                                  zones=zones or (old or {}).get("zones", ""), seen=now)
            self.server.watch.player(name, self.headers.get("User-Agent", ""), zones)
            return self._send(200, "ok\n", "text/plain")
        if action == "players":
            names = sorted(set(presence) | set(hosts), key=str.lower)
            return self._send(200, "".join(self._player_line(n, hosts) + "\n" for n in names), "text/plain")
        if action == "invite":
            frm, to = arg("from"), arg("to")
            if not frm or not to:
                return self._send(400, "from/to missing\n", "text/plain")
            if self.command == "POST":
                invites.setdefault(to.lower(), {})[frm] = now
                print("  friends: %s invites %s" % (frm, to))
            elif self.command == "DELETE":
                invites.get(to.lower(), {}).pop(frm, None)
            return self._send(200, "ok\n", "text/plain")
        if action == "invites":
            mine = invites.get(arg("name").lower(), {})
            lines = [self._player_line(f, hosts) for f in sorted(mine, key=lambda f: mine[f])]
            return self._send(200, "".join(l + "\n" for l in lines), "text/plain")
        # join requests: "from" asks "to" (who hosts a Toy Box) to let them in; accepting sends an invite
        requests = self.server.requests
        for to in list(requests):
            requests[to] = {f: t for f, t in requests[to].items() if now - t < self.INVITE_TTL}
            if not requests[to]:
                del requests[to]
        if action == "request":
            frm, to = arg("from"), arg("to")
            if not frm or not to:
                return self._send(400, "from/to missing\n", "text/plain")
            if self.command == "POST":
                requests.setdefault(to.lower(), {})[frm] = now
                print("  friends: %s asks to join %s" % (frm, to))
            elif self.command == "DELETE":
                requests.get(to.lower(), {}).pop(frm, None)
            return self._send(200, "ok\n", "text/plain")
        if action == "requests":
            mine = requests.get(arg("name").lower(), {})
            lines = [self._player_line(f, hosts) for f in sorted(mine, key=lambda f: mine[f])]
            return self._send(200, "".join(l + "\n" for l in lines), "text/plain")
        return self._send(404, "unknown lobby call\n", "text/plain")

    # ------------------------------------------------------------------ services
    def h_config(self):
        """Service directory: every service url points back to this server."""
        base = self.base_url()
        cfg = {k: base + v for k, v in CONFIG_PATHS.items()}
        cfg.update(CONFIG_LITERALS)
        cfg["big_endians"] = ["x360", "ps3", "wiiu"]
        self._send(200, cfg)

    def _identity_reply(self, key, u):
        """New tokens for an account and the Disney ID login reply built from it."""
        u["access_token"] = "LOCAL-AT-" + hashlib.sha1((key + str(time.time())).encode()).hexdigest()
        u["refresh_token"] = "LOCAL-RT-" + hashlib.sha1((key + "r" + str(time.time())).encode()).hexdigest()
        u["last_login"] = now_iso()
        self.server.store.save_users()
        return dict(access_token=u["access_token"], refresh_token=u["refresh_token"], token_type="BEARER",
                    expires_in=86400, swid=u["swid"], displayName=u["displayName"], username=u["username"],
                    login_username=u["username"], first_name=u["first_name"], last_name=u["last_name"],
                    email=u["email"], date_of_birth=u["date_of_birth"], age="30", ageBand="ADULT",
                    countryCodeDetected=self.server.country, gender="0", proposedStatus="ACCEPTED")

    def h_did(self, action):
        """Disney ID: login, account creation/update, name checks and legal documents."""
        req = self._json_body()
        store = self.server.store
        action = action.strip("/")
        if self.command == "GET" and not action:
            return self._send(200, {})
        if (action.startswith("authenticate") or action == "create") and not self.server.allow_login:
            # Signing in makes the PC game load its saves from the cloud and replace the local ones;
            # the cloud responses need a signature this server cannot produce yet. Refuse until then.
            print("  login refused (start the server with --allow-login to accept it)")
            return self._send(200, {"error": {"errors": []}, "code": "100",
                                    "name": "SECURITY.PERMISSION_LOGIN_DENIED"})
        if action.startswith("authenticate") or action in ("create", "update", ""):
            # who is this?  Steam assertion id, username, or an existing token
            key = None
            if req.get("assertion_id"):
                key = "%s:%s" % (req.get("assertion_platform", "platform"), req["assertion_id"])
            elif req.get("username") or req.get("login"):
                key = "did:%s" % (req.get("username") or req.get("login")).lower()
            elif req.get("refresh_token"):
                key, _u = store.user_by_token(req["refresh_token"])
            if not key:
                auth = self.headers.get("Authorization", "")
                m = re.search(r"BEARER\s+(\S+)", auth, re.I)
                if m:
                    key, _u = store.user_by_token(m.group(1))
            key = key or "local:default"
            u = store.user(key, self.server.player_name)
            if action == "update":
                for k in ("displayName", "first_name", "last_name", "email", "username"):
                    if isinstance(req.get(k), str) and req[k]:
                        u[k] = req[k]
                prof = req.get("profile") if isinstance(req.get("profile"), dict) else {}
                for src, dst in (("displayName", "displayName"), ("firstName", "first_name"),
                                 ("lastName", "last_name"), ("email", "email")):
                    if isinstance(prof.get(src), str) and prof[src]:
                        u[dst] = prof[src]
            return self._send(200, self._identity_reply(key, u))
        if action == "check":
            return self._send(200, {"available": True})
        if action in ("recovery", "unlink"):
            return self._send(200, {})
        # compliance / legal documents and anything else: neutral answer
        return self._send(200, {
            "compliance": {"fields": {"profile": {"parentEmail": {"use": "OPTIONAL"}}},
                           "legalRequirements": {"CREATE": {"cookiePermissions": {
                               "TARGETING_ADVERTISING": {"permissionDefault": "OPT_IN"}}}}},
            "legal": {"documents": {}}, "child_privacy_policy": "false", "text_tags": {}})

    # ---- cloud saves: {"global": blob, "playsets": {name: {set: {"header","body","slots": {slot: {...}}}}}}
    def _save_user(self):
        """swid of the account that owns the request's bearer token ('default' if none)."""
        auth = self.headers.get("Authorization", "")
        m = re.search(r"BEARER\s+(\S+)", auth, re.I)
        key, u = self.server.store.user_by_token(m.group(1) if m else None)
        return (u or {}).get("swid", "default").strip("{}")

    def _save_load(self, swid):
        """Cloud save data of an account (global blob and playsets)."""
        fp = os.path.join(self.server.store.root, "saves", swid + ".json")
        try:
            with open(fp, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"global": None, "playsets": {}}

    def _save_store(self, swid, data):
        """Writes the cloud save data of an account atomically."""
        d = os.path.join(self.server.store.root, "saves")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, swid + ".json.tmp"), "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(os.path.join(d, swid + ".json.tmp"), os.path.join(d, swid + ".json"))

    def h_save(self):
        """Cloud saves: global save and per-playset sets/slots (GET, POST/PUT, DELETE)."""
        swid = self._save_user()
        data = self._save_load(swid)
        rest = self.url_path.split("/infinity/save/", 1)[1]
        rest = rest.split("/", 2)[2] if rest.count("/") >= 2 else ""      # after v1/steam/
        if self.command in ("POST", "PUT"):
            req = self._json_body()
            if isinstance(req.get("global"), str):
                data["global"] = req["global"]
            ps = req.get("playset")
            if isinstance(ps, dict) and ps.get("name"):
                st = ps.get("set") if isinstance(ps.get("set"), dict) else {}
                sets = data["playsets"].setdefault(str(ps["name"]).lower(), {})
                sd = sets.setdefault(str(st.get("id", 0)), {"slots": {}})
                for k in ("header", "body"):
                    if isinstance(st.get(k), str):
                        sd[k] = st[k]
                sl = st.get("slot") if isinstance(st.get("slot"), dict) else None
                if sl is not None:
                    so = sd["slots"].setdefault(str(sl.get("id", "a")), {})
                    for k in ("header", "body", "image"):
                        if isinstance(sl.get(k), str):
                            so[k] = sl[k]
            self._save_store(swid, data)
            return self._send(200, {})
        if self.command == "DELETE":
            parts = rest.strip("/").split("/")
            if len(parts) >= 2 and parts[0] == "playset":
                data["playsets"].pop(parts[1].lower(), None)
            elif rest.strip("/") in ("", "delete", "all"):
                data = {"global": None, "playsets": {}}
            self._save_store(swid, data)
            return self._send(200, {})
        # GET
        parts = rest.strip("/").split("/")
        if parts[0] == "global":
            return self._send(200, {"global": data["global"]} if data.get("global") else {})
        if parts[0] == "playset" and len(parts) >= 2:
            name = parts[1].lower()
            sset = (self.query.get("set") or ["0"])[0]
            slot = (self.query.get("slot") or ["a"])[0]
            sd = data["playsets"].get(name, {}).get(sset)
            if not sd:
                return self._send(200, {})
            out = {"name": name, "id": sset}
            for k in ("header", "body"):
                if sd.get(k):
                    out[k] = sd[k]
            so = sd.get("slots", {}).get(slot)
            if so:
                out["slots"] = dict(so, id=slot)
            return self._send(200, out)
        return self._send(200, {})

    def h_moderation(self):
        """Text moderation: every text is accepted."""
        self._send(200, {"contentAction": "accept"})

    def h_cdn(self):
        """Serves files from data/cdn (posters, icons); 404 when missing."""
        rel = self.url_path[len("/infinity3cdn/"):]
        fp = os.path.normpath(os.path.join(self.server.store.root, "cdn", rel))
        if fp.startswith(os.path.join(self.server.store.root, "cdn")) and os.path.isfile(fp):
            with open(fp, "rb") as f:
                ext = os.path.splitext(fp)[1].lower()
                ctype = {".png": "image/png", ".jpg": "image/jpeg", ".json": "application/json"}.get(ext,
                                                                                                "application/octet-stream")
                return self._send(200, f.read(), ctype)
        return self._send(404, {"message": "not on the local CDN"})

    def h_store(self):
        """Generic REST store: GET returns what PUT/POST stored on the same path."""
        st = self.server.store
        p = self.url_path
        if self.command in ("GET", "HEAD"):
            body, ctype = st.get(p)
            if body is None:
                return self._send(404, {"message": "not found"})
            return self._send(200, body, ctype)
        if self.command in ("PUT", "POST", "PATCH"):
            st.put(p, self.body, self.headers.get("Content-Type", "application/octet-stream"))
            return self._send(200, {})
        if self.command == "DELETE":
            st.delete(p)
            return self._send(200, {})
        return self._send(405, {"message": "method not allowed"})


class Server(ThreadingHTTPServer):
    """Threaded HTTP server; main() attaches store, watch, lobby, presence, invites and requests."""
    daemon_threads = True
    allow_reuse_address = False         # on Windows True lets two servers share the port


def tail(data_dir, n):
    """Prints the last n requests of data/logs/requests.jsonl (with the start of their body)."""
    fp = os.path.join(data_dir, "logs", "requests.jsonl")
    if not os.path.exists(fp):
        print("No requests logged yet (%s)" % fp)
        return
    with open(fp, encoding="utf-8") as f:
        lines = f.readlines()[-n:]
    for ln in lines:
        e = json.loads(ln)
        print("%s %-6s %s -> %s" % (e["t"], e["method"], e["path"], e["status"]))
        if e.get("body"):
            print("      body: %s" % e["body"][:300].replace("\n", " "))


def tail_changes(data_dir, n):
    """Prints the last n change events of data/logs/changes.jsonl."""
    fp = os.path.join(data_dir, "logs", "changes.jsonl")
    if not os.path.exists(fp):
        print("No changes detected yet (%s)" % fp)
        return
    with open(fp, encoding="utf-8") as f:
        lines = f.readlines()[-n:]
    for ln in lines:
        e = json.loads(ln)
        print("%s %-7s %-22s %s" % (e["t"], e["level"], e["kind"], e["message"]))


def main(argv=None):
    """Command line entry point: parses the options and runs the server until Ctrl+C."""
    ap = argparse.ArgumentParser(description="Disney Infinity 3.0 PC local replacement server")
    ap.add_argument("--host", default="127.0.0.1", help="interface to listen on (0.0.0.0 = whole LAN)")
    ap.add_argument("--port", type=int, default=8085)
    ap.add_argument("--public", default="", help="base url the game should use (default http://127.0.0.1:<port>)")
    ap.add_argument("--public-ip", default="",
                    help="this PC's internet address, given to players joining a game hosted on this PC "
                         "(default: its LAN address, enough for players on the same network)")
    ap.add_argument("--data", default=os.path.join(HERE, "data"), help="data folder")
    ap.add_argument("--name", default="Player", help="display name of the first local account")
    ap.add_argument("--country", default="US")
    ap.add_argument("--allow-login", action="store_true",
                    help="accept Disney ID sign-ins (WARNING: the game then replaces its local saves with the "
                         "cloud ones; keep a backup)")
    ap.add_argument("--tail", type=int, nargs="?", const=40, help="print the last N logged requests and exit")
    ap.add_argument("--game", default="",
                    help="game folder to watch for changes (exe build, CrabeLoader, mods, menus); read only")
    ap.add_argument("--watch-interval", type=int, default=30,
                    help="seconds between two checks of --game (0 = only at start-up)")
    ap.add_argument("--client-dll", default=DEFAULT_CLIENT_DLL,
                    help="reference dinet_bk.dll; a different one in the watched game is reported")
    ap.add_argument("--changes", type=int, nargs="?", const=40,
                    help="print the last N detected changes and exit")
    a = ap.parse_args(argv)
    if a.tail:
        return tail(a.data, a.tail)
    if a.changes:
        return tail_changes(a.data, a.changes)
    if a.game and not os.path.isfile(os.path.join(a.game, "DisneyInfinity3.exe")):
        raise SystemExit("--game: DisneyInfinity3.exe not found in %s" % a.game)
    srv = Server((a.host, a.port), Handler)
    srv.store = Store(a.data)
    srv.watch = gamewatch.Watch(a.data, a.game or None, a.client_dll, quiet_prefixes=("/_status", "/_changes"))
    srv.public_url = (a.public or "http://127.0.0.1:%d" % a.port).rstrip("/")
    srv.public_fixed = bool(a.public)
    srv.public_ip = a.public_ip.strip()
    srv.lobby = {}
    srv.presence = {}
    srv.invites = {}
    srv.requests = {}
    srv.player_name = a.name
    srv.country = a.country
    srv.allow_login = a.allow_login
    if not a.allow_login:
        print("Disney ID sign-in is REFUSED (protects your local saves). Use --allow-login to accept it.")
    print("Disney Infinity local server %s on %s:%d  (game url %s)" % (VERSION, a.host, a.port, srv.public_url))
    print("Data: %s   log: %s" % (a.data, os.path.join(a.data, "logs", "requests.jsonl")))
    srv.watch.start(a.watch_interval)
    for w in srv.watch.warnings():
        print("  !! %s" % w)
    print("Ctrl+C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("stopped")
    finally:
        srv.watch.save()


if __name__ == "__main__":
    main()
