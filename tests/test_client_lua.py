# -*- coding: utf-8 -*-
"""
Runs the client mod's main.lua (chat part) in Lua 5.1 with fake CrabeLoader, game and dinet_bk.dll APIs.

    pip install lupa
    python -m unittest tests.test_client_lua -v

Skipped when lupa is not installed. It checks the logic only: key handling, sending, receiving, bubble
placement and that drawing runs; how it looks in the real game must be checked in the game.
"""
import os
import unittest

try:
    import lupa.lua51 as lua51
except ImportError:                                        # pragma: no cover
    lua51 = None

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN_LUA = next(p for p in (os.path.join(HERE, "..", "crabe_mod", "dinet_online", "main.lua"),
                            os.path.join(HERE, "..", "..", "ServerDI_Client", "dinet_online", "main.lua"))
                if os.path.exists(p))

FAKES = r"""
calls = { sent = {}, captured = {}, draws = 0, logs = {} }
inbox = ""
clock, world = 0, "TBX_Test"
local handlers = {}
Crabe = {
  log = function(m) calls.logs[#calls.logs + 1] = m end,
  hostPlayer = function() return 0 end,
  Memory = {},
  Events = { on = function(name, fn) handlers[name] = fn end },
  Input = { captureKeys = function(keys) calls.captured[#calls.captured + 1] = keys and #keys or 0 end,
            capturePad = function(on) pad = on end },
  ImGui = {
    GetDisplaySize = function() return 1920, 1080 end,
    DrawText = function() calls.draws = calls.draws + 1 end,
    DrawRect = function(x, y, w, h, color, rounding, thickness)
      calls.draws = calls.draws + 1
      if thickness == 2 then calls.border = color % 16777216 end      -- rgb of the outline
    end,
    DrawCircle = function() calls.draws = calls.draws + 1 end,
  },
  Camera = { GetPosition = function() return 0, 3, -6 end },
  Mod = { register = function(def) mod = def end },
  Hooks = { patchChunk = function(match, src) calls.patched = (calls.patched or 0) + 1; patch_src = src end },
}
editors = 0
ModeSelect = { onEnter = function(self) return "entered" end }          -- a Toy Box editor screen class
Game = {
  ListPlayers = function() return { { id = 0, isLocal = true }, { id = 1, isLocal = false } } end,
  GetAvatarHandle = function(id) return 100 + id end,
  ActorPosition = function(h) if h == 100 then return 0, 0, 0 end return 2, 0, 3 end,
}
typing, pad = false, false
UI_GetGamerTag = function(id) if id == 0 then return "MeTag" end return "FriendTag" end
UI_CurrentWorldName = function() return world end
playset, ini = "", {}
UI_GetZoneMgrString = function(zone, key) if zone == "<current>" and key == "PlaysetProperties" then return playset end end
function rgb(r, g, b) return (b * 256 + g) * 256 + r end
System_TimerSecPausable = function() return clock end
Translator_TranslateString = function(key)
  local cmd = string.sub(key, 8)
  if cmd == "chatcfg" then return "1\t55\t2.2\t0\t8\t0" end
  if cmd == "editor" then return editors > 0 and "1" or "0" end
  if string.sub(cmd, 1, 4) == "ini:" then return ini[string.sub(cmd, 5)] or "" end
  if string.sub(cmd, 1, 7) == "typing:" then typing = string.sub(cmd, 8) == "1" return "ok" end
  if string.sub(cmd, 1, 7) == "editor:" then
    local op = string.sub(cmd, 8, 8)
    if op == "+" then editors = editors + 1 elseif op == "-" then editors = math.max(0, editors - 1)
    else editors = 0 end
    return "ok"
  end
  if cmd == "me" then return "Me" end
  if cmd == "chat" then local r = inbox; inbox = ""; return r end
  if string.sub(cmd, 1, 8) == "chatkey:" then
    local vk = tonumber(string.sub(cmd, 9))
    if vk >= 0x41 and vk <= 0x5A then return string.char(vk + 32) end
    if vk == 0x20 then return " " end
    if vk == 0xBA then return "\195\177" end         -- a key that types n with tilde (UTF-8)
    return ""
  end
  if string.sub(cmd, 1, 9) == "chatsend:" then calls.sent[#calls.sent + 1] = string.sub(cmd, 10) return "ok" end
  return key
end
function key(vk, rep) handlers.keyDown(vk, rep or false) end
function frame() clock = clock + 1 / 60; mod.onUpdate(1 / 60); mod.onDraw() end
"""


@unittest.skipIf(lua51 is None, "lupa is not installed")
class ChatLuaTest(unittest.TestCase):
    def setUp(self):
        self.lua = lua51.LuaRuntime(encoding=None)
        self.lua.execute(FAKES.encode("utf-8"))
        with open(MAIN_LUA, "rb") as f:
            src = f.read()
        fn = self.lua.eval("function(s) local f, e = loadstring(s, '=main.lua'); if not f then error(e) end "
                           "return f end")(src)
        fn()
        self.g = self.lua.globals()
        for _ in range(5):
            self.lua.execute(b"frame()")

    def run_lua(self, code):
        self.lua.execute(code.encode("utf-8"))

    def val(self, expr):
        return self.lua.eval(expr.encode("utf-8"))

    def test_type_and_send(self):
        self.run_lua("key(0x54)")                                       # T opens the chat line
        self.assertEqual(self.val("calls.captured[#calls.captured]"), 0xFE - 0x08 + 1)
        self.run_lua("key(0x48) key(0x4F) key(0x4C) key(0x41) key(0x41) key(0x08) key(0xBA)")  # h o l a a <- n~
        self.run_lua("key(0x0D) frame()")
        self.assertEqual(self.val("calls.sent[1]"), "MeTag\thola\xc3\xb1".encode("latin-1"))
        self.assertEqual(self.val("calls.captured[#calls.captured]"), 0)   # keys given back
        self.assertEqual(self.val("#calls.sent"), 1)

    def test_escape_cancels_and_t_types_once_open(self):
        self.run_lua("key(0x54) key(0x54) key(0x1B) frame()")
        self.assertEqual(self.val("#calls.sent"), 0)
        self.run_lua("key(0x54) key(0x0D) frame()")                    # empty line: nothing sent
        self.assertEqual(self.val("#calls.sent"), 0)

    def test_no_chat_when_paused_or_in_menu(self):
        self.run_lua("for i = 1, 30 do mod.onUpdate(1 / 60) end key(0x54) key(0x41) key(0x0D)")  # clock stopped
        self.assertEqual(self.val("#calls.sent"), 0)
        self.run_lua("for i = 1, 5 do frame() end world = 'frontend' key(0x54) key(0x41) key(0x0D)")
        self.assertEqual(self.val("#calls.sent"), 0)

    def test_no_chat_in_toybox_editor(self):
        self.assertEqual(self.val("calls.patched"), 2)                 # editor scripts hooked when they load
        self.assertEqual(self.val("ModeSelect.onEnter({})"), b"entered")   # wrapped, original still runs
        self.run_lua("frame() key(0x54) key(0x41) key(0x0D) frame()")
        self.assertEqual(self.val("#calls.sent"), 0)                   # editor open: T does nothing
        self.run_lua("key(0x54) ModeSelect.onEnter({}) frame()")       # editor opening closes the line
        self.run_lua("ModeSelect.onExit({}) ModeSelect.onExit({}) frame() key(0x54) key(0x41) key(0x0D)")
        self.assertEqual(self.val("#calls.sent"), 1)                   # editor closed: chat works
        self.run_lua("ModeSelect.onEnter({}) world = 'TBX_Other' for i = 1, 20 do frame() end "
                     "key(0x54) key(0x42) key(0x0D)")
        self.assertEqual(self.val("#calls.sent"), 2)                   # a new level resets a stuck editor

    def border_in(self, level, set_name):
        self.run_lua("playset = '%s' world = '%s' for i = 1, 20 do frame() end key(0x54) frame()" % (set_name, level))
        border = self.val("calls.border")
        self.run_lua("key(0x1B) frame()")
        return border

    def test_colours_follow_the_playset(self):
        self.assertEqual(self.border_in("TBX_Test2", ""), self.val("rgb(70, 175, 245)"))       # Toy Box: blue
        self.assertEqual(self.border_in("IO_Hub", "InsideOut"), self.val("rgb(235, 110, 200)"))
        self.assertEqual(self.border_in("SW_Level", "Empire_Act1"), self.val("rgb(240, 190, 60)"))  # part
        self.run_lua(r'ini["chat_colors\tInsideOut"] = "102030,000000,FFFFFF"')                   # override
        self.assertEqual(self.border_in("IO_Hub2", "InsideOut"), self.val("rgb(16, 32, 48)"))
        self.run_lua(r'ini["chat\ttheme"] = "0"')                                               # theme off
        self.assertEqual(self.border_in("IO_Hub3", "InsideOut"), self.val("rgb(70, 175, 245)"))

    def test_game_input_silenced_while_typing(self):
        self.run_lua("key(0x54) frame()")
        self.assertTrue(self.val("typing") and self.val("pad"))       # keyboard (DLL) and pad captured
        self.run_lua("typing = false for i = 1, 30 do frame() end")
        self.assertTrue(self.val("typing"))                            # renewed while the line stays open
        self.run_lua("key(0x41) key(0x0D) frame()")
        self.assertFalse(self.val("typing") or self.val("pad"))        # Enter gives them back
        self.run_lua("key(0x54) key(0x1B) frame()")
        self.assertFalse(self.val("typing") or self.val("pad"))        # Esc
        self.run_lua("key(0x54) for i = 1, 30 do mod.onUpdate(1 / 60) end")
        self.assertFalse(self.val("typing") or self.val("pad"))        # a pause closes the line
        self.run_lua("for i = 1, 5 do frame() end key(0x54) mod.onShutdown()")
        self.assertFalse(self.val("typing") or self.val("pad"))        # F4 reload

    def test_editor_patch_survives_states_without_base_library(self):
        self.assertEqual(self.val("calls.patched"), 2)
        # the loader may run the patch in a state with no base library and no _G: it must just return
        ok = self.val("pcall(setfenv(loadstring(patch_src), {}))")
        self.assertTrue(ok)
        # a class that a script returned (package.loaded) is hooked too
        self.run_lua("package.loaded.ModeSelectSpark = { onEnter = function() end } "
                     "loadstring(patch_src)() package.loaded.ModeSelectSpark.onEnter({}) frame() "
                     "key(0x54) key(0x41) key(0x0D)")
        self.assertEqual(self.val("#calls.sent"), 0)                   # spark editor open: no chat

    def test_editor_screen_survives_missing_helper(self):
        # without dinet_bk the game's native rejects "#dinet:" keys with a Lua error (CrabeLoader 1.1 before
        # dinet_bk.asi): the wrapped screen must still open
        self.run_lua("Translator_TranslateString = function() error(\"bad argument #2 to 'tr'\") end "
                     "RibbonToyBox = { onEnter = function(self) return 'ribbon' end } loadstring(patch_src)()")
        self.assertEqual(self.val("RibbonToyBox.onEnter({})"), b"ribbon")

    def test_asi_replaces_code_cave_on_crabe_1_1(self):
        load = ("local f = loadstring(main_src, '=main.lua') f()")
        with open(MAIN_LUA, "rb") as f:
            self.g.main_src = f.read()
        # CrabeLoader 1.1 with dinet_bk.asi: no code cave, no memory touched
        self.run_lua("io = { open = function(p) if p == 'mods\\\\dinet_online\\\\dinet_bk.asi' then "
                     "return { close = function() end } end end } "
                     "Crabe.versionMajor, Crabe.versionMinor, Crabe.version = 1, 1, '1.1.0' "
                     "Crabe._moduleBase = function() error('memory read') end calls.logs = {} " + load)
        self.assertIn(b"loads dinet_bk.asi", self.val("calls.logs[1]"))
        # CrabeLoader 1.0 (no .asi loader): the code cave path still runs
        self.run_lua("Crabe.versionMinor, Crabe.version = 0, '1.0.0' calls.logs = {} " + load)
        self.assertIn(b"loader error", self.val("calls.logs[1]"))

    def test_pause_closes_open_line(self):
        self.run_lua("key(0x54) for i = 1, 30 do mod.onUpdate(1 / 60) end key(0x41) key(0x0D)")
        self.assertEqual(self.val("#calls.sent"), 0)
        self.assertEqual(self.val("calls.captured[#calls.captured]"), 0)

    def test_receive_places_bubble(self):
        self.run_lua('inbox = "7\\tFriend\\tFriendTag\\thello there\\n" for i = 1, 20 do frame() end')
        # the friend stands in front of the camera: bubble (rect, outline, tail, name, text) + chat log line
        self.run_lua("calls.draws = 0 frame()")
        self.assertEqual(self.val("calls.draws"), 6)
        self.run_lua("for i = 1, 60 * 13 do frame() end calls.draws = 0 frame()")   # bubble and log expire
        self.assertEqual(self.val("calls.draws"), 0)
        self.assertEqual(self.val("#calls.sent"), 0)

    def test_friend_behind_camera_has_no_bubble(self):
        self.run_lua("Game.ActorPosition = function(h) if h == 100 then return 0, 0, 0 end return 0, 0, -20 end")
        self.run_lua('inbox = "8\\tFriend\\tFriendTag\\thi\\n" for i = 1, 20 do frame() end calls.draws = 0 frame()')
        self.assertEqual(self.val("calls.draws"), 1)                   # only the chat log line


if __name__ == "__main__":
    unittest.main()
