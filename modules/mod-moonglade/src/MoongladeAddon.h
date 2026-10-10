/*
 * mod-moonglade: the add-on message channel shared by true-pause (MGPAUSE) and front-menu-saves (MGSAVE).
 *
 * The Moonglade add-on talks to the server with SendAddonMessage(<prefix>, <text>, "WHISPER", <own name>). That arrives
 * as CMSG_MESSAGECHAT with LANG_ADDON, a thread-unsafe opcode handled in World::UpdateSessions (the world thread), and
 * Player::Whisper calls OnPlayerCanUseChat(..., receiver) for it. One PlayerScript (MoongladeAddon.cpp) parses
 * "<prefix>\t<text>" once and hands <text> to the handler registered for <prefix>; a message for a registered
 * prefix is always swallowed (it never goes out as a whisper). Handlers run on the world thread, inside the chat
 * handler: they must not log the player out there (queue it for WorldScript::OnUpdate instead).
 */

#ifndef MOONGLADE_ADDON_H
#define MOONGLADE_ADDON_H

#include <functional>
#include <string>
#include <string_view>

class Player;

namespace MoongladeAddon
{
    using Handler = std::function<void(Player* player, std::string_view text)>;

    /// Register the handler for one prefix (at script load; not thread-safe).
    void Register(std::string_view prefix, Handler handler);

    /// An add-on whisper to the player itself: "<prefix>\t<text>" (cut at 255 bytes, the chat limit).
    void Send(Player* player, std::string_view prefix, std::string_view text);
}

#endif
