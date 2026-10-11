/*
 * mod-moonglade: pure saves logic (see SavesLogic.h). Standard library only.
 */
#include "SavesLogic.h"

#include <algorithm>
#include <cctype>
#include <cstdio>

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

    std::string JsonEscape(std::string_view in)
    {
        std::string out;
        out.reserve(in.size() + 2);
        for (unsigned char c : in)
        {
            if (c == '"' || c == '\\')
            {
                out += '\\';
                out += char(c);
            }
            else if (c < 0x20)
            {
                char buf[8];
                std::snprintf(buf, sizeof(buf), "\\u%04x", c);
                out += buf;
            }
            else
                out += char(c);
        }
        return out;
    }

    bool IsHexType(std::string const& type)
    {
        return type == "blob" || type == "tinyblob" || type == "mediumblob" || type == "longblob" || type == "binary"
            || type == "varbinary" || type == "bit";
    }

    bool IsFloatType(std::string const& type)
    {
        return type == "float" || type == "double" || type == "decimal";
    }

    std::string SqlValue(std::optional<std::string> const& v, std::string const& type)
    {
        if (!v)
            return "NULL";
        if (IsHexType(type))
        {
            for (char c : *v)
                if (!std::isxdigit(static_cast<unsigned char>(c)))
                    return "NULL";
            return "X'" + *v + "'";
        }
        std::string out = "'";
        for (char c : *v)
        {
            if (c == '\\' || c == '\'')
                out += '\\';
            if (c == '\0')
            {
                out += "\\0";
                continue;
            }
            out += c;
        }
        return out + "'";
    }

    std::string FormatTime(std::time_t t, char const* fmt, bool utc)
    {
        std::tm tmv{};
        if (utc)
            gmtime_r(&t, &tmv);
        else
            localtime_r(&t, &tmv);
        char buf[64];
        std::strftime(buf, sizeof(buf), fmt, &tmv);
        return buf;
    }

    std::string DefaultLabel(std::string const& moment, std::string const& zoneName, std::uint32_t level, std::time_t t)
    {
        static constexpr char const* MIDDOT = "\xC2\xB7";             // " · " in labels (UTF-8)
        std::string s;
        if (!moment.empty())
            s = moment + ": ";
        s += zoneName + " " + MIDDOT + " Level " + std::to_string(level) + " " + MIDDOT + " " + FormatTime(t, "%a %H:%M", false);
        return s;
    }

    std::string SaveRefusal(SaveState const& s)
    {
        if (s.inCombat)
            return "You can't save in combat.";
        if (!s.alive || s.ghost)
            return "You can't save while dead.";
        if (s.falling)
            return "You can't save while falling.";
        if (s.inBattlegroundOrArena)
            return "You can't save in a battleground or arena.";
        if (s.teleporting)
            return "You can't save while travelling between places.";
        return {};
    }

    std::string DisabledReason(bool enable, std::int32_t workerThreads, bool dirEmpty)
    {
        if (!enable)
            return "Moonglade.Saves.Enable = 0";
        if (workerThreads != 1)
            return "CharacterDatabase.WorkerThreads = " + std::to_string(workerThreads)
                + " (saves need 1: one FIFO async worker orders a load after every earlier write)";
        if (dirEmpty)
            return "Moonglade.Saves.Dir is empty";
        return {};
    }
}
