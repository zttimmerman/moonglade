/*
 * mod-moonglade: pure saves logic (openspec/changes/test-pyramid design 4). This file and SavesLogic.cpp include only
 * the C++ standard library (and may include <zlib.h>); everything else comes in as plain parameters, so the host-built
 * unit tests (harness/tests/unit/cpp) and the module run the same code. MoongladeSaves.cpp calls these functions.
 */
#ifndef MOONGLADE_LOGIC_SAVES_H
#define MOONGLADE_LOGIC_SAVES_H

#include <cstdint>
#include <ctime>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace MoongladeLogic
{
    /// A player's name for a save: printable, no tabs or '|' (the add-on's separators), at most 48 bytes, never a
    /// UTF-8 sequence cut in half, leading and trailing spaces removed.
    std::string CleanLabel(std::string_view in);

    /// The retention choice after a save of `kind`: given the names of the character's save folders
    /// (<YYYYMMDD-HHMMSS>-<kind>[-n]; ".tmp-" folders and other kinds are ignored), the ones to delete so the newest
    /// `keep` of that kind remain, oldest first. Manual saves and keep == 0 never rotate.
    std::vector<std::string> RetentionDeletes(std::vector<std::string> const& names, std::string const& kind,
                                              std::uint32_t keep);

    /// A string as the inside of a JSON string literal: '"' and '\\' escaped, control bytes as \u00XX, every other
    /// byte (UTF-8 included) as is.
    std::string JsonEscape(std::string_view in);

    /// MySQL column types a save stores as hex (binary) / reads through DOUBLE (floating point).
    bool IsHexType(std::string const& type);
    bool IsFloatType(std::string const& type);

    /// A saved column value as an SQL literal for the load: NULL; binary as X'..' (anything not hex -> NULL); else
    /// a quoted string with '\\', '\'' and NUL escaped.
    std::string SqlValue(std::optional<std::string> const& v, std::string const& type);

    /// strftime of `t` in UTC or local time (the time comes in; the module passes the clock's).
    std::string FormatTime(std::time_t t, char const* fmt, bool utc);

    /// A save's label as the menus show it: "<Zone> · Level <n> · <Ddd HH:MM>" (local time), an autosave's starting
    /// with its moment ("Rested: Goldshire · Level 5 · Tue 21:04"). The zone's name comes in (the module reads it from
    /// AreaTable.dbc).
    std::string DefaultLabel(std::string const& moment, std::string const& zoneName, std::uint32_t level, std::time_t t);

    /// What a player-requested save is refused for: the module fills it from the Player.
    struct SaveState
    {
        bool inCombat = false, alive = true, ghost = false, falling = false, inBattlegroundOrArena = false,
             teleporting = false;
    };
    /// The refusal text for a player-requested save now ("" = allowed), first reason in this order: combat, dead,
    /// falling, battleground/arena, teleport.
    std::string SaveRefusal(SaveState const& s);

    /// Why saves are disabled at startup ("" = enabled): the switch off, CharacterDatabase.WorkerThreads other than 1
    /// (one FIFO async worker orders a load after every earlier write), or no save folder.
    std::string DisabledReason(bool enable, std::int32_t workerThreads, bool dirEmpty);
}

#endif
