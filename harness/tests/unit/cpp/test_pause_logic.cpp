// Unit tests for modules/mod-moonglade/src/logic/PauseLogic (true-pause rules: refused while travelling, disabled by
// config, only one pause; the module's own root; resume when the pausing player is gone; the cooldown resync).
#include "PauseLogic.h"
#include "mgtest.h"

using namespace MoongladeLogic;

MG_TEST(pause_decide, paused_when_enabled_and_idle)
{
    MG_TRUE(DecidePause(true, false, true, false, false) == PauseDecision::Paused);
    MG_TRUE(DecidePause(true, false, false, false, false) == PauseDecision::Paused);   // the console's pause
}

MG_TEST(pause_decide, disabled_wins)
{
    MG_TRUE(DecidePause(false, false, true, true, false) == PauseDecision::Disabled);
    MG_TRUE(DecidePause(false, true, true, false, false) == PauseDecision::Disabled);
}

MG_TEST(pause_decide, refused_on_taxi_or_transport)
{
    MG_TRUE(DecidePause(true, false, true, true, false) == PauseDecision::Refused);
    MG_TRUE(DecidePause(true, false, true, false, true) == PauseDecision::Refused);
}

MG_TEST(pause_decide, already_paused)
{
    MG_TRUE(DecidePause(true, true, true, true, false) == PauseDecision::AlreadyPaused);
}

MG_TEST(pause_root, only_when_not_rooted)
{
    MG_TRUE(ModuleRoots(false));
    MG_TRUE(!ModuleRoots(true));
}

MG_TEST(pause_owner, resume_when_owner_gone)
{
    MG_TRUE(ResumeForOwner(true, true, false));
    MG_TRUE(!ResumeForOwner(true, true, true));
    MG_TRUE(!ResumeForOwner(true, false, false));     // a console pause has no owner
    MG_TRUE(!ResumeForOwner(false, true, false));
}

MG_TEST(pause_ack, first_paused_tick)
{
    MG_TRUE(SendAck(true, true));
    MG_TRUE(!SendAck(true, false));
    MG_TRUE(!SendAck(false, true));
}

MG_TEST(pause_cooldown, modify_when_more_than_pause_left)
{
    std::uint32_t left = 0;
    MG_TRUE(ResyncCooldown(true, 70000, 10000, 20000, &left) == CooldownAction::Modify);
    MG_EQ(left, std::uint32_t(60000));
}

MG_TEST(pause_cooldown, resend_when_client_already_finished)
{
    std::uint32_t left = 0;
    MG_TRUE(ResyncCooldown(true, 25000, 10000, 20000, &left) == CooldownAction::Resend);
    MG_EQ(left, std::uint32_t(15000));
    MG_TRUE(ResyncCooldown(true, 30000, 10000, 20000, &left) == CooldownAction::Resend);   // exactly the pause
}

MG_TEST(pause_cooldown, skip_untracked_or_ended)
{
    MG_TRUE(ResyncCooldown(false, 70000, 10000, 20000, nullptr) == CooldownAction::Skip);
    MG_TRUE(ResyncCooldown(true, 10000, 10000, 20000, nullptr) == CooldownAction::Skip);
    MG_TRUE(ResyncCooldown(true, 5000, 10000, 20000, nullptr) == CooldownAction::Skip);
}
