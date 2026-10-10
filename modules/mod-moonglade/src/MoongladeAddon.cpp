/*
 * mod-moonglade: the shared add-on message parser (see MoongladeAddon.h).
 */

#include "MoongladeAddon.h"

#include "Chat.h"
#include "Player.h"
#include "ScriptMgr.h"
#include "WorldPacket.h"

#include <unordered_map>

namespace
{
    std::unordered_map<std::string, MoongladeAddon::Handler>& Handlers()
    {
        static std::unordered_map<std::string, MoongladeAddon::Handler> handlers;
        return handlers;
    }
}

void MoongladeAddon::Register(std::string_view prefix, Handler handler)
{
    Handlers()[std::string(prefix)] = std::move(handler);
}

void MoongladeAddon::Send(Player* player, std::string_view prefix, std::string_view text)
{
    WorldPacket data;
    std::string msg(prefix);
    msg += '\t';
    msg += text;
    if (msg.size() > 255)
        msg.resize(255);
    ChatHandler::BuildChatPacket(data, CHAT_MSG_WHISPER, LANG_ADDON, player, player, msg);
    player->SendDirectMessage(&data);
}

class MoongladeAddonPlayerScript : public PlayerScript
{
public:
    MoongladeAddonPlayerScript() : PlayerScript("MoongladeAddonPlayerScript", { PLAYERHOOK_CAN_PLAYER_USE_PRIVATE_CHAT }) { }

    bool OnPlayerCanUseChat(Player* player, uint32 type, uint32 language, std::string& msg, Player* receiver) override
    {
        if (type != CHAT_MSG_WHISPER || language != LANG_ADDON || receiver != player)
            return true;
        std::string_view m(msg);
        size_t tab = m.find('\t');
        if (tab == std::string_view::npos)
            return true;
        auto it = Handlers().find(std::string(m.substr(0, tab)));
        if (it == Handlers().end())
            return true;
        it->second(player, m.substr(tab + 1));
        return false;                 // swallowed: the message never goes out
    }
};

void AddMoongladeAddonScripts()
{
    new MoongladeAddonPlayerScript();
}
