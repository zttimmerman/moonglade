/*
 * mod-moonglade: pure true-pause logic (openspec/changes/test-pyramid design 4; MoongladePause.cpp calls it).
 * Standard library only: the decisions take plain flags and times.
 */
#ifndef MOONGLADE_LOGIC_PAUSE_H
#define MOONGLADE_LOGIC_PAUSE_H

#include <cstdint>

namespace MoongladeLogic
{
    enum class PauseDecision { Paused, AlreadyPaused, Refused, Disabled };

    /// A pause request: disabled by config, already paused, refused on a flight path or a transport (the client moves
    /// a passenger itself), else paused. `hasPlayer` false is the console's pause (no travel to check).
    PauseDecision DecidePause(bool enabled, bool alreadyPaused, bool hasPlayer, bool inFlight, bool onTransport);

    /// The module roots the pausing player only when nothing else already roots it (and then unroots it on resume).
    bool ModuleRoots(bool alreadyRooted);

    /// Each world tick while paused: resume when the pausing player is gone or its connection is closed (a console
    /// pause, with no owner, never ends this way).
    bool ResumeForOwner(bool paused, bool hasOwner, bool ownerConnected);

    /// The "paused" acknowledgement goes out on the first tick that runs paused.
    bool SendAck(bool ackPending, bool tickPaused);

    enum class CooldownAction { Skip, Modify, Resend };

    /// After a resume, for one spell cooldown the client tracks (`needSend`), whose server end time is `endMs` at
    /// game time `nowMs`, with the pause having lasted `pausedMs`: Skip when the client doesn't track it or it has
    /// ended; Modify (SMSG_MODIFY_COOLDOWN by pausedMs) when more than pausedMs remain, so the client's timer, which
    /// ran on, gets the paused time back; else Resend (SMSG_SPELL_COOLDOWN with `left`), which the client has already
    /// finished.
    CooldownAction ResyncCooldown(bool needSend, std::uint32_t endMs, std::uint32_t nowMs, std::uint32_t pausedMs,
                                  std::uint32_t* left);
}

#endif
