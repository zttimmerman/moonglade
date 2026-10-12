/*
 * mod-moonglade: pure chat-filter logic (see ChatFilterLogic.h).
 */
#include "ChatFilterLogic.h"

#include <array>

namespace MoongladeLogic
{
    namespace
    {
        // The exact starts of mod-autobalance's player-facing lines at our pin (fork.lock autobalance_sha): the tag
        // every Message.cpp locale string begins with, and the login announcement (ABPlayerScript.cpp:15).
        constexpr std::array<std::string_view, 2> AUTOBALANCE_PREFIXES = {
            "|cffc3dbff [AutoBalance]|r",
            "This server is running the |cff4CFF00AutoBalance |rmodule.",
        };
    }

    bool IsAutoBalanceLine(std::string_view message)
    {
        for (std::string_view prefix : AUTOBALANCE_PREFIXES)
            if (message.substr(0, prefix.size()) == prefix)
                return true;
        return false;
    }
}
