/*
 * mod-moonglade: pure saves logic (see SavesLogic.h). Standard library only.
 */
#include "SavesLogic.h"

#include <algorithm>
#include <cctype>

namespace MoongladeLogic
{
    std::string CleanLabel(std::string_view in)
    {
        std::string out;
        for (unsigned char c : in)
        {
            if (c < 0x20 || c == '|' || c == 0x7f)
                continue;
            out += char(c);
        }
        while (!out.empty() && out.back() == ' ')
            out.pop_back();
        size_t start = out.find_first_not_of(' ');
        out = start == std::string::npos ? std::string() : out.substr(start);
        if (out.size() > 48)
        {
            out.resize(48);
            while (!out.empty() && (static_cast<unsigned char>(out.back()) & 0xC0) == 0x80)
                out.pop_back();                       // never cut a UTF-8 sequence in half
            if (!out.empty() && (static_cast<unsigned char>(out.back()) & 0x80))
                out.pop_back();
        }
        return out;
    }

    std::vector<std::string> RetentionDeletes(std::vector<std::string> const& names, std::string const& kind,
                                              std::uint32_t keep)
    {
        std::vector<std::string> ids;
        if (kind == "manual" || keep == 0)
            return ids;
        std::string suffix = "-" + kind;
        for (std::string const& n : names)
        {
            if (n.rfind(".tmp-", 0) == 0)
                continue;
            // <YYYYMMDD-HHMMSS>-<kind>[-n]
            if (n.size() > 16 && n.compare(15, suffix.size(), suffix) == 0
                && (n.size() == 15 + suffix.size() || n[15 + suffix.size()] == '-'))
                ids.push_back(n);
        }
        std::sort(ids.begin(), ids.end(), [](std::string const& a, std::string const& b)
        {
            // same second: "-<kind>" before "-<kind>-2" before "-<kind>-10"
            if (a.substr(0, 15) != b.substr(0, 15))
                return a < b;
            auto n = [](std::string const& x)
            {
                size_t p = x.rfind('-');
                return std::isdigit(static_cast<unsigned char>(x[p + 1])) && p > 15 ? std::stoul(x.substr(p + 1)) : 1ul;
            };
            return n(a) < n(b);
        });
        std::vector<std::string> del;
        if (ids.size() > keep)
            del.assign(ids.begin(), ids.end() - keep);
        return del;
    }
}
