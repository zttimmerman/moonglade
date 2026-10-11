/*
 * mod-moonglade: pure dungeon-lockout logic (see DungeonsLogic.h). Standard library only.
 */
#include "DungeonsLogic.h"

namespace MoongladeLogic
{
    bool FinalDone(std::uint32_t mask, std::vector<Encounter> const& encounters)
    {
        bool any = false;
        for (Encounter const& e : encounters)
        {
            if (!e.lastEncounterDungeon)
                continue;
            any = true;
            if (!(mask & (1u << e.encounterIndex)))
                return false;
        }
        return any;
    }

    EnterDecision DecideEnter(bool finished, bool realPlayerInside, bool mapLoaded, bool resetSucceeded)
    {
        if (!finished)
            return EnterDecision::Keep;
        if (mapLoaded && realPlayerInside)
            return EnterDecision::KeepPlayerInside;
        if (!mapLoaded || resetSucceeded)
            return EnterDecision::ResetAll;
        return EnterDecision::UnbindWithCompanions;
    }

    bool KeepOnLeave(std::uint32_t mask, bool finished)
    {
        return mask != 0 && !finished;
    }
}
