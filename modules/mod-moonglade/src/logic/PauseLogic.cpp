/*
 * mod-moonglade: pure true-pause logic (see PauseLogic.h). Standard library only.
 */
#include "PauseLogic.h"

namespace MoongladeLogic
{
    PauseDecision DecidePause(bool enabled, bool alreadyPaused, bool hasPlayer, bool inFlight, bool onTransport)
    {
        if (!enabled)
            return PauseDecision::Disabled;
        if (alreadyPaused)
            return PauseDecision::AlreadyPaused;
        if (hasPlayer && (inFlight || onTransport))
            return PauseDecision::Refused;
        return PauseDecision::Paused;
    }

    bool ModuleRoots(bool alreadyRooted)
    {
        return !alreadyRooted;
    }

    bool ResumeForOwner(bool paused, bool hasOwner, bool ownerConnected)
    {
        return paused && hasOwner && !ownerConnected;
    }

    bool SendAck(bool ackPending, bool tickPaused)
    {
        return ackPending && tickPaused;
    }

    CooldownAction ResyncCooldown(bool needSend, std::uint32_t endMs, std::uint32_t nowMs, std::uint32_t pausedMs,
                                  std::uint32_t* left)
    {
        if (!needSend || endMs <= nowMs)
            return CooldownAction::Skip;
        std::uint32_t l = endMs - nowMs;
        if (left)
            *left = l;
        return l > pausedMs ? CooldownAction::Modify : CooldownAction::Resend;
    }
}
