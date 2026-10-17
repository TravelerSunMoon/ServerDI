# -*- coding: utf-8 -*-
"""
Tests for di_server.py and gamewatch.py (standard library only).

    python -m unittest discover -s tests -v

The server runs on a free local port with a temporary data folder; the "game folder" is a fake one made of
small files, so no game install is needed.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from urllib.parse import urlencode

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import di_server  # noqa: E402
import gamewatch  # noqa: E402


def write(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def make_game(root):
    """A fake game folder: exe, CrabeLoader marker, menus archive and the client mod."""
    write(os.path.join(root, "DisneyInfinity3.exe"), b"exe v1")
    write(os.path.join(root, "bink2w32_orig.dll"))
    write(os.path.join(root, "assets", "presentation", "presentation.zip"), b"menus v1")
    write(os.path.join(root, "mods", "dinet_online", "mod.json"), json.dumps({"id": "dinet_online",
                                                                              "version": "1.0.0"}).encode())
    write(os.path.join(root, "mods", "dinet_online", "dinet_bk.dll"), b"dll v1")


class GameWatcherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.game = os.path.join(self.tmp, "game")
        make_game(self.game)
        self.log = gamewatch.ChangeLog(echo=False)
        self.state = {}
        self.w = gamewatch.GameWatcher(self.game, self.log, self.state)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def kinds(self):
        return [e["kind"] for e in self.log.events]

    def test_first_check_only_announces(self):
        self.assertEqual(self.w.check(), 0)
        self.assertEqual(self.kinds(), ["game_watch_start"])

    def test_detects_changes(self):
        self.w.check()
        write(os.path.join(self.game, "DisneyInfinity3.exe"), b"exe v2 - a game update")
        write(os.path.join(self.game, "assets", "presentation", "presentation.zip"), b"menus v2, rebuilt")
        write(os.path.join(self.game, "mods", "other", "mod.json"), b'{"version": "0.1"}')
        write(os.path.join(self.game, "mods", "dinet_online", "mod.json"), b'{"version": "1.1.0"}')
        write(os.path.join(self.game, "mods", "dinet_online", "di_net.log"), b"logs never count")
        os.remove(os.path.join(self.game, "bink2w32_orig.dll"))
        self.assertGreater(self.w.check(), 0)
        k = self.kinds()
        for kind in ("game_exe_changed", "presentation_changed", "mod_added", "mod_updated", "crabeloader_changed"):
            self.assertIn(kind, k)
        exe = [e for e in self.log.events if e["kind"] == "game_exe_changed"][0]
        self.assertEqual(exe["level"], "warning")             # unknown build
        self.assertIn("CrabeLoader is not installed in the game folder", self.w.warnings())

    def test_log_files_are_ignored(self):
        self.w.check()
        write(os.path.join(self.game, "mods", "dinet_online", "di_net.log"), b"new log line")
        self.assertEqual(self.w.check(), 0)

    def test_crabeloader_1_1_asi(self):
        self.w.check()
        os.remove(os.path.join(self.game, "bink2w32_orig.dll"))          # 1.1 installer restores bink
        write(os.path.join(self.game, "CrabeLoader.asi"), b"asi")
        self.w.check()
        self.assertNotIn("crabeloader_changed", self.kinds())
        self.assertNotIn("CrabeLoader is not installed in the game folder", self.w.warnings())

    def test_client_asi_mismatch(self):
        ref = os.path.join(self.tmp, "ref_dinet_bk.dll")
        write(ref, b"dll v1")
        write(os.path.join(self.game, "mods", "dinet_online", "dinet_bk.asi"), b"dll v0, left behind")
        w = gamewatch.GameWatcher(self.game, self.log, {}, client_dll=ref)
        w.check()
        msgs = [e["message"] for e in self.log.events if e["kind"] == "client_dll_mismatch"]
        self.assertEqual(len(msgs), 1)
        self.assertIn("dinet_bk.asi", msgs[0])

    def test_client_dll_mismatch(self):
        ref = os.path.join(self.tmp, "ref_dinet_bk.dll")
        write(ref, b"dll v2")
        w = gamewatch.GameWatcher(self.game, self.log, {}, client_dll=ref)
        w.check()
        self.assertIn("client_dll_mismatch", self.kinds())


class ClientTrackerTest(unittest.TestCase):
    def test_versions_and_zones(self):
        log = gamewatch.ChangeLog(echo=False)
        t = gamewatch.ClientTracker(log, {})
        t.observe_player("ana", "dinet_bk/2", "AAAAAAAA-0")
        t.observe_player("bob", "dinet_bk/1", "BBBBBBBB-3")
        t.observe_player("ana", "dinet_bk/3", "AAAAAAAA-0")
        t.observe_player("ana", "dinet_bk/3", "CCCCCCCC-1")
        kinds = [e["kind"] for e in log.events]
        self.assertIn("client_outdated", kinds)
        self.assertIn("client_version_changed", kinds)
        self.assertIn("zones_changed", kinds)
        warn = " ".join(t.warnings({"ana", "bob"}))
        self.assertIn("bob runs an outdated client helper", warn)
        self.assertIn("number the levels differently", warn)
        self.assertEqual(t.warnings({"ana"}), [])

    def test_route_folding(self):
        r = gamewatch.EndpointTracker.route_of
        self.assertEqual(r("GET", "/infinity/save/v1/steam/playset/ri?set=0"), "GET /infinity/save/v1/steam/playset")
        self.assertEqual(r("GET", "/profile/12345"), "GET /profile/*")


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.game = os.path.join(cls.tmp, "game")
        make_game(cls.game)
        data = os.path.join(cls.tmp, "data")
        # a route history, so new routes are reported (a first run only learns them)
        os.makedirs(data)
        with open(os.path.join(data, "watch_state.json"), "w") as f:
            json.dump({"endpoints": ["GET /coregames/config/v1/infinity3/steam"]}, f)
        srv = di_server.Server(("127.0.0.1", 0), di_server.Handler)
        srv.store = di_server.Store(data)
        srv.watch = gamewatch.Watch(data, cls.game, None, quiet_prefixes=("/_status", "/_changes"), echo=False)
        srv.watch.start(0)
        srv.public_url, srv.public_fixed, srv.public_ip = "http://127.0.0.1", False, ""
        srv.lobby, srv.presence, srv.invites, srv.requests = {}, {}, {}, {}
        srv.chat, srv.chat_next, srv.chat_lock = [], 1, threading.Lock()
        srv.player_name, srv.country, srv.allow_login = "Player", "US", True
        srv.server_name, srv.server_description = "TestServer", "for the tests"
        cls.srv = srv
        cls.base = "http://127.0.0.1:%d" % srv.server_address[1]
        cls.thread = threading.Thread(target=srv.serve_forever, daemon=True)
        cls.thread.start()
        cls._stdout, sys.stdout = sys.stdout, open(os.devnull, "w")     # the server prints every request

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        sys.stdout.close()
        sys.stdout = cls._stdout
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def call(self, method, path, body=None, agent="GameClient/1.0", ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"User-Agent": agent, "Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_config_points_back(self):
        st, body = self.call("GET", "/coregames/config/v1/infinity3/steam/")
        self.assertEqual(st, 200)
        cfg = json.loads(body)
        self.assertTrue(cfg["url_inf_save"].startswith(self.base))

    def test_login_and_store(self):
        st, body = self.call("POST", "/coregames/did/v3/authenticate", json.dumps({"username": "tester"}))
        self.assertEqual(st, 200)
        self.assertIn("access_token", json.loads(body))
        self.assertEqual(self.call("GET", "/infinity/profile/v2/steam/x")[0], 404)
        self.call("PUT", "/infinity/profile/v2/steam/x", '{"a": 1}')
        self.assertEqual(self.call("GET", "/infinity/profile/v2/steam/x"), (200, b'{"a": 1}'))

    def test_presence_status_and_changes(self):
        form = "application/x-www-form-urlencoded"
        self.call("POST", "/dinet/v1/presence", urlencode(dict(name="ana", private_ip="10.0.0.2",
                                                               zones="AAAAAAAA-0")), "dinet_bk/2", form)
        self.call("POST", "/dinet/v1/presence", urlencode(dict(name="bob", private_ip="10.0.0.3",
                                                               zones="BBBBBBBB-1")), "dinet_bk/1", form)
        st, body = self.call("GET", "/_status")
        s = json.loads(body)
        self.assertEqual({p["name"] for p in s["players"]}, {"ana", "bob"})
        self.assertTrue(any("outdated" in w for w in s["warnings"]))
        self.assertTrue(any("levels differently" in w for w in s["warnings"]))
        self.assertEqual(s["watch"]["clients"]["ana"]["version"], 2)
        self.call("GET", "/some/new/service/route")
        ev = json.loads(self.call("GET", "/_changes?since=0")[1])["events"]
        kinds = {e["kind"] for e in ev}
        self.assertTrue({"player_new", "client_outdated", "endpoint_new", "agent_new"} <= kinds)
        last = ev[-1]["id"]
        self.assertEqual(json.loads(self.call("GET", "/_changes?since=%d" % last)[1])["events"], [])
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "data", "watch_state.json")))

    def test_server_directory(self):
        st, body = self.call("GET", "/dinet/v1/hello")
        self.assertEqual(st, 200)
        name, online, version = body.decode().rstrip("\n").split("\t")
        self.assertEqual((name, version), ("TestServer", di_server.VERSION))
        # no servers.json: only this server
        st, body = self.call("GET", "/dinet/v1/servers")
        lines = body.decode().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].split("\t")[0], "TestServer")
        # servers.json adds others (read on every request); duplicates, bad urls and tabs are dropped
        fp = os.path.join(self.srv.store.root, "servers.json")
        with open(fp, "w", encoding="utf-8") as f:
            json.dump([{"name": "Friend\tServer", "url": "http://10.0.0.5:85/", "description": "LAN"},
                       {"name": "Dup", "url": "http://10.0.0.5:85"}, {"name": "Bad", "url": "ftp://x"},
                       "junk"], f)
        try:
            st, body = self.call("GET", "/dinet/v1/servers")
            lines = body.decode().splitlines()
            self.assertEqual(lines[1:], ["Friend Server\thttp://10.0.0.5:85\tLAN"])
        finally:
            os.remove(fp)

    def test_chat(self):
        form = "application/x-www-form-urlencoded"
        # a game that just started only learns the last id
        st, body = self.call("GET", "/dinet/v1/chat?name=bob", agent="dinet_bk/3")
        start = int(body.decode().splitlines()[0][1:])
        st, body = self.call("POST", "/dinet/v1/chat", urlencode(dict(name="ana", tag="Ana_Steam",
                                                                       text="hola\tmundo\n!")), "dinet_bk/3", form)
        self.assertEqual(st, 200)
        self.call("POST", "/dinet/v1/chat", urlencode(dict(name="bob", tag="Bob", text="hi")), "dinet_bk/3", form)
        self.assertEqual(self.call("POST", "/dinet/v1/chat", urlencode(dict(name="bob", text="")), "dinet_bk/3",
                                   form)[0], 400)
        lines = self.call("GET", "/dinet/v1/chat?name=bob&since=%d" % start)[1].decode().splitlines()
        self.assertEqual(lines[0], "#%d" % (start + 2))
        self.assertEqual(lines[1:], ["%d\tana\tAna_Steam\thola mundo !" % (start + 1)])   # not bob's own
        self.assertEqual(self.call("GET", "/dinet/v1/chat?name=bob&since=%d" % (start + 2))[1].decode(),
                         "#%d\n" % (start + 2))
        long = self.call("POST", "/dinet/v1/chat", urlencode(dict(name="ana", text="x" * 500)), "dinet_bk/3", form)
        self.assertEqual(long[0], 200)
        last = self.call("GET", "/dinet/v1/chat?name=bob&since=%d" % (start + 2))[1].decode().splitlines()[1]
        self.assertEqual(len(last.split("\t")[3]), di_server.CHAT_TEXT)
        with open(os.path.join(self.tmp, "data", "logs", "chat.jsonl"), encoding="utf-8") as f:
            self.assertEqual(len(f.readlines()), 3)


if __name__ == "__main__":
    unittest.main()
