# -*- coding: utf-8 -*-
r"""
Installs the online client as a CrabeLoader mod: <game>\mods\dinet_online.

    python install_crabe_mod.py --game "<path to Disney Infinity 3.0 Gold Edition>"
                                --base http://127.0.0.1:8085 [--login-user NAME]
    python install_crabe_mod.py --game "..." --remove

What it does (CrabeLoader must already be installed: bink2w32.dll + bink2w32_orig.dll):
  1. DisneyInfinity3_local.exe: a copy of the game whose Disney urls point to the server (patch_exe.py
     WITHOUT --dinet, so the copy still loads bink2w32.dll = CrabeLoader). The original executable is never
     modified.
  2. mods\dinet_online: mod.json, main.lua (loads dinet_bk.dll from this folder), dinet_bk.dll and di_net.ini,
     taken from the client folder (crabe_mod\dinet_online here, or ..\ServerDI_Client\dinet_online).
  3. mods\dinet_online\presentation\presentation.zip: the online menus, built from YOUR game's own
     presentation.zip by patch_ui.py; CrabeLoader's virtual file system serves it instead of
     assets\presentation\presentation.zip, which is left untouched.
--remove deletes the mod folder and the exe copy.
"""
import argparse
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import patch_exe   # noqa: E402
import patch_ui    # noqa: E402

MOD_ID = "dinet_online"
# Server url written to di_net.ini and into the exe copy when --base is not given.
DEFAULT_BASE = "http://127.0.0.1:8085"
# The client mod: crabe_mod\dinet_online in the development copy, or its own folder next to this
# repository (ServerDI_Client\dinet_online).
DEFAULT_CLIENT = next((p for p in (os.path.join(HERE, "crabe_mod", MOD_ID),
                                   os.path.join(os.path.dirname(HERE), "ServerDI_Client", MOD_ID))
                       if os.path.isdir(p)), os.path.join(os.path.dirname(HERE), "ServerDI_Client", MOD_ID))


def main(argv=None):
    """Command line entry point: install or (--remove) uninstall the client mod in a game folder."""
    ap = argparse.ArgumentParser(description="Install the online client as a CrabeLoader mod")
    ap.add_argument("--game", required=True, help="game folder (contains DisneyInfinity3.exe)")
    ap.add_argument("--base", default=DEFAULT_BASE, help="url of the server (max 22 chars)")
    ap.add_argument("--client", default=DEFAULT_CLIENT,
                    help="folder with the client mod files (main.lua, mod.json, dinet_bk.dll)")
    ap.add_argument("--login-user", default=os.environ.get("USERNAME", ""),
                    help="Save Migration signs in by itself with this server user name")
    ap.add_argument("--ditool", default=patch_ui.DEFAULT_DITOOL)
    ap.add_argument("--remove", action="store_true", help="remove the mod and the exe copy")
    a = ap.parse_args(argv)
    game = a.game
    mod = os.path.join(game, "mods", MOD_ID)
    if a.remove:
        shutil.rmtree(mod, ignore_errors=True)
        patch_exe.main(["--game", game, "--restore"])
        print("Removed", mod)
        return

    if not os.path.exists(os.path.join(game, "DisneyInfinity3.exe")):
        raise SystemExit("DisneyInfinity3.exe not found in %s" % game)
    if not os.path.exists(os.path.join(game, "bink2w32_orig.dll")):
        raise SystemExit("CrabeLoader is not installed in this game folder (no bink2w32_orig.dll): run "
                         "CrabeInstaller.exe first (https://github.com/LucasLhomme/CrabeLoader/releases).")
    dll = os.path.join(a.client, "dinet_bk.dll")
    if not os.path.exists(dll):
        dll = patch_exe.DINET_DLL                       # a freshly built one in dinet\
    if not os.path.exists(dll):
        raise SystemExit("Missing dinet_bk.dll (build it with dinet\\build.bat or point --client at the mod)")
    for f in ("mod.json", "main.lua"):
        if not os.path.exists(os.path.join(a.client, f)):
            raise SystemExit("Missing %s in the client folder %s" % (f, a.client))

    print("[1/3] DisneyInfinity3_local.exe -> %s (loads CrabeLoader)" % a.base)
    patch_exe.main(["--game", game, "--base", a.base])

    print("[2/3] mod folder", mod)
    os.makedirs(mod, exist_ok=True)
    for f in ("mod.json", "main.lua"):
        shutil.copy2(os.path.join(a.client, f), os.path.join(mod, f))
    if os.path.exists(os.path.join(a.client, "di_net.ini")) and not os.path.exists(os.path.join(mod, "di_net.ini")):
        shutil.copy2(os.path.join(a.client, "di_net.ini"), os.path.join(mod, "di_net.ini"))   # defaults
    shutil.copy2(dll, os.path.join(mod, "dinet_bk.dll"))
    patch_exe.write_dinet_ini(os.path.join(mod, "di_net.ini"), a.base)

    print("[3/3] online menus -> mods/%s/presentation/presentation.zip" % MOD_ID)
    args = ["--game", game, "--ditool", a.ditool, "--friends-list",
            "--out", os.path.join(mod, "presentation", "presentation.zip")]
    if a.login_user:
        args += ["--login-user", a.login_user]
    patch_ui.main(args)
    if os.path.exists(os.path.join(game, "assets", "presentation", "presentation.zip.orig")):
        print("Note: assets/presentation/presentation.zip was patched in place by an older install; "
              "the mod's copy is used while CrabeLoader runs.")
    if os.path.isdir(os.path.join(game, "mods", "monstersu_port")):
        print("WARNING: mods\\monstersu_port also serves presentation.zip and wins over this mod. Reinstall "
              "that mod AFTER this one so both sets of menu changes are merged.")
    print("Done. Start the game with DisneyInfinity3_local.exe (Steam open). Log: mods/%s/di_net.log" % MOD_ID)


if __name__ == "__main__":
    main()
