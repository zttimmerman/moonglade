// Unit tests for modules/mod-moonglade/src/logic/DungeonsLogic (dungeon-lockouts: a cleared copy is fresh next
// time, an unfinished one is kept, a real player inside keeps it, companions inside get unbound).
#include "DungeonsLogic.h"
#include "mgtest.h"

using namespace MoongladeLogic;
using E = std::vector<Encounter>;

MG_TEST(dungeons_final, all_finals_in_mask)
{
    E encs = {{false, 0}, {false, 1}, {true, 4}};
    MG_TRUE(FinalDone(0b10000, encs));
    MG_TRUE(FinalDone(0b10011, encs));
    MG_TRUE(!FinalDone(0b00011, encs));
}

MG_TEST(dungeons_final, two_finals_need_both)
{
    E encs = {{true, 2}, {true, 5}};
    MG_TRUE(!FinalDone(1u << 2, encs));
    MG_TRUE(FinalDone((1u << 2) | (1u << 5), encs));
}

MG_TEST(dungeons_final, no_final_is_never_done)
{
    MG_TRUE(!FinalDone(0xffffffffu, E{{false, 0}, {false, 1}}));
    MG_TRUE(!FinalDone(0xffffffffu, E{}));
}

MG_TEST(dungeons_enter, unfinished_kept)
{
    MG_TRUE(DecideEnter(false, false, true, true) == EnterDecision::Keep);
}

MG_TEST(dungeons_enter, finished_with_player_inside_kept)
{
    MG_TRUE(DecideEnter(true, true, true, false) == EnterDecision::KeepPlayerInside);
}

MG_TEST(dungeons_enter, finished_reset_or_unbind)
{
    MG_TRUE(DecideEnter(true, false, false, false) == EnterDecision::ResetAll);     // no map loaded
    MG_TRUE(DecideEnter(true, false, true, true) == EnterDecision::ResetAll);       // reset succeeded
    MG_TRUE(DecideEnter(true, false, true, false) == EnterDecision::UnbindWithCompanions);
}

MG_TEST(dungeons_leave, keep_progress_not_finished)
{
    MG_TRUE(KeepOnLeave(0b1, false));
    MG_TRUE(!KeepOnLeave(0, false));
    MG_TRUE(!KeepOnLeave(0b1, true));
}
