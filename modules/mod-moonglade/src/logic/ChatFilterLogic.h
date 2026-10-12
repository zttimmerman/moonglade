/*
 * mod-moonglade: pure chat-filter logic (openspec/changes/m4-party-preset, dungeon-companions "AutoBalance's chat lines
 * stay hidden"; MoongladeQuiet.cpp calls it). Standard library only: a decision over one system message's text.
 */
#ifndef MOONGLADE_LOGIC_CHAT_FILTER_H
#define MOONGLADE_LOGIC_CHAT_FILTER_H

#include <string_view>

namespace MoongladeLogic
{
    /// True for a system line mod-autobalance sends to players, which the player never sees:
    /// - every line built from its locale table, which all start with the "[AutoBalance]" tag
    ///   (vendor/mod-autobalance/src/Message.cpp, e.g. :502 "leaving_instance_combat_change", the "Combat has ended"
    ///   line ABPlayerScript.cpp:224 sends when a party member leaves mid-fight, which no setting turns off);
    /// - its login announcement (ABPlayerScript.cpp:15; also off by config, AutoBalanceAnnounce.enable = 0).
    /// `.ab` command output (ABCommandScript.cpp) carries no tag and passes, as does every other message.
    bool IsAutoBalanceLine(std::string_view message);
}

#endif
