/*
 * mod-moonglade: pure dungeon-lockout logic (openspec/changes/test-pyramid design 4; MoongladeDungeons.cpp calls
 * it). Standard library only: the encounter table comes in as plain values.
 */
#ifndef MOONGLADE_LOGIC_DUNGEONS_H
#define MOONGLADE_LOGIC_DUNGEONS_H

#include <cstdint>
#include <vector>

namespace MoongladeLogic
{
    struct Encounter
    {
        bool lastEncounterDungeon;     // DungeonEncounter.lastEncounterDungeon (a final encounter)
        std::uint32_t encounterIndex;  // DungeonEncounter.dbc encounterIndex (its bit in the completed mask)
    };

    /// Every final encounter is in the completed-encounter mask. False when there's no final encounter.
    bool FinalDone(std::uint32_t mask, std::vector<Encounter> const& encounters);

    enum class EnterDecision { Keep, KeepPlayerInside, ResetAll, UnbindWithCompanions };

    /// A real player entering a 5-player dungeon whose save is finished (FinalDone): kept when it isn't finished or
    /// a real player is inside; reset for everyone when no map is loaded or the map reset succeeded; else (companions
    /// still inside) the player and the companions bound to it are unbound.
    EnterDecision DecideEnter(bool finished, bool realPlayerInside, bool mapLoaded, bool resetSucceeded);

    /// When a real player leaves a dungeon: keep its bind (permanent) when the save has progress but isn't finished.
    bool KeepOnLeave(std::uint32_t mask, bool finished);
}

#endif
