// Unit tests for the rest of SavesLogic (test-pyramid part 2, design 4: JSON, SQL values, labels, the refusal
// predicate, the startup refusal). Expected values follow the rules documented in SavesLogic.h.
#include "SavesLogic.h"
#include "mgtest.h"

#include <cstdlib>

using namespace MoongladeLogic;

MG_TEST(saves_json, escapes_quotes_backslashes_and_controls)
{
    MG_EQ(JsonEscape("a\"b\\c"), std::string("a\\\"b\\\\c"));
    MG_EQ(JsonEscape(std::string("x\ny\x01", 4)), std::string("x\\u000ay\\u0001"));
    MG_EQ(JsonEscape("Gold\xC2\xB7shire"), std::string("Gold\xC2\xB7shire"));     // UTF-8 kept as is
}

MG_TEST(saves_sql, types)
{
    MG_TRUE(IsHexType("blob") && IsHexType("varbinary") && IsHexType("bit") && !IsHexType("varchar"));
    MG_TRUE(IsFloatType("float") && IsFloatType("double") && IsFloatType("decimal") && !IsFloatType("int"));
}

MG_TEST(saves_sql, values)
{
    MG_EQ(SqlValue(std::nullopt, "int"), std::string("NULL"));
    MG_EQ(SqlValue(std::string("12"), "int"), std::string("'12'"));
    MG_EQ(SqlValue(std::string("O'Neil\\x"), "varchar"), std::string("'O\\'Neil\\\\x'"));
    MG_EQ(SqlValue(std::string("a\0b", 3), "varchar"), std::string("'a\\0b'"));
    MG_EQ(SqlValue(std::string("0A1f"), "blob"), std::string("X'0A1f'"));
    MG_EQ(SqlValue(std::string("0A1g"), "blob"), std::string("NULL"));        // not hex: never pasted into SQL
}

MG_TEST(saves_label, default_label_with_and_without_moment)
{
    setenv("TZ", "UTC", 1);
    tzset();
    std::time_t t = 1791700000;                       // Tue 2026-10-11 ... (UTC), formatted by the same strftime
    std::string when = FormatTime(t, "%a %H:%M", false);
    MG_EQ(DefaultLabel("", "Goldshire", 5, t), "Goldshire \xC2\xB7 Level 5 \xC2\xB7 " + when);
    MG_EQ(DefaultLabel("Rested", "Goldshire", 5, t), "Rested: Goldshire \xC2\xB7 Level 5 \xC2\xB7 " + when);
    MG_EQ(FormatTime(0, "%Y-%m-%dT%H:%M:%SZ", true), std::string("1970-01-01T00:00:00Z"));
}

MG_TEST(saves_refusal, each_reason_in_order)
{
    SaveState ok;
    MG_EQ(SaveRefusal(ok), std::string(""));
    SaveState s = ok;
    s.inCombat = true;
    s.alive = false;                                  // combat comes first
    MG_EQ(SaveRefusal(s), std::string("You can't save in combat."));
    s = ok; s.alive = false;
    MG_EQ(SaveRefusal(s), std::string("You can't save while dead."));
    s = ok; s.ghost = true;
    MG_EQ(SaveRefusal(s), std::string("You can't save while dead."));
    s = ok; s.falling = true;
    MG_EQ(SaveRefusal(s), std::string("You can't save while falling."));
    s = ok; s.inBattlegroundOrArena = true;
    MG_EQ(SaveRefusal(s), std::string("You can't save in a battleground or arena."));
    s = ok; s.teleporting = true;
    MG_EQ(SaveRefusal(s), std::string("You can't save while travelling between places."));
}

MG_TEST(saves_startup, disabled_reasons)
{
    MG_EQ(DisabledReason(true, 1, false), std::string(""));
    MG_EQ(DisabledReason(false, 1, false), std::string("Moonglade.Saves.Enable = 0"));
    MG_TRUE(DisabledReason(true, 2, false).rfind("CharacterDatabase.WorkerThreads = 2", 0) == 0);
    MG_EQ(DisabledReason(true, 1, true), std::string("Moonglade.Saves.Dir is empty"));
}
