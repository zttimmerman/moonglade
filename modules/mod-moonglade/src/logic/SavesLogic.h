/*
 * mod-moonglade: pure saves logic (openspec/changes/test-pyramid design 4). This file and SavesLogic.cpp include only
 * the C++ standard library (and may include <zlib.h>); everything else comes in as plain parameters, so the host-built
 * unit tests (harness/tests/unit/cpp) and the module run the same code. MoongladeSaves.cpp calls these functions.
 */
#ifndef MOONGLADE_LOGIC_SAVES_H
#define MOONGLADE_LOGIC_SAVES_H

#include <cstdint>
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
}

#endif
