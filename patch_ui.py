# -*- coding: utf-8 -*-
"""
Re-enables the hidden online menus of Disney Infinity 3.0 Gold Edition (PC).

The Gold Edition hides every online option through the Lua function
UI_IsTOGOOfflineGame() (true in this build): pause menu "Quests"/online options,
Toy Box sharing, Disney ID screens, online settings... This tool adds, at the very
start of every presentation script that asks that question, the equivalent of

        UI_IsTOGOOfflineGame = function() return false end

so the menus behave like the original online PC version. Nothing else is changed:
UI_IsPlalaGame, the store and every purchase / VirtualReader lock check stay as they are.

    python patch_ui.py --game "<game folder>"
    python patch_ui.py --game "..." --restore         (put the original presentation.zip back)

The original archive is kept as assets/presentation/presentation.zip.orig.
Needs the container tool tool/ditool.py (pip install cryptography).
NOTE: this archive is shared by both executables; with the patch, the online menus also
appear in the unpatched DisneyInfinity3.exe (they just fail without the local server).
"""

import argparse
import hashlib
import importlib.util
import os
import shutil
import struct
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
# $DITOOL, tool/ditool.py inside this repository, or ../tool/ditool.py (layout of older packages)
DEFAULT_DITOOL = next((p for p in (os.environ.get("DITOOL", ""), os.path.join(HERE, "tool", "ditool.py"),
                                   os.path.join(os.path.dirname(HERE), "tool", "ditool.py")) if os.path.exists(p)),
                      os.path.join(HERE, "tool", "ditool.py"))
FLAG = b"UI_IsTOGOOfflineGame"
HEADERS = {b"\x1bLuaQ\x00\x01\x04\x04\x04\x08\x00": "<", b"\x1bLuaQ\x00\x00\x04\x04\x04\x08\x00": ">"}


# ---------------------------------------------------------------- Lua 5.1 bytecode
def luac_parse(b):
    """Parses a Lua 5.1 bytecode file (either endianness) into nested function dicts."""
    E = HEADERS.get(bytes(b[:12]))
    if E is None:
        raise ValueError("not Lua 5.1 bytecode")
    p = [12]

    def u8():
        v = b[p[0]]; p[0] += 1; return v

    def i32():
        v = struct.unpack_from(E + "i", b, p[0])[0]; p[0] += 4; return v

    def string():
        n = struct.unpack_from(E + "I", b, p[0])[0]; p[0] += 4
        if n == 0:
            return None
        s = bytes(b[p[0]:p[0] + n - 1]); p[0] += n
        return s

    def func():
        f = dict(source=string(), line=i32(), lastline=i32(), nups=u8(), nparams=u8(), vararg=u8(), maxstack=u8())
        n = i32(); f["code"] = bytes(b[p[0]:p[0] + 4 * n]); p[0] += 4 * n
        k = []
        for _ in range(i32()):
            t = u8()
            if t == 0:
                k.append((0, None))
            elif t == 1:
                k.append((1, u8()))
            elif t == 3:
                k.append((3, bytes(b[p[0]:p[0] + 8]))); p[0] += 8
            elif t == 4:
                k.append((4, string()))
            else:
                raise ValueError("bad constant type %d" % t)
        f["consts"] = k
        f["protos"] = [func() for _ in range(i32())]
        n = i32(); f["lineinfo"] = bytes(b[p[0]:p[0] + 4 * n]); p[0] += 4 * n
        f["locvars"] = [(string(), i32(), i32()) for _ in range(i32())]
        f["upvals"] = [string() for _ in range(i32())]
        return f

    top = func()
    if p[0] != len(b):
        raise ValueError("trailing data")
    top["_header"] = bytes(b[:12])
    return top


def luac_write(top):
    """Serialises function dicts back to the game's Lua 5.1 bytecode format."""
    E = HEADERS[top["_header"]]
    out = bytearray(top["_header"])

    def string(s):
        if s is None:
            out.extend(struct.pack(E + "I", 0))
        else:
            out.extend(struct.pack(E + "I", len(s) + 1)); out.extend(s); out.append(0)

    def func(f):
        string(f["source"])
        out.extend(struct.pack(E + "iiBBBB", f["line"], f["lastline"], f["nups"], f["nparams"], f["vararg"], f["maxstack"]))
        out.extend(struct.pack(E + "i", len(f["code"]) // 4)); out.extend(f["code"])
        out.extend(struct.pack(E + "i", len(f["consts"])))
        for t, v in f["consts"]:
            out.append(t)
            if t == 1:
                out.append(v)
            elif t == 3:
                out.extend(v)
            elif t == 4:
                string(v)
        out.extend(struct.pack(E + "i", len(f["protos"])))
        for q in f["protos"]:
            func(q)
        out.extend(struct.pack(E + "i", len(f["lineinfo"]) // 4)); out.extend(f["lineinfo"])
        out.extend(struct.pack(E + "i", len(f["locvars"])))
        for s, a, c in f["locvars"]:
            string(s); out.extend(struct.pack(E + "ii", a, c))
        out.extend(struct.pack(E + "i", len(f["upvals"])))
        for s in f["upvals"]:
            string(s)

    func(top)
    return bytes(out)


def _abc(op, a, b=0, c=0):
    """Encodes a Lua 5.1 iABC instruction."""
    return op | (a << 6) | (c << 14) | (b << 23)


def _abx(op, a, bx):
    """Encodes a Lua 5.1 iABx instruction."""
    return op | (a << 6) | (bx << 14)


OP_LOADBOOL, OP_SETGLOBAL, OP_RETURN, OP_CLOSURE = 2, 7, 30, 36
MARK = b"di_local_server_online_menus"          # constant added once, marks a patched script


def uses_flag(top):
    """True when any function of the script reads the global UI_IsTOGOOfflineGame."""
    def walk(f):
        return any(t == 4 and v == FLAG for t, v in f["consts"]) or any(walk(p) for p in f["protos"])
    return walk(top)


def redirect_global(top, old, new):
    """Renames every use of the global <old> (string constant) to <new> in all functions."""
    n = 0

    def walk(f):
        nonlocal n
        for i, (t, v) in enumerate(f["consts"]):
            if t == 4 and v == old:
                f["consts"][i] = (4, new)
                n += 1
        for q in f["protos"]:
            walk(q)
    walk(top)
    return n


# Our own global names: the executable registers its native UI_* functions after some
# scripts (the main menu) are loaded, which would overwrite an override with the same name.
LOCAL_NAMES = {FLAG: b"di_local_IsTOGOOfflineGame",
               b"UI_IsCloudMigrationEnabled": b"di_local_IsCloudMigrationEnabled"}


def is_patched(top):
    """True when the script already carries this tool's override."""
    return any(t == 4 and v == MARK for t, v in top["consts"])


OP_LOADK, OP_GETGLOBAL, OP_SETTABLE, OP_TEST, OP_JMP, OP_CALL = 1, 5, 9, 26, 22, 28
STARTED = b"di_local_server_did_started"
AUTOLOGIN_SCRIPTS = ("levelselectmenu.lua",)           # PC main menu
# PC main menu: show the "Save Migration" entry, the game's own Disney ID sign-in button
# (SelectItem -> UI_InitiateDisneyID). UI_IsCloudMigrationComplete stays native.
MIGRATION_SCRIPTS = ("levelselectmenu.lua",)
# Scripts that keep the native offline flag: the Gold Edition sign-in flow lives there
# ("Save Migration" entry is shown ONLY when UI_IsTOGOOfflineGame() is true, and the
# Disney ID screens then use their migration mode).
KEEP_OFFLINE_SCRIPTS = ("levelselectmenu.lua", "disney_id.lua", "disney_id_status.lua", "disney_id_creation.lua",
                        # save menus: with the flag off they upload Toy Boxes to the cloud (saveCloud /
                        # UploadUGC) and wait forever; offline they save to disk like the Gold Edition
                        "gamesave.lua", "saveloadnames.lua")


def _sbx(op, a, sbx):
    """Encodes a Lua 5.1 iAsBx instruction (signed jump offset)."""
    return op | (a << 6) | ((sbx + 131071) << 14)


def _find_method(top, method):
    """Proto assigned to <something>.<method> in the main chunk (CLOSURE Ra Pn ; SETTABLE Rx K(method) Ra)."""
    E = HEADERS[top["_header"]]
    code = [struct.unpack_from(E + "I", top["code"], 4 * i)[0] for i in range(len(top["code"]) // 4)]
    for i, ins in enumerate(code[:-1]):
        if ins & 0x3F != OP_CLOSURE:
            continue
        a, bx = (ins >> 6) & 0xFF, ins >> 14
        for nxt in code[i + 1:i + 1 + 1 + top["protos"][bx]["nups"]][-1:]:
            if nxt & 0x3F == OP_SETTABLE and (nxt >> 14) & 0x1FF == a:
                b = nxt >> 23
                if b >= 256 and top["consts"][b - 256] == (4, method):
                    return top["protos"][bx]
    return None


def inject_autologin(top, method=b"onActive"):
    """
    At the start of the main menu's <method> (the menu is on screen and has the input):
        if not STARTED then STARTED = true
            if not IsSignedIntoDisneyID(0) then UI_InitiateDisneyID() end
        end
    The Gold Edition never starts the Disney ID sign-in by itself, and its Save Migration
    sign-in ends with a sign-out. UI_InitiateDisneyID uses the credentials stored by the
    first sign-in (DisneyID::InitiateAutoLogin), or opens the Disney ID screen.
    """
    f = _find_method(top, method)
    if f is None:
        raise ValueError("method %s not found" % method.decode())
    E = HEADERS[top["_header"]]
    pack = lambda *ins: b"".join(struct.pack(E + "I", x) for x in ins)

    def k(c):
        if c not in f["consts"]:
            f["consts"].append(c)
        return f["consts"].index(c)
    ks, kq, kf = k((4, STARTED)), k((4, b"IsSignedIntoDisneyID")), k((4, b"UI_InitiateDisneyID"))
    kz = k((3, struct.pack(E + "d", 0.0)))
    r = f["nparams"]                                 # first free register at function entry
    code = pack(_abx(OP_GETGLOBAL, r, ks),          # r = STARTED
                _abc(OP_TEST, r, 0, 1),             # falsy -> skip the jump
                _sbx(OP_JMP, 0, 9),                 # already started -> skip the 9 below
                _abc(OP_LOADBOOL, r, 1, 0),         # r = true
                _abx(OP_SETGLOBAL, r, ks),          # STARTED = true
                _abx(OP_GETGLOBAL, r, kq),          # r = IsSignedIntoDisneyID
                _abx(OP_LOADK, r + 1, kz),          # r+1 = 0
                _abc(OP_CALL, r, 2, 2),             # r = IsSignedIntoDisneyID(0)
                _abc(OP_TEST, r, 0, 1),             # falsy -> skip the jump (sign in)
                _sbx(OP_JMP, 0, 2),                 # signed in -> skip the call
                _abx(OP_GETGLOBAL, r, kf),          # r = UI_InitiateDisneyID
                _abc(OP_CALL, r, 1, 1))             # r()
    n = len(code) // 4
    li = f["lineinfo"]
    first_line = struct.unpack_from(E + "i", li, 0)[0] if li else 0
    f["code"] = code + f["code"]
    if li:
        f["lineinfo"] = struct.pack(E + "i", first_line) * n + li
    f["locvars"] = [(nm, s if s == 0 else s + n, e + n) for nm, s, e in f["locvars"]]
    f["maxstack"] = max(f["maxstack"], r + 2)
    return top


OP_MOVE, OP_GETTABLE, OP_SELF = 0, 6, 11


def _const(f, c):
    """Index of constant c in function f (added if missing)."""
    if c not in f["consts"]:
        f["consts"].append(c)
    return f["consts"].index(c)


def _rk(f, c):
    """RK operand (constant index | 256) for a constant, adding it if needed."""
    i = _const(f, c)
    if i >= 256:
        raise ValueError("constant table too large for an RK operand")
    return 256 + i


def inject_did_autologin(top, user, password=b"local"):
    """
    disney_id.lua (the Disney account screen that "Save Migration" opens):
      - new method   disney_id.DiLocalAutoLogin(self): marks both fields as typed and calls
                     self:AttemptConnect(user, password) -- what the Log In button does;
      - SetLoginList (shows the login list) ends with
                     self:ScheduleTimer("ONE_SHOT", 1.0, "DiLocalAutoLogin").
    The local server does not check passwords: the user name selects the account.
    """
    E = HEADERS[top["_header"]]
    pack = lambda *ins: b"".join(struct.pack(E + "I", x) for x in ins)
    li = top["lineinfo"]
    first_line = struct.unpack_from(E + "i", li, 0)[0] if li else 0

    # the new method
    proto = dict(source=None, line=first_line, lastline=first_line, nups=0, nparams=1, vararg=0, maxstack=5,
                 code=b"", consts=[], protos=[], lineinfo=b"", locvars=[], upvals=[])
    num = lambda v: (3, struct.pack(E + "d", v))
    code = pack(_abc(OP_GETTABLE, 1, 0, _rk(proto, (4, b"DataProvider"))),
                _abc(OP_SETTABLE, 1, _rk(proto, (4, b"userNameEntered")), _rk(proto, (1, 1))),
                _abc(OP_SETTABLE, 1, _rk(proto, (4, b"passwordEntered")), _rk(proto, (1, 1))),
                _abc(OP_SELF, 1, 0, _rk(proto, (4, b"AttemptConnect"))),
                _abx(OP_LOADK, 3, _const(proto, (4, user))),
                _abx(OP_LOADK, 4, _const(proto, (4, password))),
                _abc(OP_CALL, 1, 4, 1),
                _abc(OP_RETURN, 0, 1, 0))
    proto["code"] = code
    if li:
        proto["lineinfo"] = struct.pack(E + "i", first_line) * (len(code) // 4)
    top["protos"].append(proto)
    pidx = len(top["protos"]) - 1

    # main chunk: disney_id.DiLocalAutoLogin = <closure>, before its final RETURN
    kcls, kname = _const(top, (4, b"disney_id")), _rk(top, (4, b"DiLocalAutoLogin"))
    _const(top, (4, MARK))
    ret = pack(_abc(OP_RETURN, 0, 1, 0))
    if top["code"][-4:] != ret:
        raise ValueError("disney_id main chunk does not end with RETURN")
    add = pack(_abx(OP_GETGLOBAL, 5, kcls), _abx(OP_CLOSURE, 6, pidx), _abc(OP_SETTABLE, 5, kname, 6))
    top["code"] = top["code"][:-4] + add + ret
    if li:
        top["lineinfo"] = li[:-4] + li[-4:] * 4
    top["maxstack"] = max(top["maxstack"], 7)

    # SetLoginList: schedule the automatic login before its final RETURN
    f = _find_method(top, b"SetLoginList")
    if f is None:
        raise ValueError("SetLoginList not found")
    if f["code"][-4:] != ret:
        raise ValueError("SetLoginList does not end with RETURN")
    add = pack(_abc(OP_SELF, 1, 0, _rk(f, (4, b"ScheduleTimer"))),
               _abx(OP_LOADK, 3, _const(f, (4, b"ONE_SHOT"))),
               _abx(OP_LOADK, 4, _const(f, num(1.0))),
               _abx(OP_LOADK, 5, _const(f, (4, b"DiLocalAutoLogin"))),
               _abc(OP_CALL, 1, 5, 1))
    f["code"] = f["code"][:-4] + add + ret
    if f["lineinfo"]:
        f["lineinfo"] = f["lineinfo"][:-4] + f["lineinfo"][-4:] * (len(add) // 4 + 1)
    f["maxstack"] = max(f["maxstack"], 6)
    return top


UI_FUNCS = os.path.join(HERE, "dinet", "ui_funcs.luac")    # compiled by compile_ui_lua.py
UI_FUNCS_SCRIPTS = {"onlinemp_options.lua": b"onlinemp_options", "friends_list_u.lua": b"friends_list_u",
                    "friend_invites_u.lua": b"friend_invites_u", "player_hud_combined.lua": b"player_hud_combined",
                    "levelselectmenu.lua": b"levelselectmenu", "pausemenu.lua": b"pausemenu"}
# natives replaced in ONE script by a function returning a constant (named di_local_<native>, so the exe
# registering its natives later does not undo it):
#   SetOnInviteScreen     - removed XMPP client (no-op)
#   IsMultiplayerAllowed  - false in the Gold Edition: hides the pause menu's online multiplayer entry
UI_FUNCS_NO_OPS = {"friend_invites_u.lua": ((b"SetOnInviteScreen", False),),
                   "pausemenu.lua": ((b"IsMultiplayerAllowed", True),)}
OP_FORLOOP, OP_FORPREP = 31, 32


def insert_code(f, pos, ins):
    """Inserts instructions (ints) before index pos of function f, fixing jumps, line info and locals."""
    E = ">"
    code = [struct.unpack_from(E + "I", f["code"], 4 * i)[0] for i in range(len(f["code"]) // 4)]
    n = len(ins)
    out = []
    for i, x in enumerate(code):
        if x & 0x3F in (OP_JMP, OP_FORLOOP, OP_FORPREP):
            target = i + 1 + (x >> 14) - 131071
            ni = i + n if i >= pos else i
            nt = target + n if target > pos else target          # a jump to pos lands on the new code
            x = (x & 0x3FFF) | ((nt - ni - 1 + 131071) << 14)
        out.append(x)
    out[pos:pos] = ins
    f["code"] = b"".join(struct.pack(E + "I", x) for x in out)
    li = f["lineinfo"]
    if li:
        line = li[4 * max(pos - 1, 0): 4 * max(pos - 1, 0) + 4]
        f["lineinfo"] = li[:4 * pos] + line * n + li[4 * pos:]
    f["locvars"] = [(nm, s + n if s > pos else s, e + n if e >= pos else e) for nm, s, e in f["locvars"]]


def inject_ui_funcs(top, script):
    """
    Adds the compiled dinet/ui_funcs.lua as a function of this script and calls it with the script name
    right before the main chunk's first RETURN (the class is defined by then):  ui_funcs("<script>")
    """
    if top["_header"][6] != 0:
        raise ValueError("ui_funcs.luac is big-endian; this script is not")
    with open(UI_FUNCS, "rb") as fh:
        funcs = luac_parse(fh.read())
    E = HEADERS[top["_header"]]
    code = [struct.unpack_from(E + "I", top["code"], 4 * i)[0] for i in range(len(top["code"]) // 4)]
    pos = next(i for i, x in enumerate(code) if x & 0x3F == OP_RETURN)
    funcs.pop("_header", None)
    top["protos"].append(funcs)
    pidx = len(top["protos"]) - 1
    r = top["maxstack"]                              # registers above everything the chunk uses
    ks = _const(top, (4, script))
    _const(top, (4, MARK))
    insert_code(top, pos, [_abx(OP_CLOSURE, r, pidx), _abx(OP_LOADK, r + 1, ks), _abc(OP_CALL, r, 2, 1)])
    top["maxstack"] = r + 2
    if top["maxstack"] > 250:
        raise ValueError("register limit")
    return top


def enable_mp_buttons(top):
    """
    onlinemp_options.lua (online multiplayer menu): show the console buttons the PC build hides.
      - BuildList: "if build == TOYBOX_TOGO and platform == TARGET_WIN32 then skip View Friends": that jump
        is turned into a no-op;
      - IsMultiplayerAllowed / IsInviteAllowed (natives, false in the Gold Edition) gate View Friends and
        Invites: this script calls di_local_* versions that return true.
    The buttons then run the game's own code (UI_ViewFriends, friend_invites_u...), nothing is added.
    """
    E = HEADERS[top["_header"]]
    f = _find_method(top, b"BuildList")
    if f is None:
        raise ValueError("BuildList not found")
    code = [struct.unpack_from(E + "I", f["code"], 4 * i)[0] for i in range(len(f["code"]) // 4)]
    k = lambda ins, sh: f["consts"][((ins >> sh) & 0x1FF) - 256] if ((ins >> sh) & 0x1FF) >= 256 else None
    hit = None
    for i in range(len(code) - 2):
        a, b, c = code[i], code[i + 1], code[i + 2]
        if (a & 0x3F == OP_GETGLOBAL and f["consts"][a >> 14] == (4, b"platform")
                and b & 0x3F == 23 and (b >> 6) & 0xFF == 1 and k(b, 14) == (4, b"TARGET_WIN32")
                and c & 0x3F == OP_JMP and i >= 3 and k(code[i - 2], 14) == (4, b"TOYBOX_TOGO")):
            hit = i + 2
    if hit is None:
        raise ValueError("PC 'View Friends' check not found in BuildList")
    code[hit] = _sbx(OP_JMP, 0, 0)
    f["code"] = b"".join(struct.pack(E + "I", x) for x in code)
    for name in (b"IsMultiplayerAllowed", b"IsInviteAllowed"):
        local = b"di_local_" + name
        redirect_global(top, name, local)
        top = inject_override(top, local, True, mark=(name == b"IsMultiplayerAllowed"))
    return top


# Screens that run dinet.lua (friends, invites, sign-in text) when they open; see dinet/dinet.lua.
BOOTSTRAP_SCRIPTS = {"disney_id.lua": b"disney_id", "onlinemp_options.lua": b"onlinemp_options",
                     "friends_list_u.lua": b"friends_list_u", "friend_invites_u.lua": b"friend_invites_u"}


def inject_bootstrap(top, script, method=b"onEnter"):
    """
    At the start of <method>(self, ...):
        local f = loadstring(Translator_TranslateString("#dinet:lua"), "=dinet")
        if f then pcall(f, self, "<script>") end
    dinet_bk.dll answers that key with the text of dinet.lua; without the DLL the key comes back
    untranslated, does not compile, and nothing happens. The chunk name must be short: by default
    it is the whole source, and the engine sprintf's "function: <chunk name>" into a stack buffer.
    """
    f = _find_method(top, method)
    if f is None:
        raise ValueError("method %s not found" % method.decode())
    E = HEADERS[top["_header"]]
    pack = lambda *ins: b"".join(struct.pack(E + "I", x) for x in ins)
    kp, kl, kt = _const(f, (4, b"pcall")), _const(f, (4, b"loadstring")), _const(f, (4, b"Translator_TranslateString"))
    kk, ks, kn = _const(f, (4, b"#dinet:lua")), _const(f, (4, script)), _const(f, (4, b"=dinet"))
    r = f["nparams"]                                 # first free register at function entry
    code = pack(_abx(OP_GETGLOBAL, r, kp),          # r   = pcall
                _abx(OP_GETGLOBAL, r + 1, kl),      # r+1 = loadstring
                _abx(OP_GETGLOBAL, r + 2, kt),      # r+2 = Translator_TranslateString
                _abx(OP_LOADK, r + 3, kk),          # r+3 = "#dinet:lua"
                _abc(OP_CALL, r + 2, 2, 2),         # r+2 = text
                _abx(OP_LOADK, r + 3, kn),          # r+3 = "=dinet" (chunk name)
                _abc(OP_CALL, r + 1, 3, 2),         # r+1 = loadstring(text, "=dinet")   (nil on error)
                _abc(OP_TEST, r + 1, 0, 0),         # truthy -> skip the jump
                _sbx(OP_JMP, 0, 3),                 # nil -> skip the call
                _abc(OP_MOVE, r + 2, 0),            # r+2 = self
                _abx(OP_LOADK, r + 3, ks),          # r+3 = script name
                _abc(OP_CALL, r, 4, 1))             # pcall(f, self, name)
    n = len(code) // 4
    li = f["lineinfo"]
    first_line = struct.unpack_from(E + "i", li, 0)[0] if li else 0
    f["code"] = code + f["code"]
    if li:
        f["lineinfo"] = struct.pack(E + "i", first_line) * n + li
    f["locvars"] = [(nm, s if s == 0 else s + n, e + n) for nm, s, e in f["locvars"]]
    f["maxstack"] = max(f["maxstack"], r + 4)
    _const(top, (4, MARK))
    return top


def inject_override(top, name=FLAG, value=False, mark=True):
    """main chunk: R0 = closure(function() return <value> end); _G[name] = R0  (prepended)."""
    E = HEADERS[top["_header"]]
    pack = lambda *ins: b"".join(struct.pack(E + "I", x) for x in ins)
    li = top["lineinfo"]
    first_line = struct.unpack_from(E + "i", li, 0)[0] if li else 0
    proto = dict(source=None, line=first_line, lastline=first_line, nups=0, nparams=0, vararg=0, maxstack=2,
                 code=pack(_abc(OP_LOADBOOL, 0, 1 if value else 0, 0), _abc(OP_RETURN, 0, 2, 0),
                           _abc(OP_RETURN, 0, 1, 0)),
                 consts=[], protos=[],
                 lineinfo=struct.pack(E + "iii", first_line, first_line, first_line) if li else b"",
                 locvars=[], upvals=[])
    top["protos"].append(proto)
    pidx = len(top["protos"]) - 1
    top["consts"].append((4, name))
    kidx = len(top["consts"]) - 1
    if mark:
        top["consts"].append((4, MARK))
    if kidx >= 1 << 18 or pidx >= 1 << 18:
        raise ValueError("too many constants/protos")
    top["code"] = pack(_abx(OP_CLOSURE, 0, pidx), _abx(OP_SETGLOBAL, 0, kidx)) + top["code"]
    if li:
        top["lineinfo"] = struct.pack(E + "ii", first_line, first_line) + li
    top["locvars"] = [(n, s + 2, e + 2) for n, s, e in top["locvars"]]
    top["maxstack"] = max(top["maxstack"], 2)
    return top


# ---------------------------------------------------------------- container
def load_ditool(path):
    """Imports tool/ditool.py from a path (container unpack / repack / verify)."""
    spec = importlib.util.spec_from_file_location("ditool", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main(argv=None):
    """Command line entry point: patches (or restores) the menus archive presentation.zip."""
    ap = argparse.ArgumentParser(description="Re-enable the online menus hidden in the Gold Edition")
    ap.add_argument("--game", required=True, help="game folder")
    ap.add_argument("--ditool", default=DEFAULT_DITOOL, help="path of tool/ditool.py")
    ap.add_argument("--restore", action="store_true", help="restore the original presentation.zip")
    ap.add_argument("--autologin", action="store_true",
                    help="sign in to Disney ID by itself when the main menu is shown (once per game start, only "
                         "when not signed in): what pressing Save Migration does. In the Gold Edition every "
                         "sign-in runs its cloud migration (the server's saves replace the local ones) and then "
                         "signs out; dinet_bk.dll keeps the session (block_signout) and backs the saves up "
                         "(protect). Use with --login-user so the Disney ID screen presses Log In by itself")
    ap.add_argument("--no-signin-button", dest="signin_button", action="store_false",
                    help="do not show the Save Migration / Disney ID entry in the main menu")
    ap.add_argument("--login-user", default="",
                    help="Save Migration signs in by itself with this local-server user name "
                         "(the server does not check passwords)")
    ap.add_argument("--login-pass", default="local", help="password sent with --login-user (ignored by the server)")
    ap.add_argument("--mp-buttons", action="store_true",
                    help="show the hidden View Friends / Invites buttons of the online multiplayer menu "
                         "(they run the game's own code; nothing is answered for them)")
    ap.add_argument("--friends-list", action="store_true",
                    help="'Friends' button in the online multiplayer menu: the friends list shows the players "
                         "connected to the server (compiled dinet/ui_funcs.luac, needs dinet_bk.dll)")
    ap.add_argument("--friends", action="store_true",
                    help="EXPERIMENTAL (crashed the multiplayer menu): make the Disney ID / multiplayer / "
                         "friends / invites screens run dinet.lua")
    ap.add_argument("--out", default="",
                    help="write the patched presentation.zip here (e.g. a CrabeLoader mod folder) and leave "
                         "the game's own file untouched")
    a = ap.parse_args(argv)
    zp = os.path.join(a.game, "assets", "presentation", "presentation.zip")
    orig = zp + ".orig"
    if a.out:                                         # start from the unmodified file, write elsewhere
        if not os.path.exists(orig):
            orig = zp
        zp = a.out
        os.makedirs(os.path.dirname(os.path.abspath(zp)), exist_ok=True)
    if a.restore:
        if not os.path.exists(orig):
            raise SystemExit("No backup %s" % orig)
        shutil.copy2(orig, zp)
        print("Restored", zp)
        return
    if not os.path.exists(orig) and not a.out:
        shutil.copy2(zp, orig)
        print("Backup:", orig)
    dt = load_ditool(a.ditool)
    work = tempfile.mkdtemp(prefix="di_ui_")
    try:
        dt.unpack(orig, work)                         # always start from the original
        patched = []
        for name in sorted(os.listdir(work)):
            fp = os.path.join(work, name)
            if not name.endswith(".lua"):
                continue
            with open(fp, "rb") as f:
                b = f.read()
            if b[:4] != b"\x1bLua":
                continue
            top = luac_parse(b)
            low = name.lower()
            want_flag = uses_flag(top) and low not in KEEP_OFFLINE_SCRIPTS
            want_mig = a.signin_button and low in MIGRATION_SCRIPTS
            want_login = bool(a.login_user) and low == "disney_id.lua"
            want_boot = a.friends and low in BOOTSTRAP_SCRIPTS
            want_mpb = a.mp_buttons and low == "onlinemp_options.lua"
            want_fl = a.friends_list and low in UI_FUNCS_SCRIPTS
            if not (want_flag or want_mig or want_login or want_boot or want_mpb or want_fl) or is_patched(top):
                continue
            if want_fl:
                top = inject_ui_funcs(top, UI_FUNCS_SCRIPTS[low])
                for native, value in UI_FUNCS_NO_OPS.get(low, ()):
                    redirect_global(top, native, b"di_local_" + native)
                    top = inject_override(top, b"di_local_" + native, value, mark=False)
                name += " (+friends / invites)"
            if want_mpb:
                top = enable_mp_buttons(top)
                name += " (View Friends / Invites buttons)"
            if want_boot:
                top = inject_bootstrap(top, BOOTSTRAP_SCRIPTS[low])
                name += " (+dinet.lua)"
            if want_login:
                top = inject_did_autologin(top, a.login_user.encode("utf-8"), a.login_pass.encode("utf-8"))
                name += " (automatic Log In as %s)" % a.login_user
            if want_flag:
                redirect_global(top, FLAG, LOCAL_NAMES[FLAG])
                top = inject_override(top, LOCAL_NAMES[FLAG], False)
            if want_mig:
                cm = b"UI_IsCloudMigrationEnabled"
                redirect_global(top, cm, LOCAL_NAMES[cm])
                top = inject_override(top, LOCAL_NAMES[cm], True, mark=not want_flag)
                name += " (sign-in button only)" if not want_flag else " (+sign-in button)"
            if a.autologin and low in AUTOLOGIN_SCRIPTS:
                top = inject_autologin(top)
                name += " (+sign-in)"
            nb = luac_write(top)
            chk = luac_parse(nb)                     # must parse back
            assert is_patched(chk) and len(chk["code"]) == len(top["code"])
            with open(fp, "wb") as f:
                f.write(nb)
            patched.append(name)
        if not patched:
            raise SystemExit("No script uses %s: unsupported version." % FLAG.decode())
        tmp_zip = zp + ".new"
        dt.repack(work, tmp_zip)
        dt.verify(tmp_zip)
        os.replace(tmp_zip, zp)
        print("Patched %d scripts: %s" % (len(patched), ", ".join(patched)))
        print("Written:", zp)
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
