# -*- coding: utf-8 -*-
"""
gamewatch - change detection for the Disney Infinity 3.0 local server.

The server only sees what the games send it, and the game install on the server PC can change under it
(a Steam update, a new or updated CrabeLoader mod, a rebuilt client helper...). This module notices those
changes, prints them on the server console, keeps them in data/logs/changes.jsonl and exposes them through
GET /_changes and the "warnings" list of GET /_status.

Three independent detectors feed one ChangeLog:

  ClientTracker   passive, from the HTTP traffic of every connected game:
                    - a player connects for the first time;
                    - the version of a player's client helper changes (User-Agent "dinet_bk/<n>");
                    - a player runs an older client helper than the newest one seen (warning);
                    - a player's level-id fingerprint changes (mods that add levels were installed,
                      updated or removed on that PC);
                    - a User-Agent never seen before (another game build / another tool).
  EndpointTracker passive: the game asks for a route the server has never seen. After a game update this is
                  usually the first sign of a service the server does not handle yet.
  GameWatcher     active and optional (--game <folder>): takes a snapshot of the game folder at start-up and
                  every --watch-interval seconds, compares it with the previous one (also across server
                  restarts) and reports:
                    - DisneyInfinity3.exe changed, and whether the new build is one the tools support;
                    - the patched copy DisneyInfinity3_local.exe appeared / disappeared / changed;
                    - CrabeLoader installed or removed;
                    - a CrabeLoader mod added, removed, or changed (version from mod.json, or files);
                    - the client mod's dinet_bk.dll differs from the build shipped next to the server;
                    - assets/presentation/presentation.zip changed (a game update or another tool
                      rewrote the menus: reinstall the client mod).

Everything here only reads the game folder; nothing is ever written there.
State survives restarts in data/watch_state.json.
"""

import hashlib
import json
import os
import re
import threading
import time

# Build fingerprints the patch tools know (md5 of DisneyInfinity3.exe -> label).
KNOWN_BUILDS = {"7ee2954a6c6ca7070bb4bfba0a8b9f94": "Steam build 1522802"}
CLIENT_AGENT = re.compile(r"^dinet_bk/(\d+)")
SKIP_EXT = (".log", ".tmp", ".bak")           # files that change on every run: never a "mod change"
SKIP_DIRS = {"__pycache__", "di_saves", "logs"}
MAX_EVENTS = 500


def _now():
    """Local time as 'YYYY-MM-DDTHH:MM:SS'."""
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def md5_file(path, chunk=1 << 20):
    """md5 hex digest of a file, or '' when it cannot be read."""
    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            for b in iter(lambda: f.read(chunk), b""):
                h.update(b)
    except OSError:
        return ""
    return h.hexdigest()


class ChangeLog:
    """Ordered list of change events, printed on the console and appended to a jsonl file.

    An event is a dict: id (increasing), t (local time), kind (short machine name), level ("info" or
    "warning"), message (one English sentence) plus kind-specific fields.
    """

    def __init__(self, path=None, echo=True):
        """path: jsonl file to append events to (None = memory only); echo: print them."""
        self.path = path
        self.echo = echo
        self.lock = threading.Lock()
        self.events = []
        self.next_id = 1

    def add(self, kind, message, level="info", **fields):
        """Records an event and returns it."""
        with self.lock:
            ev = dict(id=self.next_id, t=_now(), kind=kind, level=level, message=message, **fields)
            self.next_id += 1
            self.events.append(ev)
            del self.events[:-MAX_EVENTS]
            if self.path:
                try:
                    os.makedirs(os.path.dirname(self.path), exist_ok=True)
                    with open(self.path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
                except OSError:
                    pass
        if self.echo:
            print("  %s change: %s" % ("!!" if level == "warning" else "--", message))
        return ev

    def since(self, last_id=0):
        """Events with an id greater than last_id (oldest first)."""
        with self.lock:
            return [e for e in self.events if e["id"] > last_id]


class ClientTracker:
    """Remembers, per player, the client helper version and level-id fingerprint the game reports."""

    def __init__(self, log, state):
        """log: the ChangeLog; state: persisted dict (keys 'clients' and 'agents')."""
        self.log = log
        self.clients = state.setdefault("clients", {})        # name -> {agent, version, zones, first, last}
        self.agents = state.setdefault("agents", [])          # every User-Agent seen
        self.lock = threading.Lock()

    @staticmethod
    def client_version(agent):
        """Version number of the client helper from its User-Agent ('dinet_bk/2' -> 2), None otherwise."""
        m = CLIENT_AGENT.match(agent or "")
        return int(m.group(1)) if m else None

    def newest_version(self):
        """Highest client helper version any player has used."""
        vs = [c.get("version") for c in self.clients.values() if c.get("version") is not None]
        return max(vs) if vs else None

    def observe_agent(self, agent):
        """Called for every request: reports User-Agents never seen before."""
        agent = (agent or "").strip()[:120]
        if not agent:
            return
        with self.lock:
            if agent in self.agents:
                return
            self.agents.append(agent)
        self.log.add("agent_new", "new client software seen: %s" % agent, agent=agent)

    def observe_player(self, name, agent, zones):
        """Called on every presence / host announcement of a player.

        name: player name, agent: User-Agent header, zones: '<fingerprint>-<levels added by mods>' or ''.
        """
        version = self.client_version(agent)
        with self.lock:
            old = self.clients.get(name)
            new = dict(old or {}, agent=agent, last=_now())
            if version is not None:
                new["version"] = version
            if zones:
                new["zones"] = zones
            if old is None:
                new["first"] = new["last"]
            self.clients[name] = new
            newest = self.newest_version()
        if old is None:
            self.log.add("player_new", "%s connected for the first time (%s)" % (name, agent or "unknown client"),
                         player=name, agent=agent)
        else:
            if version is not None and old.get("version") not in (None, version):
                self.log.add("client_version_changed", "%s updated the client helper: v%s -> v%s"
                             % (name, old["version"], version), player=name, old=old["version"], new=version)
            if zones and old.get("zones") and zones != old["zones"]:
                self.log.add("zones_changed", "%s's level ids changed: %s -> %s (mods with levels were added, "
                             "updated or removed on that PC)" % (name, old["zones"], zones),
                             player=name, old=old["zones"], new=zones)
        if version is not None and newest is not None and version < newest and \
                (old is None or old.get("version") != version):
            self.log.add("client_outdated", "%s runs client helper v%d, older than v%d used by other players: "
                         "update the client mod on that PC" % (name, version, newest), level="warning",
                         player=name, version=version, newest=newest)

    def warnings(self, online=None):
        """Current problems: outdated clients and level-id mismatches among the given online player names."""
        out = []
        with self.lock:
            newest = self.newest_version()
            names = [n for n in self.clients if online is None or n in online]
            for n in names:
                v = self.clients[n].get("version")
                if v is not None and newest is not None and v < newest:
                    out.append("%s runs an outdated client helper (v%d < v%d)" % (n, v, newest))
            fps = {}
            for n in names:
                z = self.clients[n].get("zones", "")
                if z:
                    fps.setdefault(z.partition("-")[0], []).append(n)
        if len(fps) > 1:
            out.append("players number the levels differently (%s): joins between groups load the wrong level"
                       % "; ".join("%s: %s" % (fp, ", ".join(sorted(ns))) for fp, ns in sorted(fps.items())))
        return out


class EndpointTracker:
    """Reports the first request to every route (path with ids folded into '*', at most 5 segments)."""

    ID_SEGMENT = re.compile(r"^(\d+|[0-9a-f]{8,}|\{?[0-9a-f-]{32,36}\}?|[0-9a-f]{2,}-[0-9a-f-]+)$", re.I)

    def __init__(self, log, state, quiet_prefixes=()):
        """log: the ChangeLog; state: persisted dict; quiet_prefixes: paths to ignore."""
        self.log = log
        self.routes = state.setdefault("endpoints", [])
        self.quiet = tuple(quiet_prefixes)
        self.lock = threading.Lock()
        self._known = set(self.routes)
        # A server with no route history (first run) learns the routes silently instead of reporting
        # every one of them; the next runs report only the new ones.
        self.learning = not self.routes

    @classmethod
    def route_of(cls, method, path):
        """'GET /infinity/save/v1/steam/playset/ri?x=1' -> 'GET /infinity/save/v1/steam/playset'."""
        segs = [s for s in path.split("?", 1)[0].lower().split("/") if s][:5]
        segs = ["*" if cls.ID_SEGMENT.match(s) else s for s in segs]
        return "%s /%s" % (method, "/".join(segs))

    def observe(self, method, path, status):
        """Called after every request with the status the server answered."""
        if path.startswith(self.quiet):
            return
        route = self.route_of(method, path)
        with self.lock:
            if route in self._known:
                return
            self._known.add(route)
            self.routes.append(route)
        if self.learning:
            return
        self.log.add("endpoint_new", "the game used a route not seen before: %s -> %d" % (route, status),
                     level="warning" if status >= 400 else "info", route=route, status=status)


class GameWatcher:
    """Snapshots a game folder and reports what changed between two snapshots."""

    def __init__(self, game_dir, log, state, client_dll=None):
        """game_dir: folder with DisneyInfinity3.exe; client_dll: reference dinet_bk.dll."""
        self.game = game_dir
        self.log = log
        self.state = state
        self.client_dll = client_dll                  # dinet_bk.dll shipped with the server, to compare with
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self.thread = None

    # ---------------------------------------------------------------- snapshot
    def _file_info(self, path, old=None, hash_it=True):
        """{size, mtime, md5} of a file (md5 reused from `old` when size and mtime did not change).

        A rewrite that keeps the size and lands in the same file-system timestamp tick is not noticed;
        hashing the large game files on every check would cost far more than that rare case.
        """
        try:
            st = os.stat(path)
        except OSError:
            return None
        info = dict(size=st.st_size, mtime=st.st_mtime_ns)
        if hash_it:
            if old and old.get("size") == info["size"] and old.get("mtime") == info["mtime"] and old.get("md5"):
                info["md5"] = old["md5"]
            else:
                info["md5"] = md5_file(path)
        return info

    def _mod_info(self, folder):
        """{id, name, version, files} for one CrabeLoader mod folder; files = hash of names, sizes, mtimes."""
        meta = {}
        try:
            with open(os.path.join(folder, "mod.json"), encoding="utf-8-sig") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            pass
        h = hashlib.md5()
        count = 0
        for root, dirs, files in os.walk(folder):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for fn in sorted(files):
                if fn.lower().endswith(SKIP_EXT):
                    continue
                p = os.path.join(root, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                h.update(("%s|%d|%d\n" % (os.path.relpath(p, folder).lower(), st.st_size, st.st_mtime_ns))
                         .encode("utf-8"))
                count += 1
        return dict(name=str(meta.get("name", "")), version=str(meta.get("version", "")),
                    files=h.hexdigest(), count=count)

    def snapshot(self, old=None):
        """Current state of the game folder (dict, JSON-serialisable)."""
        old = old or {}
        g = self.game
        snap = dict(t=_now(), game=g)
        snap["exe"] = self._file_info(os.path.join(g, "DisneyInfinity3.exe"), old.get("exe"))
        snap["exe_local"] = self._file_info(os.path.join(g, "DisneyInfinity3_local.exe"), old.get("exe_local"))
        # CrabeLoader 1.0 replaces bink2w32.dll (keeps bink2w32_orig.dll); 1.1+ is dinput8.dll + CrabeLoader.asi
        snap["crabeloader"] = (os.path.exists(os.path.join(g, "bink2w32_orig.dll"))
                               or os.path.exists(os.path.join(g, "CrabeLoader.asi")))
        snap["presentation"] = self._file_info(os.path.join(g, "assets", "presentation", "presentation.zip"),
                                               old.get("presentation"))
        mods = {}
        mdir = os.path.join(g, "mods")
        if os.path.isdir(mdir):
            for d in sorted(os.listdir(mdir)):
                if os.path.isfile(os.path.join(mdir, d, "mod.json")):
                    mods[d] = self._mod_info(os.path.join(mdir, d))
        snap["mods"] = mods
        snap["client_dll"] = self._file_info(os.path.join(mdir, "dinet_online", "dinet_bk.dll"),
                                             old.get("client_dll"))
        # CrabeLoader 1.1+ loads this copy (main.lua then leaves the .dll alone)
        snap["client_asi"] = self._file_info(os.path.join(mdir, "dinet_online", "dinet_bk.asi"),
                                             old.get("client_asi"))
        return snap

    # ---------------------------------------------------------------- compare
    def compare(self, old, new):
        """Logs one event per difference between two snapshots; returns the number of events."""
        n = 0

        def add(kind, msg, level="info", **kw):
            nonlocal n
            n += 1
            self.log.add(kind, msg, level, **kw)

        def changed(a, b):
            if not a or not b:
                return bool(a) != bool(b)
            return a.get("md5", (a["size"], a["mtime"])) != b.get("md5", (b["size"], b["mtime"]))

        if changed(old.get("exe"), new.get("exe")):
            md5 = (new.get("exe") or {}).get("md5", "")
            build = KNOWN_BUILDS.get(md5)
            if build:
                add("game_exe_changed", "DisneyInfinity3.exe changed: %s (supported)" % build, md5=md5)
            else:
                add("game_exe_changed", "DisneyInfinity3.exe changed to an unknown build (md5 %s): the exe patch "
                    "and the client helper's addresses may not apply; check before playing" % (md5 or "?"),
                    level="warning", md5=md5)
        if changed(old.get("exe_local"), new.get("exe_local")):
            if not new.get("exe_local"):
                add("local_exe_removed", "DisneyInfinity3_local.exe was removed: run install_crabe_mod.py again",
                    level="warning")
            else:
                add("local_exe_changed", "DisneyInfinity3_local.exe was (re)created")
        if bool(old.get("crabeloader")) != bool(new.get("crabeloader")):
            add("crabeloader_changed", "CrabeLoader was %s" % ("installed" if new.get("crabeloader") else
                                                               "removed: the client mod will not load"),
                level="info" if new.get("crabeloader") else "warning")
        if changed(old.get("presentation"), new.get("presentation")):
            add("presentation_changed", "assets/presentation/presentation.zip changed (game update or another "
                "tool): rebuild the client mod's menus with install_crabe_mod.py", level="warning")
        om, nm = old.get("mods", {}), new.get("mods", {})
        for m in sorted(set(nm) - set(om)):
            add("mod_added", "mod added: %s %s" % (m, nm[m].get("version", "")), mod=m)
        for m in sorted(set(om) - set(nm)):
            add("mod_removed", "mod removed: %s" % m, level="warning" if m == "dinet_online" else "info", mod=m)
        for m in sorted(set(om) & set(nm)):
            a, b = om[m], nm[m]
            if a.get("version") != b.get("version"):
                add("mod_updated", "mod %s updated: %s -> %s (players need the same version to share levels)"
                    % (m, a.get("version") or "?", b.get("version") or "?"), mod=m)
            elif a.get("files") != b.get("files"):
                add("mod_files_changed", "mod %s changed its files (same version %s)" % (m, b.get("version") or "?"),
                    mod=m)
        if changed(old.get("client_dll"), new.get("client_dll")):
            add("client_dll_changed", "mods/dinet_online/dinet_bk.dll changed", mod="dinet_online")
        if changed(old.get("client_asi"), new.get("client_asi")):
            add("client_dll_changed", "mods/dinet_online/dinet_bk.asi changed", mod="dinet_online")
        return n

    def check_client_build(self, snap):
        """Warns when the installed client helper is not the build shipped with the server."""
        if not self.client_dll or not os.path.exists(self.client_dll):
            return
        ref = md5_file(self.client_dll)
        for key, name in (("client_dll", "dinet_bk.dll"), ("client_asi", "dinet_bk.asi")):
            inst = (snap.get(key) or {}).get("md5")
            if inst and ref and ref != inst:
                self.log.add("client_dll_mismatch", "the game's mods/dinet_online/%s is not the build in %s: "
                             "reinstall the client mod" % (name, self.client_dll), level="warning")

    def warnings(self):
        """Current problems of the watched game folder."""
        snap = self.state.get("game") or {}
        out = []
        md5 = (snap.get("exe") or {}).get("md5")
        if md5 and md5 not in KNOWN_BUILDS:
            out.append("unknown game build (md5 %s)" % md5)
        if snap and not snap.get("crabeloader"):
            out.append("CrabeLoader is not installed in the game folder")
        if snap and "dinet_online" not in snap.get("mods", {}):
            out.append("the client mod (mods/dinet_online) is not installed in the game folder")
        return out

    # ---------------------------------------------------------------- run
    def check(self):
        """Takes a new snapshot, reports differences with the last one and stores it. Returns event count."""
        with self.lock:
            old = self.state.get("game")
            new = self.snapshot(old)
            if old is None or old.get("game") != new["game"]:
                self.log.add("game_watch_start", "watching %s: %s, %d mod(s)%s" % (
                    self.game, KNOWN_BUILDS.get((new.get("exe") or {}).get("md5"), "unknown build"),
                    len(new["mods"]), ", CrabeLoader" if new["crabeloader"] else ""))
                n = 0
                self.check_client_build(new)
            else:
                n = self.compare(old, new)
                if (old.get("client_dll") or {}).get("md5") != (new.get("client_dll") or {}).get("md5"):
                    self.check_client_build(new)
            self.state["game"] = new
            return n

    def start(self, interval, on_change=None):
        """Checks now, then every `interval` seconds in a daemon thread (no thread when interval <= 0)."""
        self.check()
        if on_change:
            on_change()
        if interval <= 0:
            return

        def loop():
            while not self._stop.wait(interval):
                try:
                    if self.check() and on_change:
                        on_change()
                except Exception as ex:                     # a watcher error must never stop the server
                    print("  !! game watcher: %r" % ex)

        self.thread = threading.Thread(target=loop, name="gamewatch", daemon=True)
        self.thread.start()

    def stop(self):
        """Stops the background thread after its current wait."""
        self._stop.set()


class Watch:
    """Facade used by the server: one ChangeLog, the trackers, the optional watcher and persisted state."""

    def __init__(self, data_dir, game_dir=None, client_dll=None, quiet_prefixes=(), echo=True):
        """Loads data/watch_state.json and builds the change log, trackers and game watcher."""
        self.state_path = os.path.join(data_dir, "watch_state.json")
        self.state = {}
        try:
            with open(self.state_path, encoding="utf-8") as f:
                self.state = json.load(f)
        except (OSError, ValueError):
            self.state = {}
        self.save_lock = threading.Lock()
        self.log = ChangeLog(os.path.join(data_dir, "logs", "changes.jsonl"), echo=echo)
        self.clients = ClientTracker(self.log, self.state)
        self.endpoints = EndpointTracker(self.log, self.state, quiet_prefixes)
        self.game = GameWatcher(game_dir, self.log, self.state, client_dll) if game_dir else None

    def save(self):
        """Writes watch_state.json atomically."""
        with self.save_lock:
            tmp = self.state_path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.state, f, indent=1)
                os.replace(tmp, self.state_path)
            except OSError:
                pass

    def start(self, interval):
        """Starts the game folder watcher, if a game folder was given."""
        if self.game:
            self.game.start(interval, on_change=self.save)

    def request(self, method, path, status, agent):
        """Hook for every finished request."""
        before = (self.log.next_id, len(self.endpoints.routes))
        self.clients.observe_agent(agent)
        self.endpoints.observe(method, path, status)
        if (self.log.next_id, len(self.endpoints.routes)) != before:
            self.save()

    def player(self, name, agent, zones):
        """Hook for every presence / host announcement."""
        before = self.log.next_id
        self.clients.observe_player(name, agent, zones)
        if self.log.next_id != before:
            self.save()

    def warnings(self, online=None):
        """Every current warning (clients among `online` names, and the game folder)."""
        return self.clients.warnings(online) + (self.game.warnings() if self.game else [])

    def summary(self):
        """Short dict for /_status."""
        g = self.state.get("game") or {}
        return dict(game=dict(folder=g.get("game"),
                              build=KNOWN_BUILDS.get((g.get("exe") or {}).get("md5"), "unknown") if g else None,
                              crabeloader=g.get("crabeloader"),
                              mods={k: v.get("version", "") for k, v in g.get("mods", {}).items()},
                              checked=g.get("t")) if g else None,
                    clients={n: dict(version=c.get("version"), zones=c.get("zones", ""), last=c.get("last"))
                             for n, c in self.state.get("clients", {}).items()},
                    last_change=self.log.next_id - 1)
