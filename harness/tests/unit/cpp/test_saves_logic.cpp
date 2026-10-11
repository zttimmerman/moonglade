// Unit tests for modules/mod-moonglade/src/logic/SavesLogic (test-pyramid task 1.1). Expected values follow the
// documented rules in SavesLogic.h and MoongladeSaves.h (the save set's folder names, the 48-byte label limit).
#include "SavesLogic.h"
#include "mgtest.h"

using MoongladeLogic::CleanLabel;
using MoongladeLogic::RetentionDeletes;
using V = std::vector<std::string>;

MG_TEST(saves_label, keeps_plain_text)
{
    MG_EQ(CleanLabel("Before Hogger"), std::string("Before Hogger"));
}

MG_TEST(saves_label, drops_separators_and_controls)
{
    MG_EQ(CleanLabel("a|b\tc\nd\x7f" "e"), std::string("abcde"));
}

MG_TEST(saves_label, trims_spaces)
{
    MG_EQ(CleanLabel("   Deadmines run   "), std::string("Deadmines run"));
    MG_EQ(CleanLabel("    "), std::string(""));
}

MG_TEST(saves_label, caps_at_48_bytes)
{
    MG_EQ(CleanLabel(std::string(60, 'x')).size(), size_t(48));
}

MG_TEST(saves_label, never_cuts_utf8)
{
    // 47 ASCII bytes then a 2-byte "é": byte 48 would be the lead byte alone, so it goes too
    std::string in = std::string(47, 'a') + "\xc3\xa9" + "zz";
    MG_EQ(CleanLabel(in), std::string(47, 'a'));
    // 46 ASCII bytes + "é" ending exactly at byte 48: the result is a prefix of at most 48 bytes and ends on a whole
    // character. (Today the module also drops that whole "é"; finding recorded in design.md 12, behaviour unchanged.)
    std::string fits = std::string(46, 'a') + "\xc3\xa9" + "zz";
    std::string got = CleanLabel(fits);
    MG_TRUE(got.size() <= 48 && fits.compare(0, got.size(), got) == 0);
    MG_TRUE(got.empty() || (static_cast<unsigned char>(got.back()) & 0x80) == 0 || got.size() == 48);
}

MG_TEST(saves_retention, keeps_newest_autos)
{
    V names = {"20261001-100000-auto", "20261001-110000-auto", "20261001-120000-auto", "20261001-130000-auto",
               "20261001-140000-auto", "20261001-150000-auto", "20261001-160000-auto"};
    MG_EQ(RetentionDeletes(names, "auto", 5), (V{"20261001-100000-auto", "20261001-110000-auto"}));
}

MG_TEST(saves_retention, other_kinds_and_tmp_ignored)
{
    V names = {"20261001-100000-quick", "20261001-110000-quick", "20261001-090000-manual",
               ".tmp-20261001-080000-quick", "20261001-070000-before-load", "20261001-060000-autox"};
    MG_EQ(RetentionDeletes(names, "quick", 1), (V{"20261001-100000-quick"}));
}

MG_TEST(saves_retention, manual_never_rotates)
{
    V names = {"20261001-100000-manual", "20261001-110000-manual"};
    MG_EQ(RetentionDeletes(names, "manual", 1), V{});
    MG_EQ(RetentionDeletes({"20261001-100000-auto", "20261001-110000-auto"}, "auto", 0), V{});
}

MG_TEST(saves_retention, same_second_numeric_order)
{
    // same second: "-auto" before "-auto-2" before "-auto-10" (not string order, where "-10" < "-2")
    V names = {"20261001-100000-auto-10", "20261001-100000-auto", "20261001-100000-auto-2"};
    MG_EQ(RetentionDeletes(names, "auto", 1), (V{"20261001-100000-auto", "20261001-100000-auto-2"}));
}

MG_TEST(saves_retention, before_load_keeps_three)
{
    V names = {"20261001-100000-before-load", "20261002-100000-before-load", "20261003-100000-before-load",
               "20261004-100000-before-load", "20261004-100000-auto"};
    MG_EQ(RetentionDeletes(names, "before-load", 3), (V{"20261001-100000-before-load"}));
}
