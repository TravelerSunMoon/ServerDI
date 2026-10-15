# -*- coding: utf-8 -*-
"""
Zone ids of the game (Disney Infinity 3.0 Gold, Steam build 1522802).

The game numbers its levels ("zones") when it starts: id = 0x401 + (PlaysetUniqueID << 8) + n, n = the order
in which it meets the zones of that playset in ZoneList.Records.Zones (lua_next order, which changes when a mod
adds zones). Network play sends those ids, so two PCs must agree on them; dinet_bk.dll gives every zone of
the unmodified game its id from dinet/zones_vanilla.h.

    py read_zones.py --pid <DisneyInfinity3.exe pid> --out zones.txt
        reads the table of the RUNNING game (read only): "id TAB name" lines
    py read_zones.py --header
        writes dinet/zones_vanilla.h from dinet/zones_vanilla.txt (a dump of the unmodified game)
"""
import argparse
import ctypes
import os
import struct

HERE = os.path.dirname(os.path.abspath(__file__))
TXT = os.path.join(HERE, "dinet", "zones_vanilla.txt")
HDR = os.path.join(HERE, "dinet", "zones_vanilla.h")
VA_ZONE_MGR = 0x0225F548      # ZoneMgr*; +0x278 hash map id -> Zone* (buckets +0x14, count +0x1C,
ZONE_MAP, ZONE_NAME = 0x278, 0x2C   # node {next, hash, id, Zone*}); Zone +0x2C = name


def read_running(pid):
    """Reads (id, name) of every zone from a running game process (read only)."""
    import ctypes.wintypes as W
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    h = k32.OpenProcess(0x0010 | 0x0400, False, pid)          # PROCESS_VM_READ | QUERY_INFORMATION
    if not h:
        raise SystemExit("cannot open process %d (error %d)" % (pid, ctypes.get_last_error()))
    mods = (ctypes.c_void_p * 1024)()
    need = W.DWORD()
    psapi.EnumProcessModulesEx(h, mods, ctypes.sizeof(mods), ctypes.byref(need), 0x01)
    base = mods[0]

    def rd(addr, n):
        buf = ctypes.create_string_buffer(n)
        got = ctypes.c_size_t()
        if not k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, n, ctypes.byref(got)):
            raise OSError("cannot read %08X" % addr)
        return buf.raw

    u32 = lambda a: struct.unpack("<I", rd(a, 4))[0]
    zm = u32(VA_ZONE_MGR - 0x400000 + base)
    buckets, count = u32(zm + ZONE_MAP + 0x14), u32(zm + ZONE_MAP + 0x1C)
    rows = []
    for i in range(count):
        n = u32(buckets + 4 * i)
        while n:
            zone = u32(n + 0xC)
            rows.append((u32(n + 8), rd(u32(zone + ZONE_NAME), 96).split(b"\0")[0].decode("latin1")))
            n = u32(n)
    k32.CloseHandle(h)
    return sorted(rows)


def write_header():
    """Writes dinet/zones_vanilla.h from dinet/zones_vanilla.txt."""
    rows = []
    with open(TXT, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                i, name = line.rstrip("\n").split("\t")
                rows.append((int(i, 16), name))
    with open(HDR, "w", encoding="utf-8", newline="\n") as f:
        f.write("/* Zone ids of the unmodified game (Steam build 1522802), read from the running game with\n"
                " * read_zones.py; regenerate with: py read_zones.py --header */\n")
        f.write("static const struct { const char* name; WORD id; } kVanillaZones[] = {\n")
        for i, name in rows:
            f.write('    { "%s", 0x%X },\n' % (name, i))
        f.write("};\n")
    print("%d zones -> %s" % (len(rows), HDR))


def main():
    """Command line entry point (--pid / --out, or --header)."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--pid", type=int, help="process id of the running DisneyInfinity3.exe")
    ap.add_argument("--out", default="", help="write the table here (default: print it)")
    ap.add_argument("--header", action="store_true", help="write dinet/zones_vanilla.h")
    a = ap.parse_args()
    if a.header:
        return write_header()
    if not a.pid:
        ap.error("--pid or --header")
    rows = read_running(a.pid)
    text = "".join("%X\t%s\n" % r for r in rows)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
        print("%d zones -> %s" % (len(rows), a.out))
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
