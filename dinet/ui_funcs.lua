-- ui_funcs.lua - friends list for the Disney Infinity 3.0 Gold local server.
--
-- compile_ui_lua.py compiles this file into ui_funcs.luac (the game's bytecode format) and patch_ui.py
-- embeds it at the end of some menu scripts, called as ui_funcs(<script name>) right after the script
-- has defined its class. Nothing is loaded with loadstring at run time.
-- Data comes from dinet_bk.dll through Translator_TranslateString("#dinet:<command>").
-- Write defensively: check every value from the game before using it.

local script = ...
local G = _G

local function call(cmd)
  local translate = rawget(G, "Translator_TranslateString")
  if type(translate) ~= "function" then return "" end
  local r = translate("#dinet:" .. cmd)
  if type(r) ~= "string" or string.sub(r, 1, 7) == "#dinet:" then return "" end
  return r
end

-- "name TAB hosting TAB ..." lines -> { {name=, hosting=}, ... }
local function players(cmd)
  local out = {}
  for line in string.gmatch(call(cmd), "[^\n]+") do
    local name, hosting = string.match(line, "^([^\t]+)\t(%d)")
    if name then out[#out + 1] = { name = name, hosting = (hosting == "1") } end
  end
  return out
end

-- a message box made with the game's own Popup screen
local function popup(self, title, body)
  if type(self.AddScreen) ~= "function" or type(self.CallScreen) ~= "function" then return end
  local handle = self:AddScreen({ screenName = "Popup" })
  if handle then
    self:CallScreen(handle, "SetUsingTable", { title = title, body = body, list = "SharedText_Ok",
      buttonPrompts = "Btn_Legend_Select", hideArrow = true, allowCancel = false, onSelect = function() end }, true)
  end
end

-- Join <name>'s Toy Box. Only a level change FROM THE MAIN MENU opens a new network session (reloading
-- the current level with UI_LaunchLevel keeps the session: tested, no JoinGame / JoinOrHost). So inside
-- a level: dinet_bk prepares the join and remembers the current level ("joinvia"), the game exits to the
-- main menu like the pause menu's "Main Menu" option (Pause_ExitGame), and the main menu (below, script
-- levelselectmenu) loads that level again, this time as a client of <name>.
-- In the front end the join is prepared and the player then enters any Toy Box.
-- Returns true when the join is under way.
local function join_game(self, name)
  local inFrontEnd = rawget(G, "System_InFrontEndOld")
  local inLevel = type(inFrontEnd) == "function" and not inFrontEnd()
  local world = nil
  local currentWorld = rawget(G, "UI_CurrentWorldName")
  if inLevel and type(currentWorld) == "function" then world = currentWorld() end
  local exitGame = rawget(G, "Pause_ExitGame")
  local via = type(world) == "string" and world ~= "" and world ~= "frontend" and type(exitGame) == "function"
  -- in the main menu: prepare silently; the main menu's timer (below) loads the host's level right away
  -- (RumpusRoom_Blank only if the host's level is unknown)
  local fromMenu = not via and type(inFrontEnd) == "function" and inFrontEnd() and rawget(G, "LevelSelectMenu") ~= nil
  local r
  if via then
    r = call("joinvia:" .. name .. "\t" .. world)
  elseif fromMenu then
    r = call("joinvia:" .. name .. "\tRumpusRoom_Blank")
  else
    r = call("join:" .. name)
  end
  call("log:join " .. name .. ": " .. r .. (via and (" -> via the main menu, then " .. world) or ""))
  if r == "error:not_hosting" then
    popup(self, "@Scn_GameInvite_Title", name .. " no esta dentro de una Toy Box ahora mismo.")
    return false
  elseif string.sub(r, 1, 10) == "error:msg:" then        -- dinet_bk explains it (different maps)
    popup(self, "@Scn_GameInvite_Title", string.sub(r, 11))
    return false
  elseif r ~= "ok" then
    popup(self, "@Scn_GameInvite_Title", "No se pudo preparar la union (" .. r .. ").")
    return false
  end
  if via then
    local player = 0
    if type(self.InstanceData) == "table" and self.InstanceData.playerNum then player = self.InstanceData.playerNum end
    exitGame()                                   -- what the pause menu's "Main Menu" confirmation calls
    local t = { screenName = "Popup", playerNum = player }
    if type(self.RemoveScreenStack) == "function" then self:RemoveScreenStack(t) end
    t.screenName = "PauseMenu"
    if type(self.RemoveScreen) == "function" then self:RemoveScreen(t) end
    local unpause = rawget(G, "Pause_UnPauseFromPausedScreenIfPaused")
    if type(unpause) == "function" then unpause(player) end
  else
    local unpause = rawget(G, "Pause_UnPauseFromPausedScreenIfPaused")
    if type(unpause) == "function" and type(self.InstanceData) == "table" then
      unpause(self.InstanceData.playerNum)
    end
  end
  return true
end

-------------------------------------------------------------------- notices (shared)
-- dinet_bk hands out one notice at a time ("notice"):
--   request  - someone wants to join our Toy Box: accept (= invite them) or refuse
--   accepted - our join request was accepted: join now
--   invite   - an invitation: join now, or keep it for later (Online multiplayer > Invites)
-- The player 1 HUD shows them during the game; the online multiplayer screens (pause menu) show them
-- while they are open, so you can wait for an answer right there.

-- Yes / No question with the game's Popup screen; self.dinetBusy while it is open
local function ask(self, title, body, onYes, onNo)
  self.dinetBusy = true
  local handle = self:AddScreen({ screenName = "Popup" })
  if not handle then self.dinetBusy = false return end
  self:CallScreen(handle, "SetUsingTable", {
    title = title, body = body, list = "SharedText_Yes,SharedText_No",
    buttonPrompts = "Btn_Legend_Select,Btn_Legend_Cancel", hideArrow = true, allowCancel = true,
    onSelect = function(option)
      self.dinetBusy = false
      if option == "SharedText_Yes" then
        if onYes then onYes() end
      elseif onNo then
        onNo()
      end
    end }, true)
end

-- takes the next notice from dinet_bk (if any) and asks about it on screen <self>
local function show_notice(self, where)
  local n = call("notice")
  local kind, name = string.match(n, "^(%a+)\t(.+)$")
  if not kind then return end
  call("log:" .. where .. " notice " .. kind .. " " .. name)
  if kind == "request" then
    ask(self, "@Scn_GameInvite_Title", name .. " quiere unirse a tu partida. Aceptar?",
      function()
        local r = call("accept:" .. name)
        if r == "error:not_hosting" then
          popup(self, "@Scn_GameInvite_Title", "Tienes que estar en una Toy Box para que " .. name .. " se una.")
        end
      end,
      function() call("deny:" .. name) end)
  elseif kind == "accepted" or kind == "invite" then
    local text = name .. " te invita a su Toy Box."
    if kind == "accepted" then text = name .. " acepto tu peticion." end
    ask(self, "@Scn_GameInvite_Title", text .. " Unirte ahora? (lo que no hayas guardado se pierde)",
      function() join_game(self, name) end, nil)
  end
end

-- menu screens: check for notices every second while the screen is open (OS timers also run paused)
local function listen_in_menu(self, where)
  if type(self.ScheduleTimer) ~= "function" then return end
  self.dinetClosed = false
  if self.dinetTimer and type(self.RemoveTimer) == "function" then self:RemoveTimer(self.dinetTimer) end
  self.dinetTimer = self:ScheduleTimer("OS_REPEAT", 1.0, function()
    if self.dinetClosed or self.dinetBusy then return end
    show_notice(self, where)
  end)
end

-- stop listening when the screen closes (wraps class method <name>, e.g. onExit)
local function stop_listening_on(C, name)
  local orig = C[name]
  C[name] = function(self, a, b)
    self.dinetClosed = true
    if self.dinetTimer and type(self.RemoveTimer) == "function" then self:RemoveTimer(self.dinetTimer) end
    self.dinetTimer = nil
    if orig then return orig(self, a, b) end
  end
end

-------------------------------------------------------------------- main menu
-- When a join was started inside a level (join_game "via"), the main menu loads that level again right
-- away, the way the new Toy Box menu loads one (Network_SetJoinSession + UI_LaunchLevel(world, "world",
-- true, true)); dinet_bk's join flag makes that load join the host.
if script == "levelselectmenu" then
  local C = rawget(G, "LevelSelectMenu")
  if type(C) == "table" then
    local active = C.onActive
    C.onActive = function(self, a, b)
      if type(self.ScheduleTimer) == "function" then
        -- every second while the main menu is up (a join can also be accepted here, from a message box)
        if self.dinetTimer and type(self.RemoveTimer) == "function" then self:RemoveTimer(self.dinetTimer) end
        self.dinetTimer = self:ScheduleTimer("OS_REPEAT", 1.0, function()
          local world = call("pendingworld")
          if world == "" then
            -- nothing to load: show invites / join requests with the game's popup (not a Windows box)
            if not self.dinetBusy then show_notice(self, "main menu") end
            return
          end
          call("log:main menu: loading " .. world .. " to join the prepared game")
          local clear = rawget(G, "ClearNewsPaper")
          if type(clear) == "function" then clear() end
          local setJoin = rawget(G, "Network_SetJoinSession")
          if type(setJoin) == "function" then setJoin(true) end
          local launch = rawget(G, "UI_LaunchLevel")
          if type(launch) == "function" then launch(world, "world", true, true) end
        end)
      end
      if active then return active(self, a, b) end
    end
  end
end

-------------------------------------------------------------------- pause menu
-- In playsets PauseMenu:HasOnlineOptions() is only true on PS3 / Wii U / Xbox 360, so the PC pause menu
-- of every playset hides the online multiplayer entry. Here it is also true for the host when online.
-- (patch_ui.py also makes this script's IsMultiplayerAllowed() answer true.)
if script == "pausemenu" then
  local C = rawget(G, "PauseMenu")
  if type(C) == "table" then
    local has = C.HasOnlineOptions
    C.HasOnlineOptions = function(self, a, b)
      if has and has(self, a, b) then return true end
      local isOnline = rawget(G, "IsOnline")
      local player = 0
      if type(self.InstanceData) == "table" and self.InstanceData.playerNum then player = self.InstanceData.playerNum end
      if type(isOnline) ~= "function" or not isOnline(player) then return false end
      if type(self.IsHost) == "function" and not self:IsHost() then return false end
      -- story playsets (Star Wars, Inside Out, Dory, Moana, Battlegrounds) stay single-player in dinet_bk
      -- unless force_multiplayer_levels=2; Takeover and Speedway are Toy Box modes and keep the button
      local zoneBool = rawget(G, "UI_GetZoneMgrBool")
      if type(zoneBool) == "function" and zoneBool("<current>", "WorldIsPlayset") and call("playsetmp") ~= "1" then
        return false
      end
      return true
    end
  end
end

-------------------------------------------------------------------- online multiplayer menu
-- adds "Friends" (friends list, invite) and "Invites" (invites received, join) buttons.
-- "Invites" (ACT_Invites) is handled by the game's own SelectItem: it opens friend_invites_u.
if script == "onlinemp_options" then
  local C = rawget(G, "OnlineMP_Options")
  if type(C) == "table" then
    local build, select = C.BuildList, C.SelectItem
    C.BuildList = function(self, a, b)
      if build then build(self, a, b) end
      local list = self.listData
      if type(list) ~= "table" or type(self.AddListButton) ~= "function" then return end
      local first = list[1]
      if #list == 1 and type(first) == "table" and first.widgetType == "EmptyList" then
        self.listData = {}
      end
      local hasInvites = false
      for _, item in ipairs(self.listData) do
        if type(item) == "table" and item.id == "ACT_Invites" then hasInvites = true end
      end
      self:AddListButton("Scn_Friends")
      if not hasInvites then self:AddListButton("ACT_Invites") end
    end
    C.SelectItem = function(self, a, b)
      local list = self.listData
      local item = nil
      if type(list) == "table" then item = list[self.selectedIndex] end
      if type(item) == "table" and item.id == "Scn_Friends" then
        call("log:opening the friends list")
        self.startId = 1
        self:AddScreen({ screenName = "friends_list_u" })
        self:SetFocus(false)
        return
      end
      if select then return select(self, a, b) end
    end
  end
end

-------------------------------------------------------------------- friends list screen
-- the players connected to the server, instead of the removed XMPP friends (GetFriendsList native)
if script == "friends_list_u" then
  local C = rawget(G, "friends_list_u")
  if type(C) == "table" then
    C.MovieLoaded = function(self)
      self.friendData = {}
      self.friendIndex = 1
      self.movieIntroComplete = true
      self:CallFlashFunction("_root.ShowClip", 2)
      self:CallFlashFunction("_root.SetupInput", 1)
      local list = players("players")
      for i, p in ipairs(list) do
        local inGame = 0
        if p.hosting then inGame = 1 end
        self:AddFriendToList({ name = p.name, stars = 0, onlineStatus = 1, inGameStatus = inGame,
                               status = "none", lastSession = 0, friendHandle = i })
      end
      call("log:friends list shows " .. #list .. " player(s)")
      self:AllFriendsFound()
      listen_in_menu(self, "friends list")
    end
    stop_listening_on(C, "onExit")
    -- the native refresh talked to the removed XMPP client
    C.UpdateFriendList = function(self) end

    -- send invitations to a list of friendData entries (we must be inside a Toy Box: they join our game)
    local function invite(self, targets)
      if call("hosting") ~= "1" then
        popup(self, "@Scn_GameInvite_Title", "Entra a una Toy Box antes de invitar: tus amigos se unen a la partida que tienes abierta.")
        return
      end
      local sent, last = 0, ""
      for _, f in ipairs(targets) do
        if type(f) == "table" and type(f.name) == "string" then
          call("invite:" .. f.name)
          f.status = "invited"
          self:CallFlashFunction("_root.SetStatusIcon", f.name, "ui/icons/icon_missionRepeat")
          sent, last = sent + 1, f.name
        end
      end
      call("log:invitation sent to " .. sent .. " player(s)")
      if sent == 1 then
        popup(self, "@Scn_GameInvite_Title", "Invitacion enviada a " .. last .. ".")
      elseif sent > 1 then
        popup(self, "@Scn_GameInvite_Title", "Invitacion enviada a " .. sent .. " jugadores.")
      end
    end


    -- picking a player: Invite / Join / Cancel. The first entry ("invite all") invites everybody.
    C.SelectItem = function(self)
      local data = self.friendData
      local f = nil
      if type(data) == "table" then f = data[self.selectedIndex] end
      if type(f) ~= "table" or type(f.name) ~= "string" then return end
      if string.sub(f.name, 1, 1) == "@" then
        local all = {}
        for i = 2, #data do all[#all + 1] = data[i] end
        invite(self, all)
        return
      end
      local where = "En el menu."
      if f.inGameStatus == 1 then
        where = "Esta dentro de una Toy Box: puedes pedirle unirte a su partida."
      end
      local handle = self:AddScreen({ screenName = "Popup" })
      if not handle then return end
      self:CallScreen(handle, "SetUsingTable", {
        title = f.name, body = where, list = "Friends_Invite,Friends_Join,Options_Cancel",
        buttonPrompts = "Btn_Legend_Select,Btn_Legend_Cancel", hideArrow = true, allowCancel = true,
        onSelect = function(option)
          if option == "Friends_Invite" then
            invite(self, { f })
          elseif option == "Friends_Join" then
            -- ask to join: the host gets "X wants to join" in game; accepting sends us an invite
            local r = call("request:" .. f.name)
            if r == "ok" then
              popup(self, "@Scn_GameInvite_Title", "Peticion enviada a " .. f.name .. ". Si acepta, te avisaremos para unirte.")
            elseif r == "error:not_hosting" then
              popup(self, "@Scn_GameInvite_Title", f.name .. " no esta dentro de una Toy Box ahora mismo.")
            else
              popup(self, "@Scn_GameInvite_Title", "No se pudo enviar la peticion (" .. r .. ").")
            end
          end
        end }, true)
    end
    C.FriendSelected = function(self) end
  end
end

-------------------------------------------------------------------- in-game notices (player 1 HUD)
-- Every second the HUD asks dinet_bk for a notice and shows it with the game's Popup screen:
--   request  - someone wants to join our Toy Box: accept (= invite them) or refuse
--   accepted - our join request was accepted: join now (the level reloads as a client)
--   invite   - an invitation: join now, or keep it for later (Online multiplayer > Invites)
-- While the game is paused the notice waits; it pops up as soon as the game runs again.
if script == "player_hud_combined" then
  local C = rawget(G, "Player_HUD_Combined")
  if type(C) == "table" then
    local loaded = C.MovieLoaded
    local function poll(self)
      -- OS_REPEAT keeps ticking while the game is paused; the pausable clock does not: while paused
      -- only tell dinet_bk the HUD is here (so it does not use a Windows message box) and wait
      local clock = rawget(G, "System_TimerSecPausable")
      if type(clock) == "function" then
        local now = clock()
        local paused = type(now) == "number" and type(self.dinetClock) == "number" and now - self.dinetClock < 0.3
        self.dinetClock = now
        if paused then call("hudalive") return end
      end
      if self.dinetBusy then call("hudalive") return end
      show_notice(self, "HUD")
    end
    C.MovieLoaded = function(self, a, b)
      -- tell dinet_bk which level we play: it is announced with our game, and a guest must load the
      -- same level (otherwise the host kicks it)
      local currentWorld = rawget(G, "UI_CurrentWorldName")
      if type(currentWorld) == "function" then
        local w = currentWorld()
        if type(w) == "string" and w ~= "" then call("world:" .. w) end
      end
      if type(self.ScheduleTimer) == "function" then
        self:ScheduleTimer("OS_REPEAT", 1.0, function() poll(self) end)
        call("log:HUD is listening for invites and join requests")
      end
      if loaded then return loaded(self, a, b) end
    end
    -- tell dinet_bk which character we play and its level (shown by the server). The game calls
    -- PlayerDataChanged with DirtyData.PlayerIconName / PlayerLevel when the figure or its level changes.
    local changed = C.PlayerDataChanged
    C.PlayerDataChanged = function(self, data, ...)
      local r = { changed and changed(self, data, ...) }
      pcall(function()
        local num = type(self.InstanceData) == "table" and self.InstanceData.playerNum or nil
        local isGuest = rawget(G, "ToyIsGuest")
        if num ~= nil and type(isGuest) == "function" and isGuest(num) then return end   -- 2nd local player
        local name = nil
        local avatarName = rawget(G, "UI_GetAvatarNameForIGPPopup")
        if num ~= nil and type(avatarName) == "function" then name = avatarName(num) end
        if type(name) ~= "string" or name == "" then
          local dirty = type(data) == "table" and data.DirtyData or nil
          name = type(dirty) == "table" and dirty.PlayerIconName or nil
          if type(name) ~= "string" or string.find(name, "HUD_PlayerIcons_Default", 1, true) then name = nil end
        end
        local info = type(self.PlayerData) == "table" and self.PlayerData.PlayerInfo or nil
        local level = type(info) == "table" and tonumber(info.curLevel) or nil
        if not name then return end
        local msg = name .. "\t" .. (level and tostring(math.floor(level)) or "")
        if msg ~= self.dinetAvatar then
          self.dinetAvatar = msg
          call("avatar:" .. msg)
        end
      end)
      return unpack(r)
    end
  end
end

-------------------------------------------------------------------- invites screen
-- invitations received, from the server; picking one prepares the join (like F8)
if script == "friend_invites_u" then
  local C = rawget(G, "friend_invites_u")
  if type(C) == "table" then
    C.MovieLoaded = function(self)
      self.friendData = {}
      self.friendIndex = 1
      self.movieIntroComplete = true
      self:CallFlashFunction("_root.ShowClip", 2)
      self:CallFlashFunction("_root.SetupInput", 1)
      local list = players("invites")
      for _, p in ipairs(list) do
        self:AddFriendToList({ name = p.name, stars = 0, status = "static", lastSession = 0 })
      end
      call("log:invites screen shows " .. #list .. " invite(s)")
      self:AllFriendsFound()
      listen_in_menu(self, "invites screen")
    end
    stop_listening_on(C, "onExit")
    C.UpdateFriendList = function(self) end
    C.FriendSearch = function(self) end
    C.FriendSelected = function(self)
      self.movieIntroComplete = true
      local data = self.friendData
      local f = nil
      if type(data) == "table" then f = data[self.selectedIndex] end
      if type(f) ~= "table" or type(f.name) ~= "string" then return end
      call("log:accept invite from " .. f.name)
      join_game(self, f.name)
    end
  end
end
