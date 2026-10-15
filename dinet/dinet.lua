-- dinet.lua - friends, invites and the sign-in screen for the Disney Infinity 3.0 Gold local server.
--
-- patch_ui.py makes the Disney ID, online multiplayer, friends and invites screens run this file
-- (loadstring(Translator_TranslateString("#dinet:lua"), "=dinet")) every time they open. dinet_bk.dll
-- answers the "#dinet:<command>" keys (see dinet.c, stage 4). Everything here must be safe to run many
-- times, and must never raise a Lua error: the engine's error handler crashes the game even for errors
-- caught by pcall. So globals are only read / written with rawget / rawset (no "undeclared global"
-- checks), and every value from the game is type-checked before use.
--
-- The Gold Edition keeps the console friends screens (friends_list_u, friend_invites_u), but their
-- natives answered from the removed XMPP client. Their protocol: GetFriendsList() makes the screen
-- receive screen:AddFriendToList{...} for every friend, then screen:AllFriendsFound().
-- Every game connected to the same server is a "friend".

local G = _G
local function global(name) return rawget(G, name) end
local translate = global("Translator_TranslateString")
if type(translate) ~= "function" then return end

local function call(cmd)
  local r = translate("#dinet:" .. cmd)
  if type(r) ~= "string" then return "" end
  return r
end
local function log(text) call("log:" .. tostring(text)) end

local D = global("dinet")
if type(D) ~= "table" then
  D = { invited = {}, orig = {} }
  rawset(G, "dinet", D)
end

local screenSelf, screenName = ...
if type(screenSelf) ~= "table" then screenSelf = nil end
if type(screenName) ~= "string" then screenName = "?" end
log("start (" .. screenName .. ")")

-- t[name] = make(original), wrapping each table only once
local function wrap(t, name, make)
  if type(t) ~= "table" then return end
  local key = tostring(t) .. ":" .. name
  if D.orig[key] == nil then
    local cur = t[name]
    D.orig[key] = (type(cur) == "function") and cur or false
  end
  t[name] = make(D.orig[key] or nil)
end

-- "name TAB hosting TAB ip TAB private_ip TAB port" lines
local function parse(text)
  local out = {}
  for line in string.gmatch(text, "[^\n]+") do
    local name, hosting = string.match(line, "^([^\t]+)\t(%d)")
    if name then
      out[#out + 1] = { name = name, hosting = (hosting == "1") }
    end
  end
  return out
end

local function popup(screen, title, body)
  if type(screen) ~= "table" or type(screen.AddScreen) ~= "function" then return end
  local handle = screen:AddScreen({ screenName = "Popup" })
  if handle and type(screen.CallScreen) == "function" then
    screen:CallScreen(handle, "SetUsingTable", {
      title = title, body = body, list = "SharedText_Ok", buttonPrompts = "Btn_Legend_Select",
      hideArrow = true, allowCancel = false, onSelect = function() end }, true)
  end
end

-- show a new status for one friend on the open screen
local function mark(name, status)
  local screen = D.screen
  if type(screen) ~= "table" or type(screen.friendData) ~= "table" then return end
  for _, f in ipairs(screen.friendData) do
    if type(f) == "table" and f.name == name then f.status = status end
  end
  if type(screen.CallFlashFunction) == "function" then
    screen:CallFlashFunction("_root.Rebuild")
  end
end

---------------------------------------------------------------------------- natives
rawset(G, "GetFriendsList", function()
  local screen = D.screen
  if type(screen) ~= "table" or type(screen.AddFriendToList) ~= "function" then return end
  local invites = D.onInviteScreen
  local list = parse(call(invites and "invites" or "players"))
  log("GetFriendsList: " .. #list .. (invites and " invite(s)" or " player(s)"))
  for i, p in ipairs(list) do
    local status = "none"
    if invites then
      status = "static"
    elseif D.invited[p.name] then
      status = "invited"
    end
    screen:AddFriendToList({
      name = p.name, stars = 0, onlineStatus = 1, inGameStatus = p.hosting and 1 or 0,
      status = status, lastSession = 0, friendHandle = i })
  end
  if type(screen.AllFriendsFound) == "function" then screen:AllFriendsFound() end
end)

rawset(G, "UI_UpdateFriendList", function() end)

rawset(G, "SetOnInviteScreen", function(on)
  D.onInviteScreen = on and true or false
end)

rawset(G, "InviteFriend", function(name)
  if type(name) ~= "string" or string.sub(name, 1, 1) == "@" then return true end
  call("invite:" .. name)
  D.invited[name] = true
  mark(name, "invited")
  return true
end)

rawset(G, "UninviteFriend", function(name)
  if type(name) ~= "string" then return true end
  call("uninvite:" .. name)
  D.invited[name] = nil
  mark(name, "none")
  return true
end)

rawset(G, "JoinFriend", function(name)
  local r = call("join:" .. tostring(name))
  if r == "ok" then return true end
  if r == "error:not_hosting" then
    popup(D.screen, "Unirse", tostring(name) .. " no esta dentro de una Toy Box ahora mismo.")
  else
    popup(D.screen, "Unirse", "No se pudo preparar la union (" .. r .. ").")
  end
  return false
end)

---------------------------------------------------------------------------- screens
-- The patched onEnter passes (screen, script name). Changes go to the class and to the open screen
-- itself, in case the class factory copied the methods into it.
local friendsClass, invitesClass = global("friends_list_u"), global("friend_invites_u")

-- remember which friends screen is open: GetFriendsList has no arguments
if screenName == "friends_list_u" or screenName == "friend_invites_u" then
  D.screen = screenSelf
  D.onInviteScreen = (screenName == "friend_invites_u")
end
local function remember_screen(class, isInvites)
  wrap(class, "MovieLoaded", function(orig)
    return function(self, ...)
      D.screen = self
      D.onInviteScreen = isInvites
      if orig then return orig(self, ...) end
    end
  end)
end
remember_screen(friendsClass, false)
remember_screen(invitesClass, true)

-- online multiplayer menu (pause menu, Toy Box): the PC build hides "View Friends"
local function patch_online_menu(target)
  wrap(target, "BuildList", function(orig)
    return function(self, ...)
      local r
      if orig then r = orig(self, ...) end
      if type(self.listData) ~= "table" or type(self.AddListButton) ~= "function" then return r end
      for _, item in ipairs(self.listData) do
        if type(item) == "table" and item.id == "Scn_View_Friends" then return r end
      end
      local isOnline = global("IsOnline")
      local player = type(self.InstanceData) == "table" and self.InstanceData.playerNum or 0
      if type(isOnline) == "function" and not isOnline(player) then return r end
      local first = self.listData[1]
      if #self.listData == 1 and type(first) == "table" and first.widgetType == "EmptyList" then
        self.listData = {}
      end
      self:AddListButton("Scn_View_Friends")
      return r
    end
  end)
  wrap(target, "SelectItem", function(orig)
    return function(self, ...)
      local item = type(self.listData) == "table" and self.listData[self.selectedIndex] or nil
      if type(item) == "table" and item.id == "Scn_View_Friends" then
        log("opening the friends list")
        self.startId = 1
        self:AddScreen({ screenName = "friends_list_u" })
        self:SetFocus(false)
        return
      end
      if orig then return orig(self, ...) end
    end
  end)
end
patch_online_menu(global("OnlineMP_Options"))
if screenName == "onlinemp_options" then patch_online_menu(screenSelf) end

-- Disney ID screen: no "Save Migration" text, only the account fields
local function no_info_text() return "" end
local didClass = global("disney_id")
if type(didClass) == "table" then didClass.GetInfoText = no_info_text end
if screenName == "disney_id" and screenSelf then screenSelf.GetInfoText = no_info_text end

log("done (" .. screenName .. ")")
