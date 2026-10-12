// Unit tests for modules/mod-moonglade/src/logic/ChatFilterLogic (m4-party-preset, dungeon-companions "The filter
// drops only AutoBalance lines"; check id unit:cpp:chat_filter). The AutoBalance texts are copied from the pinned
// module's source (vendor/mod-autobalance/src/Message.cpp enUS rows, ABPlayerScript.cpp:15).
#include "ChatFilterLogic.h"
#include "mgtest.h"

using namespace MoongladeLogic;

MG_TEST(chat_filter, drops_combat_ended_line)
{
    // Message.cpp:502 "leaving_instance_combat_change", sent by ABPlayerScript.cpp:224
    MG_TRUE(IsAutoBalanceLine("|cffc3dbff [AutoBalance]|r|cffFF8000 Combat has ended. Difficulty is no longer locked.|r"));
}

MG_TEST(chat_filter, drops_other_autobalance_lines)
{
    // Message.cpp:37 "announce_non_gm_entering_instance", formatted
    MG_TRUE(IsAutoBalanceLine("|cffc3dbff [AutoBalance]|r|cffFF8000 Hsprobe enters the instance. There are 2 player(s) "
                              "in this instance. Difficulty set to 2 player(s).|r"));
    // ABPlayerScript.cpp:15, the login announcement
    MG_TRUE(IsAutoBalanceLine("This server is running the |cff4CFF00AutoBalance |rmodule."));
}

MG_TEST(chat_filter, ordinary_messages_pass)
{
    MG_TRUE(!IsAutoBalanceLine("You have been inactive for some time."));
    MG_TRUE(!IsAutoBalanceLine(""));
    // `.ab mapstat` output (ABCommandScript.cpp:51-79) has no tag: the harness reads it
    MG_TRUE(!IsAutoBalanceLine("Players on map: 5 (Lvl 18 - 18)"));
    MG_TRUE(!IsAutoBalanceLine("Adjusted Player Count: 5|r"));
    // the tag elsewhere in a line, or without its colour code, is not AutoBalance's own line
    MG_TRUE(!IsAutoBalanceLine("Hsprobe says: [AutoBalance] is a module"));
    MG_TRUE(!IsAutoBalanceLine("[AutoBalance] Combat has ended."));
    // a shorter text that is a prefix of the tag
    MG_TRUE(!IsAutoBalanceLine("|cffc3dbff [Auto"));
}
