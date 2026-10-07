/*
 * mod-moonglade: Moonglade's single-player presentation (openspec/changes/seamless-experience, client-connectivity
 * "No server-identity text on login").
 *
 * Moonglade.QuietServerText (default 1): entering the world produces no message of the day and none of the server or
 * module announcements that name a server, a core or a bot module. Found by reading what the pinned core and modules
 * send at login (core 7f12e89, mod-playerbots 7bae1b5; design.md "As built"):
 *   - SMSG_MOTD (WorldSession::HandlePlayerLoginFromDB, src/server/game/Handlers/CharacterHandler.cpp:843 and :1173).
 *     MotdMgr::CreateWorldPacket (src/server/game/Motd/MotdMgr.cpp:143-160) always appends a hard-coded
 *     "This server runs on AzerothCore" line, whatever the motd table holds, so the packet itself is held back.
 *   - "Server.LoginInfo" (CharacterHandler.cpp:846, the core's version line): off in worldserver.conf (config.py).
 *   - mod-playerbots' PlayerbotsPlayerScript::OnPlayerLogin (src/Script/Playerbots.cpp:150-160) sends two system
 *     lines to every real player, "This server runs with mod-playerbots ..." and "Playerbots: The server is configured
 *     with N bots."; held back by the exact text they start with. (mod-playerbots' note there asks repacks and public
 *     servers to keep them; Moonglade is a personal, never-distributed install, CLAUDE.md section 1.)
 * The filter is ServerScript::CanPacketSend (WorldSession::SendPacket, WorldSession.cpp:357), which only runs for
 * sessions with a real socket: bot sessions never get here (OnPlayerbotPacketSent runs before the socket check).
 * Nothing else is touched: GM command output, chat, and every other system message pass unchanged.
 */

#include "Config.h"
#include "Log.h"
#include "Opcodes.h"
#include "ScriptMgr.h"
#include "SharedDefines.h"
#include "WorldPacket.h"
#include "WorldSession.h"

#include <array>
#include <string_view>

namespace
{
    char const* const OPT_QUIET = "Moonglade.QuietServerText";
    bool g_quiet = true;

    // The text the held-back system lines start with, exactly as mod-playerbots sends them (Playerbots.cpp:150-152
    // and :159-160; SendSysMessage doesn't escape '|' by default, so the colour codes arrive as written).
    constexpr std::array<std::string_view, 2> QUIET_SYSTEM_PREFIXES = {
        "|cff00ff00This server runs with |cff00ccffmod-playerbots|r ",
        "|cff00ff00Playerbots:|r The server is configured with ",
    };

    // SMSG_MESSAGECHAT for a system line (ChatHandler::BuildChatPacket, src/server/game/Chat/Chat.cpp:273-345, the
    // default case without a GM tag): u8 type, i32 language, u64 sender, u32 flags, u64 receiver, u32 length + 1,
    // the message (C string), u8 chat tag.
    bool IsQuietSystemLine(WorldPacket const& packet)
    {
        constexpr std::size_t HEADER = 1 + 4 + 8 + 4 + 8 + 4;
        if (packet.size() <= HEADER || packet.contents()[0] != CHAT_MSG_SYSTEM)
            return false;
        std::string_view msg(reinterpret_cast<char const*>(packet.contents()) + HEADER, packet.size() - HEADER);
        for (std::string_view prefix : QUIET_SYSTEM_PREFIXES)
            if (msg.substr(0, prefix.size()) == prefix)
                return true;
        return false;
    }
}

class MoongladeQuietWorldScript : public WorldScript
{
public:
    MoongladeQuietWorldScript() : WorldScript("MoongladeQuietWorldScript", { WORLDHOOK_ON_AFTER_CONFIG_LOAD }) { }

    void OnAfterConfigLoad(bool /*reload*/) override
    {
        g_quiet = sConfigMgr->GetOption<bool>(OPT_QUIET, true);
        LOG_INFO("module", "[mod-moonglade] {} = {}", OPT_QUIET, g_quiet ? 1 : 0);
    }
};

class MoongladeQuietServerScript : public ServerScript
{
public:
    MoongladeQuietServerScript() : ServerScript("MoongladeQuietServerScript", { SERVERHOOK_CAN_PACKET_SEND }) { }

    bool CanPacketSend(WorldSession* /*session*/, WorldPacket const& packet) override
    {
        if (!g_quiet)
            return true;
        switch (packet.GetOpcode())
        {
            case SMSG_MOTD:
                return false;
            case SMSG_MESSAGECHAT:
                return !IsQuietSystemLine(packet);
            default:
                return true;
        }
    }
};

void AddMoongladeQuietScripts()
{
    new MoongladeQuietWorldScript();
    new MoongladeQuietServerScript();
}
