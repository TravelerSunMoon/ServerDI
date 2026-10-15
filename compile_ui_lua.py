# -*- coding: utf-8 -*-
"""
Compiles dinet/ui_funcs.lua into dinet/ui_funcs.luac in the game's bytecode format (Lua 5.1,
BIG-endian, 4-byte size_t), so patch_ui.py can embed it into the menu scripts. Nothing is compiled
or loaded with loadstring inside the game.

Only needed after editing ui_funcs.lua (development PC): pip install --user lupa, then
    py compile_ui_lua.py
"""
import os
import struct
import sys

import lupa.lua51 as lua51

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import patch_ui  # noqa: E402  (luac_write and the game's header)

SRC = os.path.join(HERE, "dinet", "ui_funcs.lua")
OUT = os.path.join(HERE, "dinet", "ui_funcs.luac")
GAME_HEADER = b"\x1bLuaQ\x00\x00\x04\x04\x04\x08\x00"           # big-endian, size_t 4


def parse_dump(b):
    """Reads a Lua 5.1 dump of any endianness / size_t size into patch_ui's proto dicts."""
    if b[:5] != b"\x1bLuaQ" or b[5] != 0:
        raise ValueError("not a Lua 5.1 dump")
    E = "<" if b[6] == 1 else ">"
    si, ss, sn = b[7], b[8], b[10]
    if si != 4 or b[9] != 4 or sn != 8 or b[11] != 0:
        raise ValueError("unsupported sizes in the dump header")
    sfmt = {4: "I", 8: "Q"}[ss]
    p = [12]

    def u8():
        v = b[p[0]]; p[0] += 1; return v

    def i32():
        v = struct.unpack_from(E + "i", b, p[0])[0]; p[0] += 4; return v

    def string():
        n = struct.unpack_from(E + sfmt, b, p[0])[0]; p[0] += ss
        if n == 0:
            return None
        s = bytes(b[p[0]:p[0] + n - 1]); p[0] += n
        return s

    def func():
        f = dict(source=string(), line=i32(), lastline=i32(), nups=u8(), nparams=u8(), vararg=u8(), maxstack=u8())
        n = i32()
        f["code"] = b"".join(struct.pack(">I", struct.unpack_from(E + "I", b, p[0] + 4 * i)[0]) for i in range(n))
        p[0] += 4 * n
        k = []
        for _ in range(i32()):
            t = u8()
            if t == 0:
                k.append((0, None))
            elif t == 1:
                k.append((1, u8()))
            elif t == 3:
                d = struct.unpack_from(E + "d", b, p[0])[0]; p[0] += 8
                k.append((3, struct.pack(">d", d)))
            elif t == 4:
                k.append((4, string()))
            else:
                raise ValueError("bad constant type %d" % t)
        f["consts"] = k
        f["protos"] = [func() for _ in range(i32())]
        n = i32()
        f["lineinfo"] = b"".join(struct.pack(">i", struct.unpack_from(E + "i", b, p[0] + 4 * i)[0]) for i in range(n))
        p[0] += 4 * n
        f["locvars"] = [(string(), i32(), i32()) for _ in range(i32())]
        f["upvals"] = [string() for _ in range(i32())]
        f["source"] = None                          # no file names inside the game
        return f

    top = func()
    if p[0] != len(b):
        raise ValueError("trailing data")
    top["_header"] = GAME_HEADER
    return top


def main():
    """Compiles dinet/ui_funcs.lua and writes dinet/ui_funcs.luac in the game's format."""
    lua = lua51.LuaRuntime(encoding=None)
    with open(SRC, "rb") as fh:
        src = fh.read()
    load = lua.eval("function(s) local f, e = loadstring(s, '=ui_funcs'); if not f then error(e) end "
                    "return string.dump(f) end")
    dump = load(src)
    top = parse_dump(dump)
    if top["nups"] != 0:
        raise SystemExit("ui_funcs.lua main chunk must not have upvalues")
    out = patch_ui.luac_write(top)
    patch_ui.luac_parse(out)                        # must read back with the game's format
    with open(OUT, "wb") as fh:
        fh.write(out)
    print("Compiled %s -> %s (%d bytes, %d functions)" % (SRC, OUT, len(out), 1 + count(top)))


def count(f):
    """Number of nested functions below a function dict."""
    return sum(1 + count(q) for q in f["protos"])


if __name__ == "__main__":
    main()
