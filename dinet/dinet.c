/*
 * dinet_bk.dll - native helper for the Disney Infinity 3.0 Gold local server.
 *
 * Loaded only by DisneyInfinity3_local.exe: patch_exe.py --dinet renames that
 * copy's "bink2w32.dll" import to "dinet_bk.dll", and every Bink export is
 * forwarded to the game's real bink2w32.dll (bink_forwards.h).
 *
 * Stage 1 - Disney ID sign-in. SignInHelper::DisneyIdSignIn(bool signedIn, uint
 * playerId) sets the "signed in" flag every online feature checks. The Gold
 * Edition's one-shot cloud migration clears it right after the login; with
 * [signin] block_signout=1 the sign-out calls are ignored. Every call is logged.
 *
 * Stage 2 - two players in one Toy Box. The Gold Edition has no friends or
 * matchmaking code left (the XMPP client is gone), but Net-Z itself still works:
 * CNetZSession::JoinOrHost opens the level's session as a host, or, when the
 * session's join flag is set, joins the host that CKFOnline::PlayerListGetHost
 * returns. So:
 *   - the Net-Z port is pinned ([net] port, default 9104) instead of a random
 *     one in 9104-9603, so it can be opened in a router;
 *   - while this PC hosts, it is announced to the server lobby every 30 s;
 *   - F8 lists the lobby; picking a host sets the join flag and makes
 *     PlayerListGetHost return that host. Loading a Toy Box then joins it.
 *   - F9 cancels a prepared join.
 *
 * Stage 3 - local saves while signed in. Every Disney ID sign-in runs the Gold
 * Edition's cloud migration, which deletes the local Toy Box saves (the server
 * cannot send cloud Toy Boxes: they need Disney's response signature). And while
 * signed in, SaveGameManager::GetSaveGameList asks the cloud for the Toy Box list
 * instead of reading the local one. So:
 *   - [saves] local_toybox_list=1: GetSaveGameList always uses the local Toy Box
 *     list (PushLocalUGCList), signed in or not;
 *   - [saves] protect=1: the save folders are copied to <game>\di_saves\<time> when
 *     the game starts, and the first sign-out of the run (the end of the migration)
 *     copies back every save file the migration removed. Changed files are kept.
 *
 * Stage 5 - text chat. The CrabeLoader mod (main.lua) draws a chat box (key T) and speech bubbles; this
 * DLL turns key presses into text with the player's keyboard layout ("#dinet:chatkey:<vk>"), sends what
 * was said to the server ("#dinet:chatsend:<gamer tag> TAB <text>") and hands the messages of the other
 * players to Lua ("#dinet:chat"), polled from the server every [chat] poll_ms by its own thread.
 *
 * Build: build.bat (Visual Studio 2022, x86).
 */
#define WIN32_LEAN_AND_MEAN
#define _WINSOCK_DEPRECATED_NO_WARNINGS
#define _CRT_SECURE_NO_WARNINGS
#include <windows.h>
#include <winsock2.h>
#include <winhttp.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <string.h>
#include <ctype.h>
#define DIRECTINPUT_VERSION 0x0800
#include <dinput.h>
#include "bink_forwards.h"

#pragma comment(lib, "ws2_32.lib")
#pragma comment(lib, "winhttp.lib")
#pragma comment(lib, "user32.lib")
#pragma comment(lib, "dinput8.lib")
#pragma comment(lib, "dxguid.lib")

/* Steam build 1522802, md5 7ee2954a6c6ca7070bb4bfba0a8b9f94, linked at 0x400000. */
#define LINK_BASE            0x00400000u
#define VA_DISNEYID_SIGNIN   0x01348C60u   /* SignInHelper::DisneyIdSignIn, thiscall(bool, uint) */
#define VA_JOIN_OR_HOST      0x0184A930u   /* CNetZSession::JoinOrHost (reads the host getters) */
#define VA_PLAYERLIST_HOST   0x013297B0u   /* CKFOnline::PlayerListGetHost -> StationInfo* or NULL */
#define VA_PLAYERLIST_HEAD   0x0225ADF8u   /* its list head: node { next; ?; StationInfo at +8 } */
#define VA_NETZ_PORT_PUSHES  0x00AA4629u   /* push 9603; push 9104; call RandomPort; SetWellKnownPort */
#define VA_SESSION_PTR       0x0221275Cu   /* g_session; Network_SetJoinSession writes [g_session+0x14] */
#define SESSION_JOIN_FLAG    0x14
#define VA_SET_JOIN_SESSION  0x00659D50u   /* CSession::SetJoinSession(bool): mov al,[esp+4]; mov [ecx+14h],al; ret 4 */
/* Client side of GameNetworkLoop. Joining needs, in order:
 * g_Online.GetJoinData() != NULL (inlined: [g_Online+0x6C]; the GameSpy friends menu used to set it),
 * g_Online.JoinGame(data, password, JoinGameCallback, PlayerUpdateCallback, loop) reporting success,
 * g_Online.PlayerListAllNegotiated() (NAT negotiation done); then the level's Net-Z session opens as a
 * client and OpenInternet reads the host from PlayerListGetHost. */
#define VA_ONLINE_JOINDATA   0x0225AE6Cu   /* g_Online (0x225AE00) + 0x6C */
#define VA_ONLINE_JOINGAME   0x0132B600u   /* thiscall(void* data, Genie::String pw, cb, playerCb, user) */
#define VA_ALL_NEGOTIATED    0x01329850u   /* bool CKFOnline::PlayerListAllNegotiated(): mov eax,[list head] ... */
/* Host side: Quazal asks ClientJoinRequestCallback(JoinSessionOperation*) before letting a player in.
 * It denies the join while g_Online holds any of MultiplayerLock, LevelLock, MatchingLock, JoinLock,
 * SpeedwayTransition (CKFOnline::HasLock). The Gold Edition is an offline build and keeps some of them. */
#define VA_JOIN_REQUEST      0x01849060u   /* push -1; push <SEH>; ... */
#define VA_JOIN_DENY_BRANCH  0x018491F9u   /* jne deny (0F 85 B2 00 00 00) after the lock checks */
#define VA_ONLINE            0x0225AE00u   /* g_Online */
#define VA_HAS_LOCK          0x01329A30u   /* bool CKFOnline::HasLock(const Genie::String&) */
/* g_netError: every network failure goes through its vtable slot 0,
 * thiscall Report(const char* message, int, int, const char* sourceFile, int line). */
#define VA_NETERROR_PTR      0x0228DB5Cu
/* CKFOnline::SetConnectionState(int): mov eax,[esp+4]; mov [ecx+40h],eax; ret 4.
 * StandAloneLoop::UpdatePrivileges drops a CLIENT to the main menu ("connection lost") while the
 * state is below ConnectionInGame (7); CKFOnline set it when its GameSpy join finished, which we skip. */
#define VA_SET_CONN_STATE    0x0097D100u
#define VA_ONLINE_CONNSTATE  0x0225AE40u   /* g_Online + 0x40 */
#define CONNECTION_IN_GAME   7
/* SaveGameManager::GetSaveGameList (0x1370E60), after IsPlaysetUGC(filter):
 * "test al,al; je local; cmp byte [esp+28h],0 (online)" -> the je becomes jmp. */
#define VA_SAVELIST_UGC_TEST 0x01370F62u
#define SAVE_SNAPSHOTS_KEPT  20

static const BYTE kSigninPrologue[5] = { 0x83, 0xEC, 0x08, 0x53, 0x55 };        /* sub esp,8; push ebx; push ebp */
static const BYTE kJoinOrHostPrologue[6] = { 0x64, 0xA1, 0x00, 0x00, 0x00, 0x00 }; /* mov eax, fs:[0] */
static const BYTE kPortPushes[10] = { 0x68, 0x83, 0x25, 0x00, 0x00, 0x68, 0x90, 0x23, 0x00, 0x00 };
static const BYTE kSetConnState[10] = { 0x8B, 0x44, 0x24, 0x04, 0x89, 0x41, 0x40, 0xC2, 0x04, 0x00 };
static const BYTE kSetJoinSession[10] = { 0x8A, 0x44, 0x24, 0x04, 0x88, 0x41, 0x14, 0xC2, 0x04, 0x00 };
static const BYTE kSaveListUgcTest[9] = { 0x84, 0xC0, 0x74, 0x46, 0x80, 0x7C, 0x24, 0x28, 0x00 };

static HMODULE g_self;
static DWORD   g_base;               /* runtime base of the exe (ASLR) */
static DWORD   g_textLo, g_textHi;   /* runtime range of the exe's .text */
static char    g_logPath[MAX_PATH], g_iniPath[MAX_PATH];
static CRITICAL_SECTION g_logLock;

/* stage 1 */
static DWORD   g_signinBack;
static volatile LONG g_block;
static int     g_blockSignout;

/* stage 2 settings */
static char    g_serverHost[128] = "127.0.0.1";
static int     g_serverPort = 8085;
static int     g_netPort = 9104;
static int     g_approveAll = 1;
static char    g_name[41];
static int     g_nameFromIni;
static char    g_privateIp[32] = "127.0.0.1";

/* stage 2 state */
static DWORD   g_joinOrHostBack, g_hostBack, g_listHeadAddr;
static volatile LONG g_joinArmed;     /* a join is prepared: force the client path */
static volatile LONG g_fakeHostActive; /* PlayerListGetHost returns g_fakeHost */
static volatile LONG g_hosting;       /* announce to the lobby */
static volatile LONG g_joinStarted;   /* JoinOrHost ran in join mode */
static volatile DWORD g_joinStartTick;
#define JOIN_ARMED_TIMEOUT 120000
static char           g_pendingWorld[64];    /* level the main menu loads to start a prepared join */
static char           g_myWorld[64];         /* level we play now (told by the HUD), announced with our game */
static volatile LONG  g_announceNow;         /* announce at once (our level changed) */
static char           g_myAvatar[64];        /* character we play and its level (told by the HUD), */
static char           g_myAvatarLevel[16];   /* sent with our presence */
static volatile LONG  g_presenceNow;         /* send the presence at once (character / level changed) */
static volatile DWORD g_joinArmedTick;       /* when the join was prepared (cancelled if it never starts) */
static volatile LONG  g_unannounce;          /* the join started: remove our game from the lobby */
static char    g_joinTarget[64];

/* stage 3 */
static int     g_localToyboxList = 1, g_protectSaves = 1;
static char    g_saveRoot[MAX_PATH];   /* %APPDATA%\Disney Interactive\Disney Infinity 3.0 */
static char    g_snapDir[MAX_PATH];    /* <game>\di_saves\<time>, empty if no snapshot */
static volatile LONG  g_snapReady, g_restorePending, g_restoreDone;
static volatile DWORD g_restoreTick;

static DWORD rva(DWORD runtime) { return runtime - g_base + LINK_BASE; }   /* -> static VA */
static DWORD rt(DWORD va) { return g_base + (va - LINK_BASE); }            /* static VA -> runtime */

static void logf(const char* fmt, ...)
{
    char line[1024];
    SYSTEMTIME t;
    va_list ap;
    FILE* f;
    int n;
    GetLocalTime(&t);
    n = _snprintf(line, sizeof(line) - 2, "[%02d:%02d:%02d.%03d] ", t.wHour, t.wMinute, t.wSecond, t.wMilliseconds);
    va_start(ap, fmt);
    n += _vsnprintf(line + n, sizeof(line) - 2 - n, fmt, ap);
    va_end(ap);
    if (n < 0 || n > (int)sizeof(line) - 2) n = (int)sizeof(line) - 2;
    line[n++] = '\n';
    line[n] = 0;
    EnterCriticalSection(&g_logLock);
    f = fopen(g_logPath, "a");
    if (f) { fputs(line, f); fclose(f); }
    LeaveCriticalSection(&g_logLock);
}

/* ------------------------------------------------------------------ engine strings
 * Genie::StringA is a pointer to the characters, preceded by a 12-byte header
 * { length, capacity, refcount }. Refcount 0x7FFFFFFF marks a permanent string:
 * the engine never frees or rewrites it, so these can live in this DLL. */
typedef struct {
    int  length, capacity, refs;
    char text[64];
} EngineString;

static void estr_set(EngineString* s, const char* v)
{
    size_t n = strlen(v);
    if (n >= sizeof(s->text)) n = sizeof(s->text) - 1;
    memcpy(s->text, v, n);
    s->text[n] = 0;
    s->length = (int)n;
    s->capacity = (int)n;
    s->refs = 0x7FFFFFFF;
}

/* CKFOnline::StationInfo (PC layout, offsets confirmed by the Gold host getters:
 * PublicIP +4, PrivateIP +8, PublicPort +0x10, PrivatePort +0x12). */
typedef struct {
    BYTE  host, local, negotiated, locked;  /* +0x00 */
    char* publicIp;                         /* +0x04 */
    char* privateIp;                        /* +0x08 */
    char* name;                             /* +0x0C */
    WORD  publicPort, privatePort;          /* +0x10 */
    DWORD publicInet, privateInet;          /* +0x14 */
    DWORD stationId;                        /* +0x1C */
    char* did;                              /* +0x20 */
    BYTE  joinable, sameGame, pad[2];       /* +0x24 */
    BYTE  spare[32];                        /* room in case the Gold struct is longer */
} StationInfo;

static EngineString g_sPublic, g_sPrivate, g_sName, g_sDid;
static StationInfo  g_fakeHost;

static void set_fake_host(const char* name, const char* ip, const char* privIp, int port)
{
    estr_set(&g_sPublic, ip);
    estr_set(&g_sPrivate, privIp);
    estr_set(&g_sName, name);
    estr_set(&g_sDid, "{00000000-0000-0000-0000-000000000000}");
    memset(&g_fakeHost, 0, sizeof(g_fakeHost));
    g_fakeHost.host = 1;
    g_fakeHost.negotiated = 1;
    g_fakeHost.publicIp = g_sPublic.text;
    g_fakeHost.privateIp = g_sPrivate.text;
    g_fakeHost.name = g_sName.text;
    g_fakeHost.did = g_sDid.text;
    g_fakeHost.publicPort = (WORD)port;
    g_fakeHost.privatePort = (WORD)port;
    g_fakeHost.publicInet = inet_addr(ip);
    g_fakeHost.privateInet = inet_addr(privIp);
    g_fakeHost.joinable = 1;
    g_fakeHost.sameGame = 1;
}

static BYTE* join_flag_ptr(void)
{
    DWORD session = *(DWORD*)rt(VA_SESSION_PTR);
    return session ? (BYTE*)(session + SESSION_JOIN_FLAG) : NULL;
}

/* ------------------------------------------------------------------ hooks */
static int install_jmp(DWORD at, const BYTE* expect, int len, void* target)
{
    DWORD old;
    BYTE* p = (BYTE*)at;
    int i;
    if (expect && memcmp(p, expect, len) != 0) {
        logf("hook at %08X not installed: unexpected bytes %02X %02X %02X %02X %02X (unsupported build?)",
             rva(at), p[0], p[1], p[2], p[3], p[4]);
        return 0;
    }
    if (!VirtualProtect(p, len, PAGE_EXECUTE_READWRITE, &old)) return 0;
    p[0] = 0xE9;
    *(DWORD*)(p + 1) = (DWORD)target - (at + 5);
    for (i = 5; i < len; i++) p[i] = 0x90;
    VirtualProtect(p, len, old, &old);
    FlushInstructionCache(GetCurrentProcess(), p, len);
    return 1;
}

/* Stage 1. sp[0] = EFLAGS, sp[1..8] = pushad, sp[9] = return, sp[10] = signedIn, sp[11] = playerId. */
static void __cdecl on_signin(DWORD* sp)
{
    DWORD signedIn = sp[10] & 0xFF, player = sp[11];
    char chain[256];
    int i, n = 0, found = 0;
    for (i = 12; i < 12 + 96 && found < 8; i++) {
        DWORD v = sp[i];
        if (v >= g_textLo && v < g_textHi) {
            n += _snprintf(chain + n, sizeof(chain) - n, " %08X", rva(v));
            found++;
        }
    }
    chain[n < (int)sizeof(chain) ? n : (int)sizeof(chain) - 1] = 0;
    g_block = (!signedIn && g_blockSignout) ? 1 : 0;
    logf("DisneyIdSignIn(signedIn=%u, player=%u) caller=%08X%s stack:%s",
         signedIn, player, rva(sp[9]), g_block ? " -> BLOCKED (block_signout=1)" : "", chain);
    if (!signedIn && g_snapReady && !g_restoreDone && !g_restorePending) {
        g_restoreTick = GetTickCount();         /* the worker restores 3 s later */
        InterlockedExchange(&g_restorePending, 1);
    }
}

static __declspec(naked) void hook_signin(void)
{
    __asm {
        pushad
        pushfd
        mov  eax, esp
        push eax
        call on_signin
        add  esp, 4
        popfd
        popad
        cmp  g_block, 0
        jne  skip
        sub  esp, 8
        push ebx
        push ebp
        jmp  dword ptr [g_signinBack]
    skip:
        ret  8
    }
}

/* Stage 2: the level's Net-Z session is being opened. sp[9] = return address, sp[7] = ECX (this). */
static void __cdecl on_join_or_host(DWORD* sp)
{
    BYTE* flag = join_flag_ptr();
    int joining = flag ? *flag : -1;
    if (g_joinArmed && flag && joining == 0) {
        *flag = 1;                      /* something cleared it after F8: join anyway */
        joining = 1;
        logf("JoinOrHost: join flag was cleared, set again for the prepared join");
    }
    if (joining == 1) {
        logf("JoinOrHost(this=%08X) JOIN %s (host list: %s)", sp[7],
             g_joinArmed ? g_joinTarget : "<nothing prepared: the game has no host>", g_joinArmed ? "ours" : "engine");
        InterlockedExchange(&g_hosting, 0);
        InterlockedExchange(&g_unannounce, 1);          /* the worker removes us from the lobby */
        g_joinStartTick = GetTickCount();
        InterlockedExchange(&g_joinStarted, 1);
    } else {
        logf("JoinOrHost(this=%08X) HOST on UDP port %d (join flag %d)", sp[7], g_netPort, joining);
        InterlockedExchange(&g_fakeHostActive, 0);
        InterlockedExchange(&g_hosting, 1);
    }
}

static __declspec(naked) void hook_join_or_host(void)
{
    __asm {
        pushad
        pushfd
        mov  eax, esp
        push eax
        call on_join_or_host
        add  esp, 4
        popfd
        popad
        mov  eax, dword ptr fs:[0]      /* stolen prologue */
        jmp  dword ptr [g_joinOrHostBack]
    }
}

/* The Toy Box / playset menus call Network_SetJoinSession(false) right before loading their
 * level, which would undo the join prepared with F8. While a join is prepared, keep it true. */
static volatile LONG g_setJoinLogged;

static void __cdecl on_set_join(DWORD wanted)
{
    if (g_joinArmed && !(wanted & 0xFF) && !InterlockedExchange(&g_setJoinLogged, 1))
        logf("SetJoinSession(false) from the menu ignored: a join is prepared");
}

static __declspec(naked) void hook_set_join_session(void)
{
    __asm {
        pushad
        pushfd
        push dword ptr [esp + 40]       /* the bool argument (36 bytes saved + return address) */
        call on_set_join
        add  esp, 4
        popfd
        popad
        mov  al, byte ptr [esp + 4]
        cmp  g_joinArmed, 0
        je   store
        mov  al, 1
    store:
        mov  byte ptr [ecx + 14h], al
        ret  4
    }
}

/* --- client: JoinGame / PlayerListAllNegotiated / join data ---------------------------------- */
static DWORD g_joinGameImm, g_joinGameBack, g_negotiatedBack;
static DWORD g_savedJoinData;
static struct { char* gameName; char* inviterName; BYTE rest[504]; } g_fakeJoinData;

static void __cdecl on_join_game(DWORD* sp)
{
    /* sp[9] = return, sp[10] = data, sp[11] = password, sp[12] = callback, sp[13] = player cb, sp[14] = user */
    logf("JoinGame(data=%08X) -> success for the prepared join (%s); NAT negotiation skipped",
         sp[10], g_joinTarget);
    *(int*)rt(VA_ONLINE_CONNSTATE) = CONNECTION_IN_GAME;
}

/* While we play in a lobby host's Toy Box, keep the connection state at "in game". Failure states
 * (negative) still go through, so a real disconnect is still noticed. */
static __declspec(naked) void hook_set_conn_state(void)
{
    __asm {
        mov  eax, dword ptr [esp + 4]
        cmp  g_fakeHostActive, 0
        je   store
        cmp  eax, 0
        jl   store
        cmp  eax, 7                     /* CONNECTION_IN_GAME */
        jge  store
        mov  eax, 7
    store:
        mov  dword ptr [ecx + 40h], eax
        ret  4
    }
}

static __declspec(naked) void hook_join_game(void)
{
    __asm {
        cmp  g_joinArmed, 0
        je   original
        pushad
        pushfd
        mov  eax, esp
        push eax
        call on_join_game
        add  esp, 4
        popfd
        popad
        push dword ptr [esp + 14h]      /* user data (the GameNetworkLoop) */
        push 0                          /* JoinGameSuccess */
        call dword ptr [esp + 14h]      /* JoinGameCallback (cdecl) - [esp+0Ch] before the two pushes */
        add  esp, 8
        xor  eax, eax                   /* CKF_ONLINE_JOIN_ERROR_SUCCESS */
        ret  14h
    original:
        push -1                         /* stolen: push -1; push <SEH handler> */
        push g_joinGameImm
        jmp  dword ptr [g_joinGameBack]
    }
}

static __declspec(naked) void hook_all_negotiated(void)
{
    __asm {
        cmp  g_joinArmed, 0
        je   original
        mov  al, 1
        ret
    original:
        mov  eax, g_listHeadAddr        /* stolen: mov eax, [list head] */
        mov  eax, dword ptr [eax]
        jmp  dword ptr [g_negotiatedBack]
    }
}

static void set_join_data(int on)
{
    DWORD* slot = (DWORD*)rt(VA_ONLINE_JOINDATA);
    if (on) {
        if (*slot != (DWORD)&g_fakeJoinData) g_savedJoinData = *slot;
        memset(&g_fakeJoinData, 0, sizeof(g_fakeJoinData));
        g_fakeJoinData.gameName = g_sName.text;
        g_fakeJoinData.inviterName = g_sName.text;
        *slot = (DWORD)&g_fakeJoinData;
    } else if (*slot == (DWORD)&g_fakeJoinData) {
        *slot = g_savedJoinData;
    }
}

/* --- host: join requests -------------------------------------------------------------------- */
static DWORD g_joinReqImm, g_joinReqBack;

static int has_lock(const char* name)
{
    EngineString str;
    char* obj;
    DWORD fn = rt(VA_HAS_LOCK), online = rt(VA_ONLINE);
    int r = 0;
    estr_set(&str, name);
    obj = str.text;
    __asm {
        lea  eax, obj
        push eax
        mov  ecx, online
        call fn
        movzx eax, al
        mov  r, eax
    }
    return r;
}

static void __cdecl on_join_request(DWORD* sp)
{
    static const char* locks[] = { "MultiplayerLock", "LevelLock", "MatchingLock", "JoinLock", "SpeedwayTransition" };
    char held[160] = "";
    int i, n = 0;
    (void)sp;
    for (i = 0; i < 5; i++)
        if (has_lock(locks[i]))
            n += _snprintf(held + n, sizeof(held) - n, "%s%s", n ? ", " : "", locks[i]);
    logf("a player is joining this host; locks held: %s%s", n ? held : "none",
         n ? (g_approveAll ? " -> approved anyway (approve_all=1)" : " -> the game will DENY it") : "");
}

static __declspec(naked) void hook_join_request(void)
{
    __asm {
        pushad
        pushfd
        mov  eax, esp
        push eax
        call on_join_request
        add  esp, 4
        popfd
        popad
        push -1
        push g_joinReqImm
        jmp  dword ptr [g_joinReqBack]
    }
}

/* --- every network error the game reports --------------------------------------------------- */
static DWORD g_netErrorOrig;

static void __cdecl on_net_error(DWORD* sp)
{
    /* sp[9] = return, sp[10] = message, sp[11], sp[12], sp[13] = source file, sp[14] = line */
    const char* msg = (const char*)sp[10];
    const char* file = (const char*)sp[13];
    const char* slash;
    if (IsBadStringPtrA(msg, 256)) msg = "?";
    if (IsBadStringPtrA(file, 512)) file = "?";
    slash = strrchr(file, '/');
    if (!slash) slash = strrchr(file, '\\');
    logf("NETWORK ERROR reported: %s  [%s:%d, args %u %u, caller %08X]", msg, slash ? slash + 1 : file,
         (int)sp[14], sp[11], sp[12], rva(sp[9]));
}

static __declspec(naked) void hook_net_error(void)
{
    __asm {
        pushad
        pushfd
        mov  eax, esp
        push eax
        call on_net_error
        add  esp, 4
        popfd
        popad
        jmp  dword ptr [g_netErrorOrig]
    }
}

static void hook_net_error_vtable(void)
{
    DWORD obj = *(DWORD*)rt(VA_NETERROR_PTR), *vt, old;
    if (!obj || g_netErrorOrig) return;
    vt = *(DWORD**)obj;
    if (!vt || vt[0] < g_textLo || vt[0] >= g_textHi) return;
    if (!VirtualProtect(&vt[0], 4, PAGE_READWRITE, &old)) return;
    g_netErrorOrig = vt[0];
    vt[0] = (DWORD)hook_net_error;
    VirtualProtect(&vt[0], 4, old, &old);
    logf("logging network errors (g_netError vtable slot 0 was %08X)", rva(g_netErrorOrig));
}

static __declspec(naked) void hook_playerlist_host(void)
{
    __asm {
        cmp  g_fakeHostActive, 0
        je   original
        lea  eax, g_fakeHost
        ret
    original:
        mov  eax, g_listHeadAddr        /* stolen: mov eax, [list head] */
        mov  eax, dword ptr [eax]
        jmp  dword ptr [g_hostBack]
    }
}

static int pin_netz_port(int port)
{
    BYTE* p = (BYTE*)rt(VA_NETZ_PORT_PUSHES);
    DWORD old;
    if (port <= 0) return 0;
    if (memcmp(p, kPortPushes, sizeof(kPortPushes)) != 0) {
        logf("Net-Z port not pinned: unexpected bytes at %08X", VA_NETZ_PORT_PUSHES);
        return 0;
    }
    if (!VirtualProtect(p, 10, PAGE_EXECUTE_READWRITE, &old)) return 0;
    *(DWORD*)(p + 1) = (DWORD)port;     /* max */
    *(DWORD*)(p + 6) = (DWORD)port;     /* min: RandomPort returns min + rand() % (max - min + 1) */
    VirtualProtect(p, 10, old, &old);
    FlushInstructionCache(GetCurrentProcess(), p, 10);
    return 1;
}

/* ------------------------------------------------------------------ stage 3: saves */
/* ------------------------------------------------------------------ multiplayer in every level
 * GameSession::IsMultiplayerAllowedInLevelID allows network play only in
 * Toy Boxes / holodecks that are not the intro level, unless the zone data "NetworkMultiplayerAllowed" says
 * otherwise. A guest in such a level is kicked ("I_Got_Kicked"), the level is locked
 * (LevelLock) and the Toy Box doors warn "will end the online multiplayer session".
 * All of them read that key through ZoneMgr::GetZoneDataBool(key, zone, bool, default). With
 * [net] force_multiplayer_levels=1 that key answers true except in playsets (zone data WorldIsPlayset):
 * Disney shipped playsets without network play (only a dev switch, allowPlaysetNetworking) and their
 * missions / checkpoints / cutscenes are not replicated, so a guest freezes there; the game's own answer is
 * kept and a playset ends the session cleanly. force_multiplayer_levels=2 forces playsets too
 * (experimental: the guest freezes). Other keys are untouched. */
#define VA_ZONE_DATA_BOOL   0x0139DE80u   /* bool __thiscall ZoneMgr::GetZoneDataBool(const char*, zone, bool, bool) */
#define VA_KEY_NET_MP       0x01CD3C18u   /* "NetworkMultiplayerAllowed" */
static const BYTE kZoneDataBool[6] = { 0x83, 0xEC, 0x08, 0x56, 0x8B, 0xF1 };   /* sub esp,8; push esi; mov esi,ecx */
static DWORD g_zoneBoolBack, g_keyNetMp;
static int   g_forceMpLevels = 1;

/* the original function (stolen prologue + jump back), callable as __fastcall(this, unused edx, ...):
 * like __thiscall it takes this in ECX and pops its 4 stack arguments */
static __declspec(naked) BYTE __fastcall zone_bool_orig(void* zm, void* unused, const char* key, DWORD zone,
                                                        BYTE b, BYTE def)
{
    __asm {
        sub  esp, 8
        push esi
        mov  esi, ecx
        jmp  dword ptr [g_zoneBoolBack]
    }
}

static BYTE __fastcall zone_bool_netmp(void* zm, void* unused, const char* key, DWORD zone, BYTE b, BYTE def)
{
    if (g_forceMpLevels >= 2) return 1;
    if (zone_bool_orig(zm, NULL, "WorldIsPlayset", zone, 0, 0))
        return zone_bool_orig(zm, NULL, key, zone, b, def);    /* playset: the game's own rule */
    return 1;
}

static __declspec(naked) void hook_zone_data_bool(void)
{
    __asm {
        mov  eax, dword ptr [esp + 4]   /* the key */
        cmp  eax, g_keyNetMp
        jne  original
        xor  edx, edx                   /* same stack as the call: this in ECX, 4 arguments, pops them */
        jmp  zone_bool_netmp
    original:
        sub  esp, 8                     /* stolen prologue */
        push esi
        mov  esi, ecx
        jmp  dword ptr [g_zoneBoolBack]
    }
}

static void force_multiplayer_levels(void)
{
    DWORD at = rt(VA_ZONE_DATA_BOOL);
    g_keyNetMp = rt(VA_KEY_NET_MP);
    if (memcmp((const char*)g_keyNetMp, "NetworkMultiplayerAllowed", 26) != 0) {
        logf("multiplayer levels: key string not found at %08X, not patched", VA_KEY_NET_MP);
        return;
    }
    g_zoneBoolBack = at + 6;
    if (install_jmp(at, kZoneDataBool, 6, hook_zone_data_bool))
        logf(g_forceMpLevels >= 2 ? "multiplayer allowed in every level, playsets too (force_multiplayer_levels=2)"
                                  : "multiplayer allowed in every level except playsets (force_multiplayer_levels=1)");
}

/* ------------------------------------------------------------------ zone ids that do not depend on mods
 * ZoneMgr::LoadZoneList (0x0139E820) walks the Lua table ZoneList.Records.Zones with lua_next and numbers each
 * zone 0x401 + (PlaysetUniqueID << 8) + n, n = how many zones of that playset it met before. Network play sends
 * these ids (the host's level), but lua_next follows the table's hash layout: a mod that adds zones (the
 * Monsters U port adds MU_Master / MU_Campus to ToyBox) reorders the walk, and the same id is another level on
 * the other PC (the host plays KRT_TBSW_RacHub 0x622, the guest loads its own 0x622 = TBX_Building_Logic4).
 * So every zone of the unmodified game gets its unmodified id by name (zones_vanilla.h, read from the running
 * game with read_zones.py), and zones added by mods take the next ids of their playset in the order the game
 * meets them. An unmodified game keeps every id it had. */
#include "zones_vanilla.h"
#define VA_ZONE_MGR       0x0225F548u  /* ZoneMgr*: +0x278 hash map id -> Zone* (buckets +0x14, bucket count +0x1C,
                                          node { next, hash, id, Zone* }); Zone +0x2C = name; +0x19C current zone */
#define ZONE_MAP_OFS      0x278
#define ZONE_NAME_OFS     0x2C
#define VA_ZONE_ID_STORE  0x0139EE87u  /* LoadZoneList: mov [esp+24h], ecx (the new id); push edx */
#define VA_MAP_INSERT     0x0067A2F0u  /* map[*key] = *value, thiscall(const DWORD* key, DWORD* value) */
#define MAX_ZONES         1024
static const BYTE kZoneIdStore[5] = { 0x89, 0x4C, 0x24, 0x24, 0x52 };
typedef void* (__fastcall *map_insert_fn)(void* map, void* unused, const DWORD* key, DWORD* value);
typedef struct { DWORD id; DWORD zone; const char* name; } ZoneRow;
static DWORD g_zoneIdBack;
static int   g_zoneVanillaCount[64];   /* zones of each playset in the unmodified game */
static int   g_zoneExtra[64];          /* zones added by mods, per playset */
static int   g_zonesMoved, g_zonesAdded, g_zonesReported;
static char  g_zonesFp[24];            /* fingerprint of the ids of the unmodified zones + how many mods added */

static DWORD vanilla_zone_id(const char* name)
{
    int i;
    for (i = 0; i < (int)(sizeof(kVanillaZones) / sizeof(kVanillaZones[0])); i++)
        if (!strcmp(kVanillaZones[i].name, name)) return kVanillaZones[i].id;
    return 0;
}

static DWORD canonical_zone_id(DWORD uid, const char* name, DWORD gameId)
{
    DWORD id;
    if (uid >= 64 || !name) return gameId;
    id = vanilla_zone_id(name);
    if (id && ((id - 0x401) >> 8) == uid) {
        if (id != gameId) g_zonesMoved++;
        return id;
    }
    if (!g_zoneVanillaCount[uid]) return gameId;      /* a playset only mods use: the game's own numbering */
    if (g_zoneVanillaCount[uid] + g_zoneExtra[uid] > 0xFF) return gameId;
    g_zonesAdded++;
    return 0x401 + (uid << 8) + g_zoneVanillaCount[uid] + g_zoneExtra[uid]++;
}

static DWORD __cdecl on_zone_id(DWORD uid, const char* name, DWORD gameId)
{
    return canonical_zone_id(uid, name, gameId);
}

static __declspec(naked) void hook_zone_id(void)
{
    __asm {
        push eax                        /* the playset's zone count + 1, stored right after */
        push edx                        /* &new id, pushed right after */
        push ecx                        /* the game's id */
        push dword ptr [esp + 0x24]     /* [esp+18h] of the game: the zone name (Genie::StringA = char*) */
        push dword ptr [edi + 4]        /* the playset record: PlaysetUniqueID */
        call on_zone_id
        add  esp, 12
        mov  ecx, eax
        pop  edx
        pop  eax
        mov  dword ptr [esp + 0x24], ecx    /* stolen */
        push edx
        jmp  dword ptr [g_zoneIdBack]
    }
}

static DWORD zone_map(void)
{
    DWORD zm = *(DWORD*)rt(VA_ZONE_MGR);
    return zm ? zm + ZONE_MAP_OFS : 0;
}

static int cmp_zone_row(const void* a, const void* b)
{
    DWORD x = ((const ZoneRow*)a)->id, y = ((const ZoneRow*)b)->id;
    return x < y ? -1 : x > y;
}

/* the zones the game has now, sorted by id; 0 before the zone list is loaded */
static int zone_rows(ZoneRow* rows, int max)
{
    DWORD map = zone_map(), buckets, count, i, node;
    int n = 0;
    if (!map) return 0;
    buckets = *(DWORD*)(map + 0x14);
    count = *(DWORD*)(map + 0x1C);
    if (!buckets || count > 0x10000) return 0;
    for (i = 0; i < count; i++)
        for (node = *(DWORD*)(buckets + 4 * i); node && n < max; node = *(DWORD*)node) {
            DWORD zone = *(DWORD*)(node + 0xC);
            const char* name = zone ? *(const char**)(zone + ZONE_NAME_OFS) : NULL;
            rows[n].id = *(DWORD*)(node + 8);
            rows[n].zone = zone;
            rows[n].name = name ? name : "";
            n++;
        }
    qsort(rows, n, sizeof(ZoneRow), cmp_zone_row);
    return n;
}

/* id of the zone named <name> on this PC (level names come in any case), 0 if this game does not have it */
static DWORD zone_id_by_name(const char* name)
{
    ZoneRow rows[MAX_ZONES];
    int i, n = zone_rows(rows, MAX_ZONES);
    for (i = 0; i < n; i++)
        if (!_stricmp(rows[i].name, name)) return rows[i].id;
    return 0;
}

/* The zone list was loaded before this DLL (CrabeLoader loads it from the first Lua state): give the zones
 * their ids now. Every playset keeps the same set of ids, only who has which changes, so writing map[new id]
 * for every zone that moves leaves no stale entry. */
static void renumber_loaded_zones(ZoneRow* rows, int n)
{
    static DWORD newIds[MAX_ZONES];
    map_insert_fn insert = (map_insert_fn)rt(VA_MAP_INSERT);
    DWORD map = zone_map();
    int i, moved = 0;
    for (i = 0; i < n; i++)                         /* sorted by id = the order the game met them */
        newIds[i] = canonical_zone_id((rows[i].id - 0x401) >> 8, rows[i].name, rows[i].id);
    for (i = 0; i < n; i++)
        if (newIds[i] != rows[i].id) {
            insert((void*)map, NULL, &newIds[i], &rows[i].zone);
            moved++;
        }
    logf("zone ids: the zone list was already loaded; %d zone(s) renumbered", moved);
}

static void canonical_zone_ids(void)
{
    static ZoneRow rows[MAX_ZONES];
    int i, n;
    for (i = 0; i < (int)(sizeof(kVanillaZones) / sizeof(kVanillaZones[0])); i++) {
        DWORD uid = (kVanillaZones[i].id - 0x401) >> 8, k = (kVanillaZones[i].id - 0x401) & 0xFF;
        if (uid < 64 && (int)k + 1 > g_zoneVanillaCount[uid]) g_zoneVanillaCount[uid] = k + 1;
    }
    n = zone_rows(rows, MAX_ZONES);
    if (n > 0) {
        renumber_loaded_zones(rows, n);
        return;
    }
    g_zoneIdBack = rt(VA_ZONE_ID_STORE) + 5;
    if (install_jmp(rt(VA_ZONE_ID_STORE), kZoneIdStore, 5, hook_zone_id))
        logf("zone ids: unmodified ids for the unmodified zones (the list is not loaded yet)");
}

/* once the zone list is loaded: log it and make the fingerprint other players compare with ours.
 * It covers the ids of the unmodified zones only (equal on every PC this DLL renumbered); "-N" is how many
 * zones mods added. "" while the list is not complete (every unmodified zone present). */
static const char* zones_fingerprint(void)
{
    ZoneRow rows[MAX_ZONES];
    char fp[24];
    DWORD h = 2166136261u;
    int i, n, extra = 0, found = 0;
    const char* p;
    if (g_zonesFp[0]) return g_zonesFp;
    n = zone_rows(rows, MAX_ZONES);
    for (i = 0; i < n; i++) {
        char id[12];
        if (vanilla_zone_id(rows[i].name) == 0) { extra++; continue; }
        found++;
        _snprintf(id, sizeof(id), "%X=", rows[i].id);
        for (p = id; *p; p++) h = (h ^ (BYTE)*p) * 16777619u;
        for (p = rows[i].name; *p; p++) h = (h ^ (BYTE)*p) * 16777619u;
        h = (h ^ ';') * 16777619u;
    }
    if (found < (int)(sizeof(kVanillaZones) / sizeof(kVanillaZones[0]))) return "";
    _snprintf(fp, sizeof(fp), "%08X-%d", h, extra);
    strcpy(g_zonesFp, fp);
    if (!g_zonesReported) {
        int wrong = 0;
        g_zonesReported = 1;
        for (i = 0; i < n; i++) {
            DWORD v = vanilla_zone_id(rows[i].name);
            if (v && v != rows[i].id) wrong++;
        }
        logf("zone ids: %d zones (%d added by mods), %d moved back to their unmodified id, %d added after them, "
             "%d still different, fingerprint %s", n, extra, g_zonesMoved, g_zonesAdded, wrong, g_zonesFp);
        for (i = 0; i < n; i++)
            if (!vanilla_zone_id(rows[i].name)) logf("zone ids: added by a mod: %s = %X", rows[i].name, rows[i].id);
    }
    return g_zonesFp;
}

static int patch_local_toybox_list(void)
{
    BYTE* p = (BYTE*)rt(VA_SAVELIST_UGC_TEST);
    DWORD old;
    if (memcmp(p, kSaveListUgcTest, sizeof(kSaveListUgcTest)) != 0) {
        logf("local Toy Box list: unexpected bytes at %08X, not patched", VA_SAVELIST_UGC_TEST);
        return 0;
    }
    if (!VirtualProtect(p + 2, 1, PAGE_EXECUTE_READWRITE, &old)) return 0;
    p[2] = 0xEB;                                /* je local -> jmp local */
    VirtualProtect(p + 2, 1, old, &old);
    FlushInstructionCache(GetCurrentProcess(), p + 2, 1);
    return 1;
}

static int is_user_dir(const char* n)
{
    if (_strnicmp(n, "user", 4) != 0 || !n[4]) return 0;
    for (n += 4; *n; n++)
        if (!isdigit((unsigned char)*n)) return 0;
    return 1;
}

/* Calls cb for every file of <root>\<account>\SavedGame\user<N>\ (rel = path below root). */
typedef void (*save_file_cb)(const char* rel, const char* full, int* count);
static void walk_saves(const char* root, save_file_cb cb, int* count)
{
    WIN32_FIND_DATAA a, u, f;
    HANDLE ha, hu, hf;
    char p[MAX_PATH * 2], rel[MAX_PATH];
    _snprintf(p, sizeof(p), "%s\\*", root);
    if ((ha = FindFirstFileA(p, &a)) == INVALID_HANDLE_VALUE) return;
    do {
        if (!(a.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) || a.cFileName[0] == '.') continue;
        _snprintf(p, sizeof(p), "%s\\%s\\SavedGame\\*", root, a.cFileName);
        if ((hu = FindFirstFileA(p, &u)) == INVALID_HANDLE_VALUE) continue;
        do {
            if (!(u.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) || !is_user_dir(u.cFileName)) continue;
            _snprintf(p, sizeof(p), "%s\\%s\\SavedGame\\%s\\*", root, a.cFileName, u.cFileName);
            if ((hf = FindFirstFileA(p, &f)) == INVALID_HANDLE_VALUE) continue;
            do {
                if (f.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) continue;
                _snprintf(rel, sizeof(rel), "%s\\SavedGame\\%s\\%s", a.cFileName, u.cFileName, f.cFileName);
                rel[sizeof(rel) - 1] = 0;
                _snprintf(p, sizeof(p), "%s\\%s", root, rel);
                p[sizeof(p) - 1] = 0;
                cb(rel, p, count);
            } while (FindNextFileA(hf, &f));
            FindClose(hf);
        } while (FindNextFileA(hu, &u));
        FindClose(hu);
    } while (FindNextFileA(ha, &a));
    FindClose(ha);
}

static void make_parents(char* path)
{
    char* s;
    for (s = strchr(path + 3, '\\'); s; s = strchr(s + 1, '\\')) {
        *s = 0;
        CreateDirectoryA(path, NULL);
        *s = '\\';
    }
}

static void snapshot_file(const char* rel, const char* full, int* count)
{
    char dst[MAX_PATH * 2];
    _snprintf(dst, sizeof(dst), "%s\\%s", g_snapDir, rel);
    dst[sizeof(dst) - 1] = 0;
    make_parents(dst);
    if (CopyFileA(full, dst, FALSE)) (*count)++;
    else logf("saves: could not back up %s (error %lu)", rel, GetLastError());
}

static void restore_file(const char* rel, const char* snap, int* count)
{
    char dst[MAX_PATH * 2];
    _snprintf(dst, sizeof(dst), "%s\\%s", g_saveRoot, rel);
    dst[sizeof(dst) - 1] = 0;
    if (GetFileAttributesA(dst) != INVALID_FILE_ATTRIBUTES) return;     /* still there (maybe changed): keep it */
    make_parents(dst);
    if (CopyFileA(snap, dst, TRUE)) {
        (*count)++;
        logf("saves: restored %s", rel);
    } else {
        logf("saves: could not restore %s (error %lu)", rel, GetLastError());
    }
}

static void delete_tree(const char* dir)
{
    WIN32_FIND_DATAA f;
    HANDLE h;
    char p[MAX_PATH * 2];
    _snprintf(p, sizeof(p), "%s\\*", dir);
    if ((h = FindFirstFileA(p, &f)) != INVALID_HANDLE_VALUE) {
        do {
            if (!strcmp(f.cFileName, ".") || !strcmp(f.cFileName, "..")) continue;
            _snprintf(p, sizeof(p), "%s\\%s", dir, f.cFileName);
            if (f.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) delete_tree(p);
            else DeleteFileA(p);
        } while (FindNextFileA(h, &f));
        FindClose(h);
    }
    RemoveDirectoryA(dir);
}

static int cmp_names(const void* a, const void* b) { return strcmp((const char*)a, (const char*)b); }

/* Keeps the newest SAVE_SNAPSHOTS_KEPT folders <base>\YYYYMMDD_HHMMSS. */
static void prune_snapshots(const char* base)
{
    static char names[256][16];
    WIN32_FIND_DATAA f;
    HANDLE h;
    char p[MAX_PATH * 2];
    int n = 0, i;
    _snprintf(p, sizeof(p), "%s\\????????_??????", base);
    if ((h = FindFirstFileA(p, &f)) == INVALID_HANDLE_VALUE) return;
    do {
        if ((f.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) && strlen(f.cFileName) == 15 && n < 256)
            strcpy(names[n++], f.cFileName);
    } while (FindNextFileA(h, &f));
    FindClose(h);
    qsort(names, n, sizeof(names[0]), cmp_names);
    for (i = 0; i < n - SAVE_SNAPSHOTS_KEPT; i++) {
        _snprintf(p, sizeof(p), "%s\\%s", base, names[i]);
        delete_tree(p);
    }
}

static void snapshot_saves(void)
{
    char base[MAX_PATH], *slash;
    SYSTEMTIME t;
    int count = 0;
    if (!GetEnvironmentVariableA("APPDATA", g_saveRoot, MAX_PATH)) return;
    strncat(g_saveRoot, "\\Disney Interactive\\Disney Infinity 3.0", MAX_PATH - strlen(g_saveRoot) - 1);
    GetModuleFileNameA(NULL, base, MAX_PATH);
    if ((slash = strrchr(base, '\\')) != NULL) *slash = 0;
    strncat(base, "\\di_saves", MAX_PATH - strlen(base) - 1);
    CreateDirectoryA(base, NULL);
    GetLocalTime(&t);
    _snprintf(g_snapDir, MAX_PATH, "%s\\%04d%02d%02d_%02d%02d%02d", base, t.wYear, t.wMonth, t.wDay,
              t.wHour, t.wMinute, t.wSecond);
    walk_saves(g_saveRoot, snapshot_file, &count);
    if (!count) {
        logf("saves: nothing to back up in %s", g_saveRoot);
        g_snapDir[0] = 0;
        return;
    }
    logf("saves: %d files backed up to %s; the first sign-out restores the ones the migration removes",
         count, g_snapDir);
    InterlockedExchange(&g_snapReady, 1);
    prune_snapshots(base);
}

static void restore_saves(void)
{
    int count = 0;
    walk_saves(g_snapDir, restore_file, &count);
    logf("saves: migration finished, %d removed file(s) restored from %s", count, g_snapDir);
}

/* ------------------------------------------------------------------ server (WinHTTP) */
/* a request to any server (the directory pings the ones it lists); timeoutMs bounds every step */
static int http_to(const char* host, int port, int timeoutMs, const wchar_t* verb, const char* path,
                   const char* body, char* out, int outSize)
{
    HINTERNET s = NULL, c = NULL, r = NULL;
    wchar_t whost[128], wpath[256];
    DWORD status = 0, len = sizeof(status), got = 0, total = 0;
    int ok = 0;
    MultiByteToWideChar(CP_UTF8, 0, host, -1, whost, 128);
    MultiByteToWideChar(CP_UTF8, 0, path, -1, wpath, 256);
    if (out && outSize) out[0] = 0;
    s = WinHttpOpen(L"dinet_bk/4",                 /* protocol version: 3 = text chat, 4 = server list */ WINHTTP_ACCESS_TYPE_NO_PROXY, WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
    if (!s) goto done;
    WinHttpSetTimeouts(s, timeoutMs, timeoutMs, timeoutMs, timeoutMs + 2000);
    c = WinHttpConnect(s, whost, (INTERNET_PORT)port, 0);
    if (!c) goto done;
    r = WinHttpOpenRequest(c, verb, wpath, NULL, WINHTTP_NO_REFERER, WINHTTP_DEFAULT_ACCEPT_TYPES, 0);
    if (!r) goto done;
    if (!WinHttpSendRequest(r, L"Content-Type: application/x-www-form-urlencoded\r\n", (DWORD)-1L,
                            (LPVOID)body, body ? (DWORD)strlen(body) : 0, body ? (DWORD)strlen(body) : 0, 0))
        goto done;
    if (!WinHttpReceiveResponse(r, NULL)) goto done;
    WinHttpQueryHeaders(r, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER, NULL, &status, &len, NULL);
    while (out && total < (DWORD)outSize - 1 && WinHttpReadData(r, out + total, outSize - 1 - total, &got) && got)
        total += got;
    if (out) out[total] = 0;
    ok = (status == 200);
done:
    if (r) WinHttpCloseHandle(r);
    if (c) WinHttpCloseHandle(c);
    if (s) WinHttpCloseHandle(s);
    return ok;
}

static int http(const wchar_t* verb, const char* path, const char* body, char* out, int outSize)
{
    return http_to(g_serverHost, g_serverPort, 3000, verb, path, body, out, outSize);
}

static void url_encode(const char* in, char* out, int size)
{
    static const char hex[] = "0123456789ABCDEF";
    int n = 0;
    for (; *in && n < size - 4; in++) {
        unsigned char ch = (unsigned char)*in;
        if (isalnum(ch) || ch == '-' || ch == '_' || ch == '.') out[n++] = (char)ch;
        else { out[n++] = '%'; out[n++] = hex[ch >> 4]; out[n++] = hex[ch & 15]; }
    }
    out[n] = 0;
}

/* Address of this PC on the network that reaches the server (no packet is sent). */
static void detect_private_ip(void)
{
    WSADATA w;
    SOCKET s;
    struct sockaddr_in a, me;
    int len = sizeof(me);
    struct hostent* he;
    if (WSAStartup(MAKEWORD(2, 2), &w) != 0) return;
    memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_port = htons((u_short)g_serverPort);
    a.sin_addr.s_addr = inet_addr(g_serverHost);
    if (a.sin_addr.s_addr == INADDR_NONE && (he = gethostbyname(g_serverHost)) != NULL)
        memcpy(&a.sin_addr, he->h_addr_list[0], 4);
    if (a.sin_addr.s_addr == htonl(INADDR_LOOPBACK))
        a.sin_addr.s_addr = inet_addr("10.255.255.255");     /* server on this PC: use the LAN route */
    s = socket(AF_INET, SOCK_DGRAM, 0);
    if (s != INVALID_SOCKET) {
        if (connect(s, (struct sockaddr*)&a, sizeof(a)) == 0 && getsockname(s, (struct sockaddr*)&me, &len) == 0)
            strncpy(g_privateIp, inet_ntoa(me.sin_addr), sizeof(g_privateIp) - 1);
        closesocket(s);
    }
}

/* ------------------------------------------------------------------ F8 / F9 and the lobby */
static HWND game_window(void)
{
    HWND w = GetForegroundWindow();
    DWORD pid = 0;
    GetWindowThreadProcessId(w, &pid);
    return pid == GetCurrentProcessId() ? w : NULL;
}

static void announce(void)
{
    char body[384], name[128], world[96], zones[48];
    url_encode(g_name, name, sizeof(name));
    url_encode(g_myWorld, world, sizeof(world));
    url_encode(zones_fingerprint(), zones, sizeof(zones));
    _snprintf(body, sizeof(body), "name=%s&port=%d&private_ip=%s&world=%s&zones=%s", name, g_netPort, g_privateIp,
              world, zones);
    body[sizeof(body) - 1] = 0;
    if (!http(L"POST", "/dinet/v1/host", body, NULL, 0))
        logf("lobby: could not announce this host to %s:%d", g_serverHost, g_serverPort);
}

/* Makes the next Toy Box load join this host instead of hosting. 0 if the game has no session yet.
 * We keep announcing our own game until the join really starts (JoinOrHost in join mode), and the worker
 * cancels a join that has not started after JOIN_ARMED_TIMEOUT, so a failed attempt never leaves us
 * invisible to the other players. */
static void pending_world_for(const char* host);    /* stage 4: the host's level from the lobby */
static const char* zones_mismatch(const char* host);   /* the host's level is missing / numbered otherwise here */

static int prepare_join(const char* name, const char* ip, const char* priv, int port)
{
    BYTE* flag = join_flag_ptr();
    if (!flag) return 0;
    pending_world_for(name);        /* the main menu loads the HOST's level: another level gets us kicked */
    set_fake_host(name, ip, priv, port);
    _snprintf(g_joinTarget, sizeof(g_joinTarget), "%s %s/%s:%d", name, ip, priv, port);
    g_joinTarget[sizeof(g_joinTarget) - 1] = 0;
    InterlockedExchange(&g_joinStarted, 0);
    InterlockedExchange(&g_setJoinLogged, 0);
    InterlockedExchange(&g_fakeHostActive, 1);
    InterlockedExchange(&g_joinArmed, 1);
    set_join_data(1);
    *flag = 1;
    g_joinArmedTick = GetTickCount();
    logf("join prepared: %s (cancelled if it has not started within %d s)", g_joinTarget, JOIN_ARMED_TIMEOUT / 1000);
    return 1;
}

static void unannounce(void)
{
    char path[192], enc[128];
    url_encode(g_name, enc, sizeof(enc));
    _snprintf(path, sizeof(path), "/dinet/v1/host?name=%s", enc);
    path[sizeof(path) - 1] = 0;
    http(L"DELETE", path, NULL, NULL, 0);
}

/* ------------------------------------------------------------------ server directory (F8 in main.lua)
 * main.lua draws the list; this DLL fetches it so the game never waits on the network:
 *   servers:refresh   start a new fetch (worker thread)
 *   servers           "<state>\n" then one line per server:
 *                     name TAB url TAB description TAB online(0/1) TAB players TAB ping ms
 *                     TAB running(1 = this session uses it) TAB chosen(1 = di_net.ini [server] url) TAB usable(1 = the
 *                     url fits the exe: http://, at most 22 characters)
 *                     state: "loading", "ready", or "none" (nothing asked yet)
 *   useserver:<url>   [server] url = <url> for the next game start (dinet_bk rewrites the exe's server url in memory
 *                     at start-up, see rewrite_exe_urls); "ok", or "long" / "bad"
 * The list = the servers the current server hands out (GET /dinet/v1/servers: itself first, then its
 * servers.json), plus [servers] of di_net.ini (lines "<any key> = Name|http://host:port|description"), plus the
 * current server if neither has it. Every entry is pinged with GET /dinet/v1/hello (2 s at most). */
static const char kJoinReady[] =               /* the in-game invite / join flows show it */
    "Union preparada.\n\nAhora sal al menu principal y entra a una Toy Box: el juego se conectara a esa "
    "partida en vez de abrir la tuya.\n\nSi no entras en 2 minutos se cancela sola. F9 cancela.";

#define DIR_MAX 16
struct dir_entry { char name[48], url[128], desc[128], key[128]; };
static CRITICAL_SECTION g_dirLock;
static char g_dirText[8192] = "none\n";
static volatile LONG g_dirWanted, g_dirBusy;
static char g_runningUrl[128];                  /* the server this session talks to (url at start-up) */

static int split_url(const char* url, char* host, int hostSize, int* port)
{
    const char* h = strstr(url, "://");
    const char* end;
    int n;
    h = h ? h + 3 : url;
    end = h + strcspn(h, ":/");
    n = (int)(end - h);
    if (n <= 0 || n >= hostSize) return 0;
    memcpy(host, h, n);
    host[n] = 0;
    *port = *end == ':' ? atoi(end + 1) : 80;
    return *port > 0 && *port < 65536;
}

static void url_norm(const char* in, char* out, int size)
{
    char tmp[256];
    int n;
    strncpy(tmp, in, sizeof(tmp) - 1);
    tmp[sizeof(tmp) - 1] = 0;
    n = (int)strlen(tmp);
    while (n > 0 && (tmp[n - 1] == '/' || tmp[n - 1] == ' ')) tmp[--n] = 0;
    CharLowerA(tmp);
    strncpy(out, tmp, size - 1);
    out[size - 1] = 0;
}

static int url_usable(const char* url)
{
    return !strncmp(url, "http://", 7) && strlen(url) <= 22 && !strpbrk(url, " \t\r\n\"");
}

static void dir_add(struct dir_entry* e, int* n, const char* name, const char* url, const char* desc)
{
    char key[128];
    int j;
    url_norm(url, key, sizeof(key));
    if (!key[0] || *n >= DIR_MAX) return;
    for (j = 0; j < *n; j++) if (!strcmp(e[j].key, key)) return;
    strncpy(e[*n].name, name, sizeof(e[*n].name) - 1);
    e[*n].name[sizeof(e[*n].name) - 1] = 0;
    strncpy(e[*n].url, url, sizeof(e[*n].url) - 1);
    e[*n].url[sizeof(e[*n].url) - 1] = 0;
    strncpy(e[*n].desc, desc, sizeof(e[*n].desc) - 1);
    e[*n].desc[sizeof(e[*n].desc) - 1] = 0;
    strcpy(e[*n].key, key);
    (*n)++;
}

static void fetch_directory(void)
{
    static struct dir_entry e[DIR_MAX];
    static char list[8192], out[8192], section[4096];
    char chosen[128], run[128], line[512], *p, *next;
    int n = 0, i, len;
    GetPrivateProfileStringA("server", "url", "", chosen, sizeof(chosen), g_iniPath);
    url_norm(chosen, chosen, sizeof(chosen));
    url_norm(g_runningUrl, run, sizeof(run));
    if (http(L"GET", "/dinet/v1/servers", NULL, list, sizeof(list))) {
        for (p = list; p && *p; p = next) {
            char name[48] = "", url[128] = "", desc[128] = "";
            next = strchr(p, '\n');
            if (next) *next++ = 0;
            if (sscanf(p, "%47[^\t]\t%127[^\t\r]\t%127[^\r]", name, url, desc) >= 2) dir_add(e, &n, name, url, desc);
        }
    }
    if (GetPrivateProfileSectionA("servers", section, sizeof(section), g_iniPath)) {
        for (p = section; *p; p += strlen(p) + 1) {
            char name[48] = "", url[128] = "", desc[128] = "";
            const char* v = strchr(p, '=');
            if (v && sscanf(v + 1, " %47[^|]|%127[^|]|%127[^\r\n]", name, url, desc) >= 2) dir_add(e, &n, name, url, desc);
        }
    }
    {
        char cur[160];
        _snprintf(cur, sizeof(cur), "http://%s:%d", g_serverHost, g_serverPort);
        cur[sizeof(cur) - 1] = 0;
        dir_add(e, &n, "Current server", g_runningUrl[0] ? g_runningUrl : cur, "");
    }
    len = _snprintf(out, sizeof(out), "ready\n");
    for (i = 0; i < n; i++) {
        char host[128], hello[256], sname[48] = "";
        int port, online = 0, players = 0;
        DWORD t0 = GetTickCount(), ms = 0;
        if (split_url(e[i].url, host, sizeof(host), &port) &&
            http_to(host, port, 2000, L"GET", "/dinet/v1/hello", NULL, hello, sizeof(hello))) {
            online = 1;
            ms = GetTickCount() - t0;
            sscanf(hello, "%47[^\t]\t%d", sname, &players);
        }
        _snprintf(line, sizeof(line), "%s\t%s\t%s\t%d\t%d\t%lu\t%d\t%d\t%d\n", e[i].name, e[i].url, e[i].desc,
                  online, players, (unsigned long)ms, !strcmp(e[i].key, run), !strcmp(e[i].key, chosen),
                  url_usable(e[i].url));
        line[sizeof(line) - 1] = 0;
        if (len + (int)strlen(line) < (int)sizeof(out) - 1) { strcpy(out + len, line); len += (int)strlen(line); }
    }
    EnterCriticalSection(&g_dirLock);
    strcpy(g_dirText, out);
    LeaveCriticalSection(&g_dirLock);
    logf("servers: %d in the list", n);
}

static DWORD WINAPI dir_thread(LPVOID unused)
{
    (void)unused;
    fetch_directory();
    InterlockedExchange(&g_dirBusy, 0);
    return 0;
}

/* The exe copy has the server url written into its Disney service strings (patch_exe.py, [server] exe_url).
 * When the player chose another server (F8), rewrite those strings in memory before the game reads them:
 * CrabeLoader 1.1+ loads dinet_bk.asi before the game code runs. Same rule as patch_exe.py: the new url must
 * not be longer than the room the old string had (its zero padding included). */
static void rewrite_exe_urls(void)
{
    char want[128], have[128];
    BYTE* img = (BYTE*)g_base;
    BYTE *p, *end, *q;
    IMAGE_NT_HEADERS* nt;
    DWORD size, hits = 0, skipped = 0;
    size_t hl, wl;
    GetPrivateProfileStringA("server", "url", "", want, sizeof(want), g_iniPath);
    GetPrivateProfileStringA("server", "exe_url", "", have, sizeof(have), g_iniPath);
    url_norm(want, want, sizeof(want));
    url_norm(have, have, sizeof(have));
    strncpy(g_runningUrl, have[0] ? have : want, sizeof(g_runningUrl) - 1);
    if (!have[0]) {                                  /* older install: the exe was patched with [server] url */
        if (want[0]) WritePrivateProfileStringA("server", "exe_url", want, g_iniPath);
        return;
    }
    if (!want[0] || !strcmp(want, have)) return;
    if (!url_usable(want)) {
        logf("servers: [server] url %s does not fit the exe (http://, 22 characters at most); keeping %s", want, have);
        split_url(have, g_serverHost, sizeof(g_serverHost), &g_serverPort);
        return;
    }
    nt = (IMAGE_NT_HEADERS*)(img + ((IMAGE_DOS_HEADER*)img)->e_lfanew);
    size = nt->OptionalHeader.SizeOfImage;
    hl = strlen(have);
    wl = strlen(want);
    for (p = img; p < img + size; p = end) {           /* one committed, readable region at a time */
        MEMORY_BASIC_INFORMATION mi;
        if (!VirtualQuery(p, &mi, sizeof(mi))) break;
        end = (BYTE*)mi.BaseAddress + mi.RegionSize;
        if (end > img + size) end = img + size;
        if (mi.State != MEM_COMMIT || (mi.Protect & (PAGE_GUARD | PAGE_NOACCESS)) ||
            !(mi.Protect & (PAGE_READONLY | PAGE_READWRITE | PAGE_EXECUTE_READ | PAGE_EXECUTE_READWRITE |
                            PAGE_WRITECOPY | PAGE_EXECUTE_WRITECOPY)))
            continue;
        for (q = p; q + hl + 1 < end; q++) {
            size_t rest, room, k;
            DWORD old;
            if (*q != have[0] || _strnicmp((char*)q, have, hl) != 0) continue;
            if (q[hl] != '/' && q[hl] != 0 && q[hl] != '?') continue;
            for (rest = 0; q + hl + rest < end && q[hl + rest]; rest++) ;
            for (room = hl + rest + 1; q + room < end && q[room] == 0 && room < 512; room++) ;
            if (wl + rest + 1 > room) { skipped++; continue; }
            if (!VirtualProtect(q, room, PAGE_READWRITE, &old)) { skipped++; continue; }
            memmove(q + wl, q + hl, rest + 1);
            memcpy(q, want, wl);
            for (k = wl + rest + 1; k < hl + rest + 1; k++) q[k] = 0;
            VirtualProtect(q, room, old, &old);
            hits++;
            q += wl + rest;
        }
    }
    /* the DLL talks to the server the game uses: the new one only if the exe strings now point there */
    if (hits) strncpy(g_runningUrl, want, sizeof(g_runningUrl) - 1);
    split_url(hits ? want : have, g_serverHost, sizeof(g_serverHost), &g_serverPort);
    logf("servers: exe urls %s -> %s: %lu rewritten, %lu without room", have, want, hits, skipped);
}

static void cancel_join(const char* why, int keepHost)
{
    BYTE* flag = join_flag_ptr();
    if (!g_joinArmed && (keepHost || !g_fakeHostActive)) return;
    InterlockedExchange(&g_joinArmed, 0);
    InterlockedExchange(&g_joinStarted, 0);
    g_pendingWorld[0] = 0;
    if (!keepHost) {                    /* F9: back to normal hosting */
        set_join_data(0);
        if (flag) *flag = 0;
        InterlockedExchange(&g_fakeHostActive, 0);
    }
    logf("join %s (%s)", keepHost ? "attempt finished, host kept for the session" : "cancelled", why);
}

/* ------------------------------------------------------------------ stage 4: friends and invites
 * The Gold Edition keeps the friends / invites screens in Lua (friends_list_u, friend_invites_u) but
 * their natives answered from the removed XMPP client. dinet.lua (next to the exe, loaded by the screens
 * through loadstring) replaces them and talks to this DLL through Translator_TranslateString:
 * a key "#dinet:<command>" is answered here instead of being translated.
 *   lua                 the text of dinet.lua
 *   me                  this player's name
 *   players / invites   "name TAB hosting TAB ip TAB private_ip TAB port" lines (refreshed every 3 s)
 *   invite:<name>, uninvite:<name>, decline:<name>   sent to the server by the worker
 *   join:<name>         prepares the join of that player's Toy Box (like F8) -> "ok" or an error */
#define VA_TRANSLATE_NATIVE  0x00B4F990u   /* Lua native Translator_TranslateString(L): push -1; push <SEH> */
#define VA_LUA_TOLSTRING     0x0130D5A0u   /* const char* lua_tolstring(L, idx, size_t* len) */
#define VA_LUA_PUSHSTRING    0x0130D840u   /* void lua_pushstring(L, const char*) */
#define VA_MIGRATION_WARNING 0x01340F50u   /* shows the "Save_Migration_Warning" popup (thiscall, no args) */

typedef const char* (__cdecl* lua_tolstring_fn)(void* L, int idx, size_t* len);
typedef void (__cdecl* lua_pushstring_fn)(void* L, const char* s);

static CRITICAL_SECTION g_socialLock;
static char    g_players[8192], g_invites[4096], g_luaPath[MAX_PATH];
static char    g_queue[16][96];             /* "invite:name" ... sent by the worker */
static int     g_queueLen;
static char    g_notified[2048] = "\n";     /* "\nname\n" of the invites already shown */
static int     g_invitePopup = 1;
static char    g_requests[4096];            /* join requests to us, same format as g_invites */
static char    g_requested[1024] = "\n";    /* "\nname\n" of the players we asked to join */
static char    g_shownInv[2048] = "\n", g_shownReq[2048] = "\n";   /* notices already given to the HUD */
static volatile DWORD g_luaPollTick;        /* last "notice" from the HUD: it shows the notices in game */
static DWORD   g_transImm, g_transBack;
static volatile LONG g_joinPopupPending;

/* ------------------------------------------------------------------ stage 5: text chat */
#define CHAT_MAX_BYTES 480                  /* 120 characters of up to 4 UTF-8 bytes */
static CRITICAL_SECTION g_chatLock;
static char    g_chatOut[8][CHAT_MAX_BYTES + 80];   /* "tag TAB text" waiting to be sent */
static int     g_chatOutLen;
static char    g_chatIn[8000];              /* "id TAB name TAB tag TAB text" lines not yet taken by Lua */
static long    g_chatLast = -1;             /* newest message id seen; -1 = not asked yet */
static int     g_chatEnabled = 1, g_chatPollMs = 1000;
static char    g_chatCfg[128];              /* answer to "chatcfg", built from [chat] in di_net.ini */
static WCHAR   g_chatDead;                  /* dead key (accent) waiting for its letter */
static volatile LONG g_editorOpen;          /* Toy Box editor screens open now: no chat while > 0 */

/* queues "tag TAB text" for the chat thread */
static void chat_send(const char* msg)
{
    EnterCriticalSection(&g_chatLock);
    if (g_chatOutLen < 8) {
        strncpy(g_chatOut[g_chatOutLen], msg, sizeof(g_chatOut[0]) - 1);
        g_chatOut[g_chatOutLen][sizeof(g_chatOut[0]) - 1] = 0;
        g_chatOutLen++;
    }
    LeaveCriticalSection(&g_chatLock);
}

/* letter + accent typed with a dead key (Spanish, French... layouts), 0 when they do not combine */
static WCHAR chat_compose(WCHAR dead, WCHAR c)
{
    static const struct { WCHAR dead; const WCHAR* base; const WCHAR* out; } t[] = {
        { 0x00B4, L"aeiouyAEIOUY", L"\x00E1\x00E9\x00ED\x00F3\x00FA\x00FD\x00C1\x00C9\x00CD\x00D3\x00DA\x00DD" },
        { 0x0027, L"aeiouyAEIOUY", L"\x00E1\x00E9\x00ED\x00F3\x00FA\x00FD\x00C1\x00C9\x00CD\x00D3\x00DA\x00DD" },
        { 0x0060, L"aeiouAEIOU",   L"\x00E0\x00E8\x00EC\x00F2\x00F9\x00C0\x00C8\x00CC\x00D2\x00D9" },
        { 0x00A8, L"aeiouyAEIOU",  L"\x00E4\x00EB\x00EF\x00F6\x00FC\x00FF\x00C4\x00CB\x00CF\x00D6\x00DC" },
        { 0x0022, L"aeiouyAEIOU",  L"\x00E4\x00EB\x00EF\x00F6\x00FC\x00FF\x00C4\x00CB\x00CF\x00D6\x00DC" },
        { 0x005E, L"aeiouAEIOU",   L"\x00E2\x00EA\x00EE\x00F4\x00FB\x00C2\x00CA\x00CE\x00D4\x00DB" },
        { 0x007E, L"anoANO",       L"\x00E3\x00F1\x00F5\x00C3\x00D1\x00D5" },
    };
    int i;
    for (i = 0; i < (int)(sizeof(t) / sizeof(t[0])); i++) {
        const WCHAR* p;
        if (t[i].dead != dead) continue;
        for (p = t[i].base; *p; p++)
            if (*p == c) return t[i].out[p - t[i].base];
    }
    return 0;
}

/* The text a key press types with the current keyboard layout and Shift / Caps Lock / AltGr, as UTF-8
 * ("" for keys that type nothing, and for a dead key, which waits for the next letter). */
static void chat_key(int vk, char* out, int size)
{
    BYTE ks[256];
    WCHAR w[8], text[4];
    HWND win = game_window();
    HKL hkl = GetKeyboardLayout(win ? GetWindowThreadProcessId(win, NULL) : 0);
    int i, n, len = 0;
    out[0] = 0;
    if (vk <= 0 || vk > 254) return;
    for (i = 0; i < 256; i++) ks[i] = (GetAsyncKeyState(i) & 0x8000) ? 0x80 : 0;
    if (GetKeyState(VK_CAPITAL) & 1) ks[VK_CAPITAL] |= 1;
    ks[vk] |= 0x80;
    /* flag 4: leave the system's keyboard state (and its dead key) alone; dead keys are composed here */
    n = ToUnicodeEx((UINT)vk, MapVirtualKeyExA((UINT)vk, 0 /* MAPVK_VK_TO_VSC */, hkl), ks, w, 8, 4, hkl);
    if (n < 0) {                                /* a dead key: remember its accent */
        g_chatDead = w[0];
        return;
    }
    if (n == 0 || w[0] < 0x20) return;
    if (g_chatDead) {
        WCHAR c = chat_compose(g_chatDead, w[0]);
        if (c) text[len++] = c;
        else { text[len++] = g_chatDead; text[len++] = w[0]; }
        g_chatDead = 0;
    } else {
        text[len++] = w[0];
    }
    n = WideCharToMultiByte(CP_UTF8, 0, text, len, out, size - 1, NULL, NULL);
    out[n > 0 ? n : 0] = 0;
}

/* While the chat line is open the game must not see the keyboard. It reads the keyboard through
 * DirectInput, which window-message key filters do not reach, so the DirectInput device methods that
 * return key states (GetDeviceState, GetDeviceData) are hooked in the device vtables of dinput8.dll,
 * shared by every device the game created: while typing, a keyboard device reports no key down and no
 * key event. Mouse devices (the camera) are left alone. main.lua renews "typing:1" several times a
 * second; the flag expires by itself after a second, so a lost "typing:0" cannot leave the keyboard dead. */
static volatile DWORD g_typingTick;          /* last "typing:1" from main.lua; 0 = not typing */
#define DI_VT_GETDEVICESTATE 9
#define DI_VT_GETDEVICEDATA  10
typedef HRESULT (__stdcall* di_state_fn)(void* self, DWORD cb, LPVOID data);
typedef HRESULT (__stdcall* di_data_fn)(void* self, DWORD cbObj, LPDIDEVICEOBJECTDATA rgdod, LPDWORD inOut, DWORD flags);
typedef HRESULT (__stdcall* di_caps_fn)(void* self, LPDIDEVCAPS caps);
static di_state_fn g_diState[2];
static di_data_fn  g_diData[2];
static struct { void* dev; int keyboard; } g_diKinds[16];

static int chat_typing(void)
{
    DWORD t = g_typingTick;
    return t && GetTickCount() - t < 1000;
}

/* 1 when a DirectInput device is a keyboard (asked once per device, then remembered) */
static int di_is_keyboard(void* self)
{
    DIDEVCAPS caps;
    int i, free = -1;
    for (i = 0; i < 16; i++) {
        if (g_diKinds[i].dev == self) return g_diKinds[i].keyboard;
        if (!g_diKinds[i].dev && free < 0) free = i;
    }
    memset(&caps, 0, sizeof(caps));
    caps.dwSize = sizeof(caps);
    i = SUCCEEDED(((di_caps_fn)(*(void***)self)[3])(self, &caps)) && GET_DIDEVICE_TYPE(caps.dwDevType) == DI8DEVTYPE_KEYBOARD;
    if (free >= 0) { g_diKinds[free].keyboard = i; g_diKinds[free].dev = self; }
    return i;
}

#define DI_HOOKS(n)                                                                                         \
static HRESULT __stdcall di_state_##n(void* self, DWORD cb, LPVOID data)                                    \
{                                                                                                           \
    HRESULT r = g_diState[n](self, cb, data);                                                               \
    if (SUCCEEDED(r) && data && chat_typing() && di_is_keyboard(self)) memset(data, 0, cb);                  \
    return r;                                                                                               \
}                                                                                                           \
static HRESULT __stdcall di_data_##n(void* self, DWORD cbObj, LPDIDEVICEOBJECTDATA rgdod, LPDWORD inOut,    \
                                      DWORD flags)                                                          \
{                                                                                                           \
    HRESULT r = g_diData[n](self, cbObj, rgdod, inOut, flags);                                              \
    if (SUCCEEDED(r) && inOut && chat_typing() && di_is_keyboard(self)) *inOut = 0;   /* events dropped */  \
    return r;                                                                                               \
}
DI_HOOKS(0)
DI_HOOKS(1)

static int di_patch(void** vt, int n, void* state, void* data)
{
    DWORD old;
    if (vt[DI_VT_GETDEVICESTATE] == state) return 1;             /* already done (A and W share it) */
    if (!VirtualProtect(&vt[DI_VT_GETDEVICESTATE], 2 * sizeof(void*), PAGE_READWRITE, &old)) return 0;
    g_diState[n] = (di_state_fn)vt[DI_VT_GETDEVICESTATE];
    g_diData[n] = (di_data_fn)vt[DI_VT_GETDEVICEDATA];
    vt[DI_VT_GETDEVICESTATE] = state;
    vt[DI_VT_GETDEVICEDATA] = data;
    VirtualProtect(&vt[DI_VT_GETDEVICESTATE], 2 * sizeof(void*), old, &old);
    return 1;
}

/* hooks the keyboard device vtables of both DirectInput interfaces (ANSI and Unicode) */
static void hook_directinput(void)
{
    static const IID* iids[2] = { &IID_IDirectInput8A, &IID_IDirectInput8W };
    int n, done = 0;
    for (n = 0; n < 2; n++) {
        IDirectInput8A* di = NULL;
        IDirectInputDevice8A* dev = NULL;
        if (FAILED(DirectInput8Create(GetModuleHandleA(NULL), DIRECTINPUT_VERSION, iids[n], (void**)&di, NULL)) || !di)
            continue;
        if (SUCCEEDED(di->lpVtbl->CreateDevice(di, &GUID_SysKeyboard, &dev, NULL)) && dev) {
            void** vt = *(void***)dev;
            if (vt[DI_VT_GETDEVICESTATE] == (void*)di_state_0 || vt[DI_VT_GETDEVICESTATE] == (void*)di_state_1) done++;
            else if (di_patch(vt, n, n ? (void*)di_state_1 : (void*)di_state_0, n ? (void*)di_data_1 : (void*)di_data_0))
                done++;
            dev->lpVtbl->Release(dev);
        }
        di->lpVtbl->Release(di);
    }
    logf(done ? "chat: the game's keyboard is silenced while typing (DirectInput hooked)"
              : "chat: DirectInput not hooked; the character may move while typing");
}

/* sends the queued messages and fetches the new ones of the other players */
static DWORD WINAPI chat_worker(LPVOID unused)
{
    static char buf[16384];
    char msg[CHAT_MAX_BYTES + 80], enc[128], etag[256], etext[CHAT_MAX_BYTES * 3 + 8], body[2048], path[256];
    char *tab, *line, *next;
    (void)unused;
    hook_directinput();                         /* here, not in DllMain: it may load DLLs */
    Sleep(3000);                                /* the Steam name arrives with the game */
    for (;;) {
        for (;;) {
            EnterCriticalSection(&g_chatLock);
            if (!g_chatOutLen) { LeaveCriticalSection(&g_chatLock); break; }
            strcpy(msg, g_chatOut[0]);
            memmove(g_chatOut[0], g_chatOut[1], sizeof(g_chatOut[0]) * (--g_chatOutLen));
            LeaveCriticalSection(&g_chatLock);
            tab = strchr(msg, '\t');
            if (!tab) continue;
            *tab = 0;
            if (strlen(tab + 1) > CHAT_MAX_BYTES) tab[1 + CHAT_MAX_BYTES] = 0;
            url_encode(g_name, enc, sizeof(enc));
            url_encode(msg, etag, sizeof(etag));
            url_encode(tab + 1, etext, sizeof(etext));
            _snprintf(body, sizeof(body), "name=%s&tag=%s&text=%s", enc, etag, etext);
            body[sizeof(body) - 1] = 0;
            if (http(L"POST", "/dinet/v1/chat", body, NULL, 0)) logf("chat: %s said: %s", g_name, tab + 1);
            else logf("chat: the server did not take the message: %s", tab + 1);
        }
        url_encode(g_name, enc, sizeof(enc));
        _snprintf(path, sizeof(path), "/dinet/v1/chat?name=%s&since=%ld", enc, g_chatLast);
        path[sizeof(path) - 1] = 0;
        if (http(L"GET", path, NULL, buf, sizeof(buf))) {
            long newest = g_chatLast;
            for (line = buf; line && *line; line = next) {
                long id;
                next = strchr(line, '\n');
                if (next) *next++ = 0;
                if (line[0] == '#') {                   /* "#<last id on the server>" */
                    long last = atol(line + 1);
                    if (g_chatLast < 0 || last > newest) newest = last;
                    continue;
                }
                id = atol(line);
                if (g_chatLast < 0 || id <= g_chatLast) continue;
                EnterCriticalSection(&g_chatLock);
                if (strlen(g_chatIn) + strlen(line) + 2 < sizeof(g_chatIn)) {
                    strcat(g_chatIn, line);
                    strcat(g_chatIn, "\n");
                }
                LeaveCriticalSection(&g_chatLock);
                logf("chat: %s", line);
                if (id > newest) newest = id;
            }
            g_chatLast = newest < 0 ? 0 : newest;
        }
        Sleep(g_chatPollMs);
    }
}

static void enqueue(const char* cmd)
{
    EnterCriticalSection(&g_socialLock);
    if (g_queueLen < 16) {
        strncpy(g_queue[g_queueLen], cmd, sizeof(g_queue[0]) - 1);
        g_queue[g_queueLen][sizeof(g_queue[0]) - 1] = 0;
        g_queueLen++;
    }
    LeaveCriticalSection(&g_socialLock);
}

/* Finds "name" in a players / invites list; fills ip, private ip and port. 1 if it is hosting. */
static int find_host(const char* list, const char* name, char* ip, char* priv, int* port)
{
    char copy[8192], *line, *next;
    strncpy(copy, list, sizeof(copy) - 1);
    copy[sizeof(copy) - 1] = 0;
    for (line = copy; line && *line; line = next) {
        char n[64] = "", a[32] = "", b[32] = "";
        int hosting = 0, p = 0;
        next = strchr(line, '\n');
        if (next) *next++ = 0;
        if (sscanf(line, "%63[^\t]\t%d\t%31[^\t]\t%31[^\t]\t%d", n, &hosting, a, b, &p) < 2) continue;
        if (_stricmp(n, name) != 0) continue;
        strcpy(ip, a);
        strcpy(priv, b);
        *port = p;
        return hosting && p > 0;
    }
    return 0;
}

/* g_pendingWorld = the level <host> plays (6th field of its lobby line), or "" if unknown */
static void pending_world_for(const char* host)
{
    char copy[12288], *line, *next;
    g_pendingWorld[0] = 0;
    EnterCriticalSection(&g_socialLock);
    _snprintf(copy, sizeof(copy), "%s%s", g_invites, g_players);
    copy[sizeof(copy) - 1] = 0;
    LeaveCriticalSection(&g_socialLock);
    for (line = copy; line && *line; line = next) {
        char n[64] = "", world[64] = "";
        int hosting = 0, port = 0;
        next = strchr(line, '\n');
        if (next) *next++ = 0;
        if (sscanf(line, "%63[^\t]\t%d\t%*[^\t]\t%*[^\t]\t%d\t%63[^\t\r\n]", n, &hosting, &port, world) < 4) continue;
        if (_stricmp(n, host) != 0 || !world[0]) continue;
        strcpy(g_pendingWorld, world);
        logf("join %s: the host plays %s", host, world);
        return;
    }
}

/* Why joining <host> would put us in another level, or NULL. The host's level must exist here, and both games
 * must give the unmodified levels the same ids (the 9th field of its lobby line, "" from older versions). */
static const char* zones_mismatch(const char* host)
{
    static char msg[320];
    char copy[12288], *line, *next, theirs[24] = "";
    const char* mine = zones_fingerprint();
    pending_world_for(host);
    EnterCriticalSection(&g_socialLock);
    _snprintf(copy, sizeof(copy), "%s%s", g_invites, g_players);
    copy[sizeof(copy) - 1] = 0;
    LeaveCriticalSection(&g_socialLock);
    for (line = copy; line && *line && !theirs[0]; line = next) {
        char* f[9];
        int i;
        next = strchr(line, '\n');
        if (next) *next++ = 0;
        for (i = 0, f[0] = line; i < 8 && f[i]; i++) {    /* name hosting ip private_ip port world avatar level zones */
            char* tab = strchr(f[i], '\t');
            if (tab) *tab++ = 0;
            f[i + 1] = tab;
        }
        if (i == 8 && f[8] && !_stricmp(f[0], host)) {
            strncpy(theirs, f[8], sizeof(theirs) - 1);
            theirs[strcspn(theirs, "\r\n")] = 0;
        }
    }
    if (g_pendingWorld[0] && mine[0] && !zone_id_by_name(g_pendingWorld)) {
        logf("join %s: refused, this game has no level %s", host, g_pendingWorld);
        _snprintf(msg, sizeof(msg), "%s esta en %s, un mapa que tu juego no tiene (lo agrega un mod). "
                  "Instala el mismo mod o pidele que vaya a otro mapa.", host, g_pendingWorld);
        return msg;
    }
    if (theirs[0] && mine[0] && strncmp(theirs, mine, 8) != 0) {
        logf("join %s: refused, level ids differ (host %s, here %s)", host, theirs, mine);
        _snprintf(msg, sizeof(msg), "Los mapas de tu juego y los de %s tienen otra numeracion (version distinta del "
                  "mod de red o un mod de mapas sin actualizar). Al unirte cargarias otro mapa. Actualicen los dos "
                  "el paquete del mod.", host);
        return msg;
    }
    if (theirs[0] && mine[0] && strcmp(theirs, mine) != 0)
        logf("join %s: same level ids, different mod levels (host %s, here %s)", host, theirs, mine);
    return NULL;
}

static char* read_text_file(const char* path)
{
    FILE* f = fopen(path, "rb");
    char* buf;
    long n;
    if (!f) return NULL;
    fseek(f, 0, SEEK_END);
    n = ftell(f);
    fseek(f, 0, SEEK_SET);
    buf = (char*)malloc(n + 1);
    if (buf) {
        n = (long)fread(buf, 1, n, f);
        buf[n] = 0;
    }
    fclose(f);
    return buf;
}

/* "\nname\n" set helpers (case-insensitive names, caller holds g_socialLock) */
static int set_has(const char* set, const char* name)
{
    char key[80];
    _snprintf(key, sizeof(key), "\n%s\n", name);
    key[sizeof(key) - 1] = 0;
    return strstr(set, key) != NULL;
}

static void set_add(char* set, size_t size, const char* name)
{
    if (set_has(set, name)) return;
    if (strlen(set) + strlen(name) + 2 >= size) strcpy(set, "\n");
    strcat(set, name);
    strcat(set, "\n");
}

static void set_del(char* set, const char* name)
{
    char key[80], *p;
    _snprintf(key, sizeof(key), "\n%s\n", name);
    key[sizeof(key) - 1] = 0;
    if ((p = strstr(set, key)) != NULL) memmove(p + 1, p + strlen(key), strlen(p + strlen(key)) + 1);
}

/* First line of a list whose name is not in <shown>; adds it there. <shown> keeps only names still listed,
 * so an invite / request sent again later is shown again. Returns 1 and the name. */
static int next_notice(const char* list, char* shown, size_t shownSize, char* name, int nameSize)
{
    char copy[4096], keep[2048] = "\n", *line, *next;
    int found = 0;
    strncpy(copy, list, sizeof(copy) - 1);
    copy[sizeof(copy) - 1] = 0;
    for (line = copy; line && *line; line = next) {
        size_t len;
        char n[64];
        next = strchr(line, '\n');
        if (next) *next++ = 0;
        len = strcspn(line, "\t");
        if (!len || len >= sizeof(n)) continue;
        memcpy(n, line, len);
        n[len] = 0;
        if (!found && !set_has(shown, n)) {
            strncpy(name, n, nameSize - 1);
            name[nameSize - 1] = 0;
            found = 1;
        }
        if (found && !_stricmp(n, name)) set_add(keep, sizeof(keep), n);
        else if (set_has(shown, n)) set_add(keep, sizeof(keep), n);
    }
    strncpy(shown, keep, shownSize - 1);
    shown[shownSize - 1] = 0;
    return found;
}

static const char* bridge(const char* cmd)
{
    static char reply[8192];
    static char* lua;
    reply[0] = 0;
    if (!strcmp(cmd, "lua")) {
        free(lua);
        lua = read_text_file(g_luaPath);
        if (!lua) logf("friends: %s not found", g_luaPath);
        else logf("friends: dinet.lua requested (%u bytes)", (unsigned)strlen(lua));
        return lua ? lua : "";
    }
    if (!strncmp(cmd, "log:", 4)) {
        logf("lua: %s", cmd + 4);
        return "ok";
    }
    /* stage 5: text chat (main.lua). Asked many times a second: never logged here */
    if (!strncmp(cmd, "editor:", 7)) {              /* Toy Box editor screens: "+name" open, "-name" closed */
        if (cmd[7] == '+') InterlockedIncrement(&g_editorOpen);
        else if (cmd[7] == '-') { if (InterlockedDecrement(&g_editorOpen) < 0) InterlockedExchange(&g_editorOpen, 0); }
        else InterlockedExchange(&g_editorOpen, 0);  /* "reset": a new level was loaded */
        logf("chat: toy box editor %s (%ld screen(s) open)", cmd + 7, g_editorOpen);
        return "ok";
    }
    if (!strcmp(cmd, "editor")) return g_editorOpen > 0 ? "1" : "0";
    if (!strncmp(cmd, "ini:", 4)) {                 /* "ini:<section> TAB <key>": a value of di_net.ini, or "" */
        char section[64] = "";
        const char* key = strchr(cmd + 4, '\t');
        if (!key || key - (cmd + 4) >= (int)sizeof(section)) return "";
        memcpy(section, cmd + 4, key - (cmd + 4));
        section[key - (cmd + 4)] = 0;
        GetPrivateProfileStringA(section, key + 1, "", reply, sizeof(reply), g_iniPath);
        return reply;
    }
    if (!strcmp(cmd, "chatcfg")) return g_chatCfg;
    /* server directory (F8): the list is fetched by the worker, see fetch_directory */
    if (!strcmp(cmd, "servers")) {
        EnterCriticalSection(&g_dirLock);
        strcpy(reply, g_dirText);
        LeaveCriticalSection(&g_dirLock);
        if (g_dirWanted || g_dirBusy) {            /* a fetch is pending: say so, keep the last list */
            char* nl = strchr(reply, '\n');
            if (nl) { memmove(reply + 8, nl + 1, strlen(nl + 1) + 1); memcpy(reply, "loading\n", 8); }
            else strcpy(reply, "loading\n");
        }
        return reply;
    }
    if (!strcmp(cmd, "servers:refresh")) {
        InterlockedExchange(&g_dirWanted, 1);
        return "ok";
    }
    if (!strncmp(cmd, "useserver:", 10)) {
        char url[128];
        url_norm(cmd + 10, url, sizeof(url));
        if (strncmp(url, "http://", 7) || strpbrk(url, " \t\r\n\"")) return "bad";
        if (strlen(url) > 22) return "long";
        WritePrivateProfileStringA("server", "url", url, g_iniPath);
        logf("servers: next start uses %s", url);
        InterlockedExchange(&g_dirWanted, 1);       /* the list shows the new choice */
        return "ok";
    }
    if (!strncmp(cmd, "typing:", 7)) {              /* main.lua: the chat line is open (renewed) / closed */
        DWORD now = GetTickCount();
        g_typingTick = cmd[7] == '1' ? (now ? now : 1) : 0;
        return "ok";
    }
    if (!strncmp(cmd, "chatkey:", 8)) {
        chat_key(atoi(cmd + 8), reply, sizeof(reply));
        return reply;
    }
    if (!strncmp(cmd, "chatsend:", 9)) {
        if (!g_chatEnabled) return "off";
        chat_send(cmd + 9);
        return "ok";
    }
    if (!strcmp(cmd, "chat")) {                     /* new lines of the other players, then forgotten */
        EnterCriticalSection(&g_chatLock);
        strncpy(reply, g_chatIn, sizeof(reply) - 1);
        reply[sizeof(reply) - 1] = 0;
        g_chatIn[0] = 0;
        LeaveCriticalSection(&g_chatLock);
        return reply;
    }
    /* notice: the HUD asks every second for something to show in game:
     * "request TAB name" (wants to join us), "accepted TAB name" (accepted our request: an invite),
     * "invite TAB name", or "" */
    if (!strncmp(cmd, "world:", 6)) {               /* the HUD tells us the level we play */
        if (strcmp(g_myWorld, cmd + 6)) {
            strncpy(g_myWorld, cmd + 6, sizeof(g_myWorld) - 1);
            g_myWorld[sizeof(g_myWorld) - 1] = 0;
            logf("this PC plays %s", g_myWorld);
            InterlockedExchange(&g_announceNow, 1);
        }
        return "ok";
    }
    if (!strncmp(cmd, "avatar:", 7)) {              /* the HUD tells us our character TAB its level */
        char avatar[64] = "", level[16] = "";
        sscanf(cmd + 7, "%63[^\t]\t%15[0-9]", avatar, level);
        EnterCriticalSection(&g_socialLock);
        if (strcmp(g_myAvatar, avatar) || strcmp(g_myAvatarLevel, level)) {
            strcpy(g_myAvatar, avatar);
            strcpy(g_myAvatarLevel, level);
            logf("this PC plays as %s (level %s)", avatar, level[0] ? level : "?");
            InterlockedExchange(&g_presenceNow, 1);
        }
        LeaveCriticalSection(&g_socialLock);
        return "ok";
    }
    if (!strcmp(cmd, "hudalive")) {                 /* the HUD exists but cannot show anything now */
        g_luaPollTick = GetTickCount();
        return "";
    }
    if (!strcmp(cmd, "notice")) {
        char name[64];
        g_luaPollTick = GetTickCount();
        EnterCriticalSection(&g_socialLock);
        if (next_notice(g_requests, g_shownReq, sizeof(g_shownReq), name, sizeof(name))) {
            _snprintf(reply, sizeof(reply), "request\t%s", name);
        } else if (next_notice(g_invites, g_shownInv, sizeof(g_shownInv), name, sizeof(name))) {
            int asked = set_has(g_requested, name);
            if (asked) set_del(g_requested, name);
            _snprintf(reply, sizeof(reply), "%s\t%s", asked ? "accepted" : "invite", name);
        }
        LeaveCriticalSection(&g_socialLock);
        if (reply[0]) logf("friends: notice for the HUD: %s", reply);
        return reply;
    }
    if (strcmp(cmd, "players") && strcmp(cmd, "invites") && strcmp(cmd, "pendingworld"))
        logf("friends: command '%s'", cmd);
    /* request:<name>  ask a player who hosts a Toy Box to let us in */
    if (!strncmp(cmd, "request:", 8)) {
        char ip[32], priv[32];
        int port = 0, hosting;
        EnterCriticalSection(&g_socialLock);
        hosting = find_host(g_players, cmd + 8, ip, priv, &port);
        if (hosting) set_add(g_requested, sizeof(g_requested), cmd + 8);
        LeaveCriticalSection(&g_socialLock);
        if (!hosting) return "error:not_hosting";
        enqueue(cmd);
        return "ok";
    }
    /* accept:<name> / deny:<name>  answer a join request (accepting = inviting them) */
    if (!strncmp(cmd, "accept:", 7)) {
        if (!g_hosting) return "error:not_hosting";
        _snprintf(reply, sizeof(reply), "invite:%s", cmd + 7);
        enqueue(reply);
        _snprintf(reply, sizeof(reply), "deny:%s", cmd + 7);
        enqueue(reply);                             /* the request is answered */
        return "ok";
    }
    if (!strncmp(cmd, "deny:", 5)) {
        enqueue(cmd);
        return "ok";
    }
    if (!strcmp(cmd, "me")) return g_name;
    if (!strcmp(cmd, "playsetmp")) return g_forceMpLevels >= 2 ? "1" : "0";   /* network play forced in playsets */
    if (!strcmp(cmd, "hosting")) return g_hosting ? "1" : "0";     /* inside a Toy Box we host */
    EnterCriticalSection(&g_socialLock);
    if (!strcmp(cmd, "players")) strcpy(reply, g_players);
    else if (!strcmp(cmd, "invites")) strcpy(reply, g_invites);
    LeaveCriticalSection(&g_socialLock);
    if (reply[0] || !strcmp(cmd, "players") || !strcmp(cmd, "invites")) return reply;
    if (!strncmp(cmd, "invite:", 7) || !strncmp(cmd, "uninvite:", 9) || !strncmp(cmd, "decline:", 8)) {
        enqueue(cmd);
        return "ok";
    }
    /* pendingworld: the main menu asks (once) which level to load to start a prepared join */
    if (!strcmp(cmd, "pendingworld")) {
        if (g_joinArmed && !g_joinStarted && g_pendingWorld[0]) {
            strcpy(reply, g_pendingWorld);
            g_pendingWorld[0] = 0;
            g_joinArmedTick = GetTickCount();       /* the level load starts now */
        }
        return reply;
    }
    /* join:<name>                prepare the join, then a "join prepared - enter a Toy Box" message box
     * joinnow:<name>             prepare it silently
     * joinvia:<name> TAB <world> prepare it silently; the game goes to the main menu, which loads <world>
     *                            (a level change from the main menu opens a new network session) */
    if (!strncmp(cmd, "join:", 5) || !strncmp(cmd, "joinnow:", 8) || !strncmp(cmd, "joinvia:", 8)) {
        char whoBuf[64], *tab;
        const char* why;
        const char* who = whoBuf;
        char ip[32] = "", priv[32] = "", line[16384];
        int port = 0, hosting;
        strncpy(whoBuf, strchr(cmd, ':') + 1, sizeof(whoBuf) - 1);
        whoBuf[sizeof(whoBuf) - 1] = 0;
        if ((tab = strchr(whoBuf, '\t')) != NULL) *tab = 0;
        EnterCriticalSection(&g_socialLock);
        _snprintf(line, sizeof(line), "%s%s", g_invites, g_players);
        line[sizeof(line) - 1] = 0;
        LeaveCriticalSection(&g_socialLock);
        hosting = find_host(line, who, ip, priv, &port);
        if (!hosting) return "error:not_hosting";
        if ((why = zones_mismatch(who)) != NULL) {
            _snprintf(reply, sizeof(reply), "error:msg:%s", why);
            return reply;
        }
        if (!prepare_join(who, ip, priv, port)) return "error:no_session";
        _snprintf(reply, sizeof(reply), "decline:%s", who);
        enqueue(reply);                             /* the invite is used */
        if (cmd[4] == ':') InterlockedExchange(&g_joinPopupPending, 1);
        if (!strncmp(cmd, "joinvia:", 8) && (tab = strchr(cmd, '\t')) != NULL) {
            if (!g_pendingWorld[0]) {               /* host's level unknown (old version): ours */
                strncpy(g_pendingWorld, tab + 1, sizeof(g_pendingWorld) - 1);
                g_pendingWorld[sizeof(g_pendingWorld) - 1] = 0;
            }
            logf("join %s: going through the main menu, which loads %s", who, g_pendingWorld);
        }
        return "ok";
    }
    logf("friends: unknown command '%s'", cmd);
    return "error:unknown";
}

static int __cdecl on_translate(void* L)
{
    const char* key = ((lua_tolstring_fn)rt(VA_LUA_TOLSTRING))(L, 1, NULL);
    if (!key || strncmp(key, "#dinet:", 7) != 0) return 0;
    ((lua_pushstring_fn)rt(VA_LUA_PUSHSTRING))(L, bridge(key + 7));
    return 1;
}

static __declspec(naked) void hook_translate(void)
{
    __asm {
        mov  eax, dword ptr [esp + 4]   /* lua_State* */
        push eax
        call on_translate
        add  esp, 4
        test eax, eax
        jz   original
        mov  eax, 1                     /* one result */
        ret  4
    original:
        push -1                         /* stolen: push -1; push <SEH handler> */
        push g_transImm
        jmp  dword ptr [g_transBack]
    }
}

static DWORD WINAPI invite_popup(LPVOID arg)
{
    char* line = (char*)arg;
    char name[64] = "", ip[32] = "", priv[32] = "", msg[256], cmd[96];
    const char* why;
    int hosting = 0, port = 0;
    sscanf(line, "%63[^\t]\t%d\t%31[^\t]\t%31[^\t]\t%d", name, &hosting, ip, priv, &port);
    free(line);
    _snprintf(msg, sizeof(msg), "%s te invita a su Toy Box.\n\nUnirte ahora?\n\n(No: la invitacion queda en "
              "Multijugador > Invites)", name);
    msg[sizeof(msg) - 1] = 0;
    if (MessageBoxA(game_window(), msg, "Disney Infinity - invitacion", MB_YESNO | MB_ICONQUESTION | MB_TOPMOST) != IDYES)
        return 0;                                   /* kept: it stays in the Invites screen */
    if (!hosting || !port) {
        MessageBoxA(game_window(), "Esa partida ya no esta abierta.", "Disney Infinity - invitacion", MB_OK | MB_TOPMOST);
    } else if ((why = zones_mismatch(name)) != NULL) {
        MessageBoxA(game_window(), why, "Disney Infinity - invitacion", MB_OK | MB_ICONWARNING | MB_TOPMOST);
    } else if (!prepare_join(name, ip, priv, port)) {
        MessageBoxA(game_window(), "El juego todavia no tiene sesion de red. Prueba otra vez desde el menu.",
                    "Disney Infinity - invitacion", MB_OK | MB_ICONWARNING | MB_TOPMOST);
        return 0;
    } else {
        MessageBoxA(game_window(), kJoinReady, "Disney Infinity - invitacion", MB_OK | MB_ICONINFORMATION | MB_TOPMOST);
    }
    _snprintf(cmd, sizeof(cmd), "decline:%s", name);   /* used (or gone): remove it */
    enqueue(cmd);
    return 0;
}

static void social_send_queue(void)
{
    char cmd[96], body[256], who[128], me[128], path[320];
    for (;;) {
        EnterCriticalSection(&g_socialLock);
        if (!g_queueLen) { LeaveCriticalSection(&g_socialLock); return; }
        strcpy(cmd, g_queue[0]);
        memmove(g_queue[0], g_queue[1], sizeof(g_queue[0]) * (--g_queueLen));
        LeaveCriticalSection(&g_socialLock);
        url_encode(g_name, me, sizeof(me));
        if (!strncmp(cmd, "invite:", 7) || !strncmp(cmd, "request:", 8)) {
            int inv = cmd[0] == 'i';
            url_encode(strchr(cmd, ':') + 1, who, sizeof(who));
            _snprintf(body, sizeof(body), "from=%s&to=%s", me, who);
            body[sizeof(body) - 1] = 0;
            http(L"POST", inv ? "/dinet/v1/invite" : "/dinet/v1/request", body, NULL, 0);
            logf("friends: %s %s", inv ? "invited" : "asked to join", strchr(cmd, ':') + 1);
        } else if (!strncmp(cmd, "deny:", 5)) {     /* answer a join request: remove it */
            url_encode(cmd + 5, who, sizeof(who));
            _snprintf(path, sizeof(path), "/dinet/v1/request?from=%s&to=%s", who, me);
            path[sizeof(path) - 1] = 0;
            http(L"DELETE", path, NULL, NULL, 0);
        } else {                                     /* uninvite:<to> or decline:<from> */
            int un = !strncmp(cmd, "uninvite:", 9);
            url_encode(strchr(cmd, ':') + 1, who, sizeof(who));
            _snprintf(path, sizeof(path), "/dinet/v1/invite?from=%s&to=%s", un ? me : who, un ? who : me);
            path[sizeof(path) - 1] = 0;
            http(L"DELETE", path, NULL, NULL, 0);
        }
    }
}

static void social_poll(int sendPresence)
{
    char buf[8192], body[448], enc[128], path[192], avatar[192], level[16], *line, *next;
    url_encode(g_name, enc, sizeof(enc));
    if (sendPresence) {
        EnterCriticalSection(&g_socialLock);
        url_encode(g_myAvatar, avatar, sizeof(avatar));
        strcpy(level, g_myAvatarLevel);
        LeaveCriticalSection(&g_socialLock);
        _snprintf(body, sizeof(body), "name=%s&private_ip=%s&avatar=%s&avatar_level=%s&zones=%s", enc, g_privateIp,
                  avatar, level, zones_fingerprint());
        body[sizeof(body) - 1] = 0;
        http(L"POST", "/dinet/v1/presence", body, NULL, 0);
    }
    if (http(L"GET", "/dinet/v1/players", NULL, buf, sizeof(buf))) {
        /* drop ourselves */
        char out[8192];
        int n = 0;
        out[0] = 0;
        for (line = buf; line && *line; line = next) {
            size_t len;
            next = strchr(line, '\n');
            if (next) *next++ = 0;
            len = strcspn(line, "\t");
            if (len == strlen(g_name) && !_strnicmp(line, g_name, len)) continue;
            n += _snprintf(out + n, sizeof(out) - n, "%s\n", line);
            if (n >= (int)sizeof(out)) { out[sizeof(out) - 1] = 0; break; }
        }
        EnterCriticalSection(&g_socialLock);
        strcpy(g_players, out);
        LeaveCriticalSection(&g_socialLock);
    }
    _snprintf(path, sizeof(path), "/dinet/v1/invites?name=%s", enc);
    path[sizeof(path) - 1] = 0;
    if (http(L"GET", path, NULL, buf, sizeof(buf))) {
        EnterCriticalSection(&g_socialLock);
        strncpy(g_invites, buf, sizeof(g_invites) - 1);
        g_invites[sizeof(g_invites) - 1] = 0;
        LeaveCriticalSection(&g_socialLock);
        {
            /* g_notified = "\nname\nname\n" of the pending invites already shown: new ones pop up */
            char seen[sizeof(g_notified)] = "\n", key[80];
            for (line = buf; line && *line; line = next) {
                size_t len;
                next = strchr(line, '\n');
                if (next) *next++ = 0;
                len = strcspn(line, "\t");
                if (!len || len > 64) continue;
                _snprintf(key, sizeof(key), "\n%.*s\n", (int)len, line);
                if (strlen(seen) + strlen(key) < sizeof(seen)) strcat(seen, key + 1);
                if (strstr(g_notified, key)) continue;
                logf("friends: invite from %.*s", (int)len, line);
                /* in a level the HUD shows it in game (it asks for notices every second) */
                if (g_invitePopup && GetTickCount() - g_luaPollTick > 5000)
                    CloseHandle(CreateThread(NULL, 0, invite_popup, _strdup(line), 0, NULL));
            }
            strcpy(g_notified, seen);
        }
    }
}

static void social_poll_requests(void)
{
    char buf[4096], enc[128], path[192];
    url_encode(g_name, enc, sizeof(enc));
    _snprintf(path, sizeof(path), "/dinet/v1/requests?name=%s", enc);
    path[sizeof(path) - 1] = 0;
    if (!http(L"GET", path, NULL, buf, sizeof(buf))) return;
    EnterCriticalSection(&g_socialLock);
    if (strcmp(buf, g_requests) && buf[0]) logf("friends: join requests: %.*s", (int)strcspn(buf, "\t"), buf);
    strncpy(g_requests, buf, sizeof(g_requests) - 1);
    g_requests[sizeof(g_requests) - 1] = 0;
    LeaveCriticalSection(&g_socialLock);
}

static DWORD WINAPI join_ready_popup(LPVOID unused)
{
    (void)unused;
    MessageBoxA(game_window(), kJoinReady, "Disney Infinity - unirse", MB_OK | MB_ICONINFORMATION | MB_TOPMOST);
    return 0;
}

static void hook_social(void)
{
    BYTE* head = (BYTE*)rt(VA_TRANSLATE_NATIVE);
    if (head[0] == 0x6A && head[1] == 0xFF && head[2] == 0x68) {
        g_transImm = *(DWORD*)(head + 3);
        g_transBack = (DWORD)head + 7;
        if (install_jmp((DWORD)head, NULL, 7, hook_translate))
            logf("friends: Lua bridge on Translator_TranslateString at %08X, script %s", VA_TRANSLATE_NATIVE, g_luaPath);
    } else {
        logf("friends: Translator_TranslateString not hooked: unexpected bytes at %08X", VA_TRANSLATE_NATIVE);
    }
}

/* The "you already migrated" warning before the Disney ID screen: [signin] skip_migration_warning=1 */
static void skip_migration_warning(void)
{
    BYTE* p = (BYTE*)rt(VA_MIGRATION_WARNING);
    DWORD old;
    if (p[0] != 0xA1 || p[5] != 0x57 || p[6] != 0x8B || p[7] != 0xF9) {
        logf("migration warning: unexpected bytes at %08X, not patched", VA_MIGRATION_WARNING);
        return;
    }
    if (!VirtualProtect(p, 1, PAGE_EXECUTE_READWRITE, &old)) return;
    p[0] = 0xC3;                                    /* ret */
    VirtualProtect(p, 1, old, &old);
    FlushInstructionCache(GetCurrentProcess(), p, 1);
    logf("migration warning popup disabled (skip_migration_warning=1)");
}

/* The Steam persona name (ISteamFriends::GetPersonaName, vtable slot 0, thiscall) from the game's
   steam_api.dll; used as the player name unless [net] name is set. 1 when found. */
static int steam_persona_name(void)
{
    typedef void* (__cdecl *friends_fn)(void);
    typedef const char* (__fastcall *persona_fn)(void* self, void* unused);
    HMODULE api = GetModuleHandleA("steam_api.dll");
    friends_fn friends = api ? (friends_fn)GetProcAddress(api, "SteamFriends") : NULL;
    void* f = friends ? friends() : NULL;
    const char* n;
    char clean[sizeof(g_name)];
    size_t i, o = 0;
    if (!f || !*(void***)f) return 0;
    n = ((persona_fn)(*(void***)f)[0])(f, NULL);
    if (!n || !*n) return 0;
    for (i = 0; n[i] && o < sizeof(clean) - 1; i++)
        if ((unsigned char)n[i] >= 0x20) clean[o++] = n[i];   /* no tabs / new lines (lobby lists) */
    clean[o] = 0;
    while (o && clean[o - 1] == ' ') clean[--o] = 0;
    if (!o) return 0;
    strcpy(g_name, clean);
    return 1;
}

static DWORD WINAPI social_worker(LPVOID unused)
{
    DWORD lastPresence = 0;
    int tries;
    (void)unused;
    Sleep(2000);                                    /* the main worker detects the private ip first */
    for (tries = 0; !g_nameFromIni && tries < 30; tries++) {   /* Steam starts with the game */
        if (steam_persona_name()) { logf("player name (Steam): %s", g_name); break; }
        Sleep(1000);
    }
    for (;;) {
        int presence = InterlockedExchange(&g_presenceNow, 0) | (GetTickCount() - lastPresence > 10000);
        social_send_queue();
        social_poll(presence);
        social_poll_requests();
        if (presence) lastPresence = GetTickCount();
        if (InterlockedExchange(&g_joinPopupPending, 0))
            CloseHandle(CreateThread(NULL, 0, join_ready_popup, NULL, 0, NULL));
        Sleep(3000);
    }
}

static DWORD WINAPI worker(LPVOID unused)
{
    DWORD lastAnnounce = 0;
    int f9 = 0;
    (void)unused;
    if (g_protectSaves) snapshot_saves();
    detect_private_ip();
    logf("lobby: server %s:%d, this PC %s, name '%s', Net-Z UDP port %d", g_serverHost, g_serverPort,
         g_privateIp, g_name, g_netPort);
    for (;;) {
        HWND w = game_window();
        int down9 = w && (GetAsyncKeyState(VK_F9) & 0x8000);
        /* F8 is main.lua's server list now (the old join picker was a test tool; joins run in the game's menus) */
        if (down9 && !f9) {
            if (g_joinArmed || g_fakeHostActive) {
                cancel_join("F9", 0);
                MessageBoxA(w, "Join cancelled.", "Disney Infinity - join", MB_OK | MB_TOPMOST);
            }
        }
        f9 = down9;
        if (g_dirWanted && !InterlockedExchange(&g_dirBusy, 1)) {   /* pings can take seconds: own thread */
            HANDLE th;
            InterlockedExchange(&g_dirWanted, 0);
            th = CreateThread(NULL, 0, dir_thread, NULL, 0, NULL);
            if (th) CloseHandle(th);
            else InterlockedExchange(&g_dirBusy, 0);
        }
        if (!g_netErrorOrig) hook_net_error_vtable();
        /* the engine read the host while opening the session: stop answering for it, so the next
           level load hosts normally again unless the player prepares another join */
        if (g_joinStarted && (LONG)(GetTickCount() - g_joinStartTick) > 30000)
            cancel_join("30 s after the session opened", 1);
        if (g_joinArmed && !g_joinStarted && GetTickCount() - g_joinArmedTick > JOIN_ARMED_TIMEOUT)
            cancel_join("it did not start within 2 minutes: we host normally again", 0);
        if (InterlockedExchange(&g_unannounce, 0)) unannounce();
        if (g_restorePending && GetTickCount() - g_restoreTick > 3000) {
            InterlockedExchange(&g_restoreDone, 1);
            InterlockedExchange(&g_restorePending, 0);
            restore_saves();
        }
        if (g_hosting && (GetTickCount() - lastAnnounce > 30000 || InterlockedExchange(&g_announceNow, 0))) {
            announce();
            lastAnnounce = GetTickCount();
        }
        Sleep(50);
    }
}

/* ------------------------------------------------------------------ start */
static void find_text(void)
{
    IMAGE_DOS_HEADER* dos = (IMAGE_DOS_HEADER*)g_base;
    IMAGE_NT_HEADERS* nt = (IMAGE_NT_HEADERS*)(g_base + dos->e_lfanew);
    IMAGE_SECTION_HEADER* s = IMAGE_FIRST_SECTION(nt);
    WORD i;
    for (i = 0; i < nt->FileHeader.NumberOfSections; i++, s++) {
        if (memcmp(s->Name, ".text", 6) == 0) {
            g_textLo = g_base + s->VirtualAddress;
            g_textHi = g_textLo + s->Misc.VirtualSize;
        }
    }
}

static void read_settings(void)
{
    char url[256], *host, *colon, user[64];
    DWORD ulen = sizeof(user);
    g_blockSignout = GetPrivateProfileIntA("signin", "block_signout", 0, g_iniPath);
    g_netPort = GetPrivateProfileIntA("net", "port", 9104, g_iniPath);
    g_approveAll = GetPrivateProfileIntA("net", "approve_all", 1, g_iniPath);
    g_localToyboxList = GetPrivateProfileIntA("saves", "local_toybox_list", 1, g_iniPath);
    g_protectSaves = GetPrivateProfileIntA("saves", "protect", 1, g_iniPath);
    g_invitePopup = GetPrivateProfileIntA("net", "invite_popup", 1, g_iniPath);
    g_forceMpLevels = GetPrivateProfileIntA("net", "force_multiplayer_levels", 1, g_iniPath);
    if (!GetUserNameA(user, &ulen)) strcpy(user, "Jugador");
    GetPrivateProfileStringA("net", "name", "", g_name, sizeof(g_name), g_iniPath);
    g_nameFromIni = g_name[0] != 0;
    if (!g_nameFromIni) strcpy(g_name, user);       /* replaced by the Steam name once Steam is up */
    GetPrivateProfileStringA("server", "url", "http://127.0.0.1:8085", url, sizeof(url), g_iniPath);
    host = strstr(url, "://") ? strstr(url, "://") + 3 : url;
    if ((colon = strchr(host, '/')) != NULL) *colon = 0;
    if ((colon = strchr(host, ':')) != NULL) { *colon = 0; g_serverPort = atoi(colon + 1); }
    else g_serverPort = 80;
    strncpy(g_serverHost, host, sizeof(g_serverHost) - 1);
    {   /* stage 5: chat. The drawing settings are only passed on to main.lua ("chatcfg") */
        char fov[16], head[16];
        g_chatEnabled = GetPrivateProfileIntA("chat", "enabled", 1, g_iniPath);
        g_chatPollMs = GetPrivateProfileIntA("chat", "poll_ms", 1000, g_iniPath);
        if (g_chatPollMs < 250) g_chatPollMs = 250;
        if (g_chatPollMs > 10000) g_chatPollMs = 10000;
        GetPrivateProfileStringA("chat", "fov", "55", fov, sizeof(fov), g_iniPath);
        GetPrivateProfileStringA("chat", "head_height", "2.2", head, sizeof(head), g_iniPath);
        _snprintf(g_chatCfg, sizeof(g_chatCfg), "%d\t%s\t%s\t%d\t%d\t%d", g_chatEnabled, fov, head,
                  GetPrivateProfileIntA("chat", "mirror", 0, g_iniPath),
                  GetPrivateProfileIntA("chat", "seconds", 8, g_iniPath),
                  GetPrivateProfileIntA("chat", "debug", 0, g_iniPath));
        g_chatCfg[sizeof(g_chatCfg) - 1] = 0;
    }
}

static void start(void)
{
    char dir[MAX_PATH], *slash;
    DWORD at;
    BYTE* head;
    GetModuleFileNameA(g_self, dir, MAX_PATH);   /* ini / log next to this DLL: game folder, or mods\dinet_online under CrabeLoader */
    slash = strrchr(dir, '\\');
    if (slash) *slash = 0;
    _snprintf(g_logPath, MAX_PATH, "%s\\di_net.log", dir);
    _snprintf(g_iniPath, MAX_PATH, "%s\\di_net.ini", dir);
    _snprintf(g_luaPath, MAX_PATH, "%s\\dinet.lua", dir);
    InitializeCriticalSection(&g_socialLock);
    InitializeCriticalSection(&g_chatLock);
    InitializeCriticalSection(&g_dirLock);
    read_settings();
    g_base = (DWORD)GetModuleHandleA(NULL);
    rewrite_exe_urls();                         /* the server chosen in the F8 list (before the game reads its urls) */

    g_base = (DWORD)GetModuleHandleA(NULL);
    find_text();
    logf("dinet_bk stage 2 loaded: exe base %08X, .text %08X-%08X, block_signout=%d",
         g_base, g_textLo, g_textHi, g_blockSignout);

    at = rt(VA_DISNEYID_SIGNIN);
    g_signinBack = at + 5;
    if (install_jmp(at, kSigninPrologue, 5, hook_signin))
        logf("watching DisneyIdSignIn at %08X", VA_DISNEYID_SIGNIN);

    hook_social();
    if (GetPrivateProfileIntA("signin", "skip_migration_warning", 1, g_iniPath))
        skip_migration_warning();

    if (g_forceMpLevels) force_multiplayer_levels();
    if (GetPrivateProfileIntA("net", "canonical_zone_ids", 1, g_iniPath)) canonical_zone_ids();

    if (g_localToyboxList && patch_local_toybox_list())
        logf("Toy Box menu reads the local saves even when signed in (local_toybox_list=1)");

    if (pin_netz_port(g_netPort))
        logf("Net-Z UDP port pinned to %d (open it in the router to host over the internet)", g_netPort);

    at = rt(VA_JOIN_OR_HOST);
    g_joinOrHostBack = at + 6;
    if (install_jmp(at, kJoinOrHostPrologue, 6, hook_join_or_host))
        logf("watching CNetZSession::JoinOrHost at %08X", VA_JOIN_OR_HOST);

    if (install_jmp(rt(VA_SET_JOIN_SESSION), kSetJoinSession, 5, hook_set_join_session))
        logf("watching SetJoinSession at %08X", VA_SET_JOIN_SESSION);

    if (install_jmp(rt(VA_SET_CONN_STATE), kSetConnState, 5, hook_set_conn_state))
        logf("watching CKFOnline::SetConnectionState at %08X", VA_SET_CONN_STATE);

    /* CKFOnline::JoinGame starts with "push -1; push <SEH handler>" (6A FF 68 + relocated address). */
    at = rt(VA_ONLINE_JOINGAME);
    head = (BYTE*)at;
    if (head[0] == 0x6A && head[1] == 0xFF && head[2] == 0x68) {
        g_joinGameImm = *(DWORD*)(head + 3);
        g_joinGameBack = at + 7;
        if (install_jmp(at, NULL, 7, hook_join_game))
            logf("CKFOnline::JoinGame hooked at %08X", VA_ONLINE_JOINGAME);
    } else {
        logf("CKFOnline::JoinGame not hooked: unexpected bytes at %08X", VA_ONLINE_JOINGAME);
    }

    at = rt(VA_JOIN_REQUEST);
    head = (BYTE*)at;
    if (head[0] == 0x6A && head[1] == 0xFF && head[2] == 0x68) {
        g_joinReqImm = *(DWORD*)(head + 3);
        g_joinReqBack = at + 7;
        if (install_jmp(at, NULL, 7, hook_join_request))
            logf("watching join requests (ClientJoinRequestCallback) at %08X", VA_JOIN_REQUEST);
    }
    if (g_approveAll) {
        static const BYTE kDeny[6] = { 0x0F, 0x85, 0xB2, 0x00, 0x00, 0x00 };
        BYTE* p = (BYTE*)rt(VA_JOIN_DENY_BRANCH);
        DWORD old;
        if (memcmp(p, kDeny, 6) == 0 && VirtualProtect(p, 6, PAGE_EXECUTE_READWRITE, &old)) {
            memset(p, 0x90, 6);          /* never take the "deny" branch */
            VirtualProtect(p, 6, old, &old);
            FlushInstructionCache(GetCurrentProcess(), p, 6);
            logf("joins are approved even when the Gold Edition holds its offline locks (approve_all=1)");
        } else {
            logf("approve_all: unexpected bytes at %08X, not patched", VA_JOIN_DENY_BRANCH);
        }
    }

    at = rt(VA_ALL_NEGOTIATED);
    head = (BYTE*)at;
    g_negotiatedBack = at + 5;
    if (head[0] == 0xA1 && *(DWORD*)(head + 1) == rt(VA_PLAYERLIST_HEAD)) {
        if (install_jmp(at, NULL, 5, hook_all_negotiated))
            logf("PlayerListAllNegotiated hooked at %08X", VA_ALL_NEGOTIATED);
    } else {
        logf("PlayerListAllNegotiated not hooked: unexpected bytes at %08X", VA_ALL_NEGOTIATED);
    }

    /* PlayerListGetHost starts with "mov eax, [list head]" (A1 + relocated address). */
    at = rt(VA_PLAYERLIST_HOST);
    head = (BYTE*)at;
    g_listHeadAddr = rt(VA_PLAYERLIST_HEAD);
    g_hostBack = at + 5;
    if (head[0] == 0xA1 && *(DWORD*)(head + 1) == g_listHeadAddr) {
        if (install_jmp(at, NULL, 5, hook_playerlist_host))
            logf("PlayerListGetHost hooked at %08X", VA_PLAYERLIST_HOST);
    } else {
        logf("PlayerListGetHost not hooked: unexpected bytes at %08X", VA_PLAYERLIST_HOST);
    }

    CloseHandle(CreateThread(NULL, 0, worker, NULL, 0, NULL));
    CloseHandle(CreateThread(NULL, 0, social_worker, NULL, 0, NULL));
    if (g_chatEnabled) {
        CloseHandle(CreateThread(NULL, 0, chat_worker, NULL, 0, NULL));
        logf("chat: on (key T in game, poll every %d ms)", g_chatPollMs);
    }
}

BOOL WINAPI DllMain(HINSTANCE inst, DWORD reason, LPVOID reserved)
{
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        g_self = inst;
        DisableThreadLibraryCalls(inst);
        InitializeCriticalSection(&g_logLock);
        start();
    }
    return TRUE;
}
