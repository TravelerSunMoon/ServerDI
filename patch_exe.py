# -*- coding: utf-8 -*-
"""
Writes a COPY of DisneyInfinity3.exe whose Disney service urls point to the local server.
The original executable is never modified.

    python patch_exe.py --game "<game folder>"
    python patch_exe.py --game ... --base http://192.168.1.20      (server on another PC, port 80)
    python patch_exe.py --game ... --restore                        (delete the copy)

What is changed: every ASCII string that starts with https://api.disney.com or
https://cdninfinity-a.akamaihd.net is rewritten in place to <base>... (plain http, so no
certificate is needed). The new string must not be longer than the old one, so the base
url can have at most 22 characters (http://127.0.0.1:8085 has 21).
Also writes steam_appid.txt (541670) next to the copy: without it Steam relaunches the
original executable when the copy is started directly. Steam must be running.
"""

import argparse
import hashlib
import os
import shutil
import struct
import sys

APPID = "541670"                       # Disney Infinity 3.0: Gold Edition
KNOWN_MD5 = {"7ee2954a6c6ca7070bb4bfba0a8b9f94": "Steam build 1522802"}
PREFIXES = (b"https://api.disney.com", b"https://cdninfinity-a.akamaihd.net")
COPY_NAME = "DisneyInfinity3_local.exe"


def patch_bytes(data, base):
    """Rewrites every Disney service url in the exe image to `base`; returns (data, changes)."""
    base = base.rstrip("/").encode("ascii")
    out = bytearray(data)
    changes = []
    for pref in PREFIXES:
        if len(base) > len(pref):
            raise SystemExit("Base url '%s' is longer than '%s' (%d > %d characters)."
                             % (base.decode(), pref.decode(), len(base), len(pref)))
        start = 0
        while True:
            i = data.find(pref, start)
            if i < 0:
                break
            start = i + 1
            end = data.find(b"\0", i)
            if end < 0 or end - i > 512:
                continue
            old = data[i:end]
            new = base + old[len(pref):]
            out[i:end] = new + b"\0" * (len(old) - len(new))
            changes.append((i, old.decode("latin-1"), new.decode("latin-1")))
    return bytes(out), changes


# Code patches for the local copy (only for the known Steam build; original bytes checked).
# The game's cloud response signature check (cdecl, 9 callers, returns 1 = OK): the cloud
# responses of the shut-down Disney service were signed with a white-box key; the local
# server cannot reproduce it, so the local copy accepts its responses.
# Only the local copy is patched; the original executable is never touched.
CODE_PATCHES = {
    "7ee2954a6c6ca7070bb4bfba0a8b9f94": [
        ("VerifyResponseSignature -> SIG_RESULT_OK", 0x01330040,
         bytes.fromhex("64a1000000006aff68cb55a601"), bytes.fromhex("b801000000c3")),
    ],
}


def _va2off(data, va):
    """File offset of a virtual address in a PE image, or None."""
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    nsec = struct.unpack_from("<H", data, pe + 6)[0]
    opt = pe + 24
    base = struct.unpack_from("<I", data, opt + 28)[0]
    so = opt + struct.unpack_from("<H", data, pe + 20)[0]
    for _ in range(nsec):
        vsz, sva, rsz, raw = struct.unpack_from("<IIII", data, so + 8)
        if sva <= va - base < sva + max(vsz, rsz):
            return raw + va - base - sva
        so += 40
    return None


def patch_code(data, md5):
    """Applies the code patches known for this build (original bytes are checked first)."""
    out = bytearray(data)
    done = []
    for name, va, expect, new in CODE_PATCHES.get(md5, []):
        off = _va2off(data, va)
        if off is None or data[off:off + len(expect)] != expect:
            raise SystemExit("Code patch '%s': unexpected bytes at 0x%x, executable not supported." % (name, va))
        out[off:off + len(new)] = new
        done.append((off, name, va))
    return bytes(out), done


# --dinet: the copy loads dinet/dinet_bk.dll (our native helper) instead of
# bink2w32.dll; the helper forwards every Bink export to the real bink2w32.dll.
# Both names have 12 characters, so the import string is replaced in place.
DINET_IMPORT = (b"bink2w32.dll\0", b"dinet_bk.dll\0")
DINET_DLL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dinet", "dinet_bk.dll")
DINET_LUA = os.path.join(os.path.dirname(DINET_DLL), "dinet.lua")     # friends / invites screens


def write_dinet_ini(path, base):
    """Creates di_net.ini or updates its server url; the player's other settings are kept."""
    import configparser
    cp = configparser.ConfigParser()
    cp.optionxform = str
    if os.path.exists(path):
        cp.read(path, encoding="utf-8")
    for sec, key, value in (("signin", "block_signout", "1"), ("net", "port", "9104"),
                            ("saves", "local_toybox_list", "1"), ("saves", "protect", "1"),
                            ("signin", "skip_migration_warning", "1"), ("net", "invite_popup", "1"),
                            ("net", "force_multiplayer_levels", "1"), ("chat", "enabled", "1"),
                            ("chat", "seconds", "8"), ("chat", "fov", "55"), ("chat", "head_height", "2.2"),
                            ("chat", "mirror", "0"), ("chat", "poll_ms", "1000"), ("chat", "debug", "0"),
                            ("chat", "theme", "1")):
        if not cp.has_section(sec):
            cp.add_section(sec)
        if not cp.has_option(sec, key):
            cp.set(sec, key, value)
    if not cp.has_section("server"):
        cp.add_section("server")
    cp.set("server", "url", base.rstrip("/"))
    cp.set("server", "exe_url", base.rstrip("/"))       # the url written into the exe copy (see dinet_bk F8 list)
    if not cp.has_section("servers"):
        cp.add_section("servers")
    with open(path, "w", encoding="utf-8") as f:
        f.write("; dinet_bk.dll settings\n"
                "; [signin] block_signout=1 keeps the Disney ID session after the Gold cloud migration;\n"
                ";          skip_migration_warning=1 hides the 'you already migrated' popup\n"
                "; [net] port = Net-Z UDP port (open it in the router to host over the internet);\n"
                ";       name = how other players see you in the F8 / friends list (default: your Steam name);\n"
                ";       invite_popup=1 asks 'join?' when another player invites you\n"
                ";       force_multiplayer_levels=1 allows network play in levels the game keeps single-player\n"
                ";       (intro Toy Box, hub doors...) except playsets; 2 = playsets too (experimental: the guest\n"
                ";       freezes, playsets were shipped without network play); 0 = the game's own rules\n"
                "; [saves] local_toybox_list=1: the Toy Box menu reads the local saves even when signed in;\n"
                ";         protect=1: saves are copied to di_saves\\<time> at start, and the files the\n"
                ";         migration removes are copied back when it ends\n"
                "; [chat] enabled=1: key T opens the text chat in game (CrabeLoader mod only);\n"
                ";        seconds = how long a speech bubble stays; fov = the game's vertical field of view used\n"
                ";        to place the bubbles; head_height = how high above a character's feet they appear;\n"
                ";        mirror=1 if bubbles show on the wrong side; poll_ms = how often new messages are\n"
                ";        fetched; debug=1 writes camera / player details to di_net.log;\n"
                ";        theme=1: the chat takes the colours of the playset you play (0 = always blue)\n"
                "; [chat_colors] (optional) your own colours per playset: <playset> = border,panel,name as RRGGBB,\n"
                ";        e.g. InsideOut = EB6EC8,3C1955,FFC8F0. di_net.log shows the playset name on every level\n"
                "; [server] url = the server the game uses (set by patch_exe.py; the F8 server list changes it,\n"
                ";          it applies at the next game start). exe_url = the url written into the exe copy:\n"
                ";          when url differs, dinet_bk rewrites the exe's urls in memory at start-up (CrabeLoader\n"
                ";          1.1+; http:// and at most 22 characters)\n"
                "; [servers] (optional) more servers for the F8 list, one per line:\n"
                ";          <any key> = Name|http://host:port|description\n")
        cp.write(f)


def patch_import(data):
    """Renames the bink2w32.dll import to dinet_bk.dll (same length, in place)."""
    old, new = DINET_IMPORT
    if data.count(old) != 1:
        raise SystemExit("bink2w32.dll import not found exactly once: unsupported executable.")
    return data.replace(old, new)


def main(argv=None):
    """Command line entry point: writes (or with --restore deletes) DisneyInfinity3_local.exe."""
    ap = argparse.ArgumentParser(description="Create a copy of DisneyInfinity3.exe that uses the local server")
    ap.add_argument("--game", required=True, help="game folder (contains DisneyInfinity3.exe)")
    ap.add_argument("--base", default="http://127.0.0.1:8085", help="url of the local server (max 22 chars)")
    ap.add_argument("--out", default=COPY_NAME)
    ap.add_argument("--restore", action="store_true", help="delete the patched copy")
    ap.add_argument("--no-signature-bypass", dest="signature_bypass", action="store_false",
                    help="keep the cloud response signature check (cloud saves / private Toy Boxes will fail)")
    ap.add_argument("--dinet", action="store_true",
                    help="load the native helper dinet/dinet_bk.dll (build it first with dinet/build.bat)")
    a = ap.parse_args(argv)
    src = os.path.join(a.game, "DisneyInfinity3.exe")
    dst = os.path.join(a.game, a.out)
    if a.restore:
        if os.path.exists(dst):
            os.remove(dst)
            print("Deleted", dst)
        print("The original executable was never modified.")
        return
    if os.path.abspath(dst) == os.path.abspath(src):
        raise SystemExit("The output must be a different file than the original executable.")
    with open(src, "rb") as f:
        data = f.read()
    md5 = hashlib.md5(data).hexdigest()
    print("Original: %s (%s)" % (src, KNOWN_MD5.get(md5, "unknown build md5 %s - patching anyway" % md5)))
    new, changes = patch_bytes(data, a.base)
    if not any("coregames/config" in c[1] for c in changes):
        raise SystemExit("The config service url was not found: unsupported executable.")
    code = []
    if a.signature_bypass:
        if md5 not in CODE_PATCHES:
            print("WARNING: unknown build, the response signature check is NOT bypassed (cloud features fail).")
        new, code = patch_code(new, md5)
    if a.dinet:
        if not os.path.exists(DINET_DLL):
            raise SystemExit("Build the helper first: %s (run dinet\\build.bat)" % DINET_DLL)
        new = patch_import(new)
        shutil.copy2(DINET_DLL, os.path.join(a.game, "dinet_bk.dll"))
        shutil.copy2(DINET_LUA, os.path.join(a.game, "dinet.lua"))
        write_dinet_ini(os.path.join(a.game, "di_net.ini"), a.base)
        print("Native helper: import renamed to dinet_bk.dll, copied DLL, log -> di_net.log")
    with open(dst, "wb") as f:
        f.write(new)
    for off, old, nw in changes:
        print("  0x%08x  %s\n              -> %s" % (off, old, nw))
    for off, name, va in code:
        print("  code 0x%08x (va 0x%x): %s" % (off, va, name))
    print("%d urls redirected, %d code patch(es). Written: %s" % (len(changes), len(code), dst))
    appid = os.path.join(a.game, "steam_appid.txt")
    if not os.path.exists(appid):
        with open(appid, "w") as f:
            f.write(APPID)
        print("Written:", appid)
    with open(os.path.join(a.game, "di_local_patch.txt"), "w", encoding="utf-8") as f:
        f.write("source md5 %s\nbase %s\n" % (md5, a.base) + "".join("%08x %s -> %s\n" % c for c in changes)
                + "".join("%08x code: %s\n" % (off, name) for off, name, va in code))


if __name__ == "__main__":
    main()
