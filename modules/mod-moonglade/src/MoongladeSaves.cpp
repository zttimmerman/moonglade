/*
 * mod-moonglade: per-character saves (openspec/changes/front-menu-saves; no core patch). See MoongladeSaves.h for
 * the save set and the change's design.md for the whole design.
 *
 * Configuration (mod_moonglade.conf, Moonglade.Saves.*): Enable, Dir (the lane's save store, rendered by the
 * launcher's config.write_all), AutoMinIntervalS, KeepAuto / KeepQuick / KeepBeforeLoad. Saves stay disabled, with
 * the reason logged and reported by `.moonglade save status`, when:
 *   - Enable is 0, or Dir is empty, relative or can't be created;
 *   - CharacterDatabase.WorkerThreads isn't 1: a load relies on the character database's async queue being FIFO with
 *     one worker, so it lands after every earlier write of the character (design.md 3, "Ordering").
 */

#include "MoongladeSaves.h"

#include "CharacterPackets.h"
#include "Chat.h"
#include "CommandScript.h"
#include "Config.h"
#include "AccountMgr.h"
#include "AsyncCallbackProcessor.h"
#include "CryptoHash.h"
#include "DatabaseEnv.h"
#include "DBCStores.h"
#include "GitRevision.h"
#include "QueryCallback.h"
#include "Timer.h"
#include "Transaction.h"
#include "World.h"
#include "Log.h"
#include "ObjectAccessor.h"
#include "Player.h"
#include "ScriptMgr.h"
#include "Util.h"
#include "WorldSession.h"

#include <zlib.h>

#include <algorithm>
#include <cstdio>
#include <ctime>
#include <fcntl.h>
#include <filesystem>
#include <functional>
#include <memory>
#include <string>
#include <thread>
#include <unistd.h>
#include <string_view>
#include <unordered_map>
#include <vector>

using namespace Acore::ChatCommands;
namespace fs = std::filesystem;

namespace MoongladeSaves
{
    struct Settings
    {
        bool enabled = false;
        std::string why = "not configured yet";      // why saves are disabled ("" when enabled)
        fs::path dir;
        uint32 autoMinIntervalS = 120;
        uint32 keepAuto = 5;
        uint32 keepQuick = 1;
        uint32 keepBeforeLoad = 3;
        int32 workerThreads = 1;
    };

    Settings g_cfg;
    std::string g_schemaFingerprint;                // sha256 over the save set's column lists (design.md 2)
    std::unordered_map<std::string, std::vector<std::string>> g_columns;   // table -> columns (ordinal order)
    std::unordered_map<std::string, std::vector<std::string>> g_types;     // table -> DATA_TYPE per column

    std::string Sha256Hex(std::string const& data);

    std::string Where(std::string_view where, uint32 guid)
    {
        std::string out(where);
        std::string const g = std::to_string(guid);
        for (size_t p; (p = out.find("{g}")) != std::string::npos; )
            out.replace(p, 3, g);
        return out;
    }

    /// Remove leftovers of saves a crash interrupted: every `.tmp-*` folder two levels below the store
    /// (<dir>/<ACCOUNT>/<character id>/.tmp-<id>). Returns how many were removed.
    uint32 CleanTmp()
    {
        uint32 n = 0;
        std::error_code ec;
        if (g_cfg.dir.empty() || !fs::is_directory(g_cfg.dir, ec))
            return 0;
        for (auto const& acct : fs::directory_iterator(g_cfg.dir, ec))
        {
            if (!acct.is_directory(ec))
                continue;
            for (auto const& chr : fs::directory_iterator(acct.path(), ec))
            {
                if (!chr.is_directory(ec))
                    continue;
                std::vector<fs::path> tmp;
                for (auto const& s : fs::directory_iterator(chr.path(), ec))
                    if (s.path().filename().string().rfind(".tmp-", 0) == 0)
                        tmp.push_back(s.path());
                for (fs::path const& p : tmp)
                {
                    fs::remove_all(p, ec);
                    ++n;
                }
            }
        }
        return n;
    }

    /// The running database's columns for every save-set table (INFORMATION_SCHEMA, ordinal order) and their
    /// fingerprint. A save whose fingerprint differs is refused (design.md 3, Checks).
    bool LoadSchema(std::string& why)
    {
        g_columns.clear();
        g_types.clear();
        std::string fp;
        for (SaveTable const& t : SAVE_SET)
        {
            QueryResult r = CharacterDatabase.Query("SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = '{}' ORDER BY ORDINAL_POSITION", t.name);
            if (!r)
            {
                why = "table " + std::string(t.name) + " not found";
                return false;
            }
            auto& cols = g_columns[std::string(t.name)];
            auto& types = g_types[std::string(t.name)];
            do
            {
                cols.push_back((*r)[0].Get<std::string>());
                types.push_back((*r)[1].Get<std::string>());
            } while (r->NextRow());
            fp += std::string(t.name) + ":";
            for (size_t i = 0; i < cols.size(); ++i)
                fp += cols[i] + " " + types[i] + ",";
            fp += ";";
        }
        g_schemaFingerprint = Sha256Hex(fp);
        return true;
    }


    // ------------------------------------------------------------------------------------------ store and writer

    constexpr uint64 MIN_FREE_BYTES = 2ull * 1024 * 1024 * 1024;     // harness/hslib/backups.py MIN_FREE_BYTES
    constexpr std::string_view MIDDOT = "\xC2\xB7";                  // " · " in labels (UTF-8)
    constexpr std::string_view TEST_ACCOUNT = "HSTEST";              // harness paths.TEST_USER: test switches only

    AsyncCallbackProcessor<QueryCallback> g_queries;                 // world thread (OnUpdate)
    AsyncCallbackProcessor<TransactionCallback> g_transactions;      // world thread (OnUpdate)

    /// `.moonglade save test ...` (console only, characters of the test account only): a load's transaction fails
    /// after N statements (saves.load_atomic); a save pauses S seconds after its first file (saves.atomic).
    struct TestSwitch
    {
        uint32 failAfter = 0;
        uint32 pauseAfterFirstFileS = 0;
    } g_test;

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

    std::string Q(std::string_view s) { return "\"" + JsonEscape(s) + "\""; }

    bool IsHexType(std::string const& type)
    {
        return type == "blob" || type == "tinyblob" || type == "mediumblob" || type == "longblob" || type == "binary"
            || type == "varbinary" || type == "bit";
    }

    bool IsFloatType(std::string const& type)
    {
        return type == "float" || type == "double" || type == "decimal";
    }

    /// SELECT list for a save-set table: every column as text. FLOAT/DOUBLE via DOUBLE, so positions are exact
    /// (the shortest text that reads back as the same double, which is the stored float); binary as hex.
    std::string SelectList(std::string const& table)
    {
        std::string out;
        auto const& cols = g_columns[table];
        auto const& types = g_types[table];
        for (size_t i = 0; i < cols.size(); ++i)
        {
            if (i)
                out += ", ";
            std::string c = "`" + cols[i] + "`";
            if (IsHexType(types[i]))
                out += "HEX(" + c + ")";
            else if (IsFloatType(types[i]))
                out += "CAST(CAST(" + c + " AS DOUBLE) AS CHAR)";
            else
                out += "CAST(" + c + " AS CHAR)";
        }
        return out;
    }

    using Row = std::vector<std::optional<std::string>>;

    /// One character's save-set rows, in SAVE_SET order.
    struct Snapshot
    {
        uint32 guid = 0;
        std::vector<std::vector<Row>> tables;     // index = SAVE_SET index
        uint32 pending = 0;
        bool failed = false;
    };

    std::string Gzip(std::string const& data, bool& ok)
    {
        z_stream zs{};
        ok = deflateInit2(&zs, 6, Z_DEFLATED, 15 + 16, 8, Z_DEFAULT_STRATEGY) == Z_OK;
        if (!ok)
            return {};
        std::string out;
        out.resize(deflateBound(&zs, data.size()) + 64);
        zs.next_in = reinterpret_cast<Bytef*>(const_cast<char*>(data.data()));
        zs.avail_in = uInt(data.size());
        zs.next_out = reinterpret_cast<Bytef*>(out.data());
        zs.avail_out = uInt(out.size());
        ok = deflate(&zs, Z_FINISH) == Z_STREAM_END;
        out.resize(zs.total_out);
        deflateEnd(&zs);
        return out;
    }

    std::string Gunzip(std::string const& data, bool& ok)
    {
        z_stream zs{};
        ok = inflateInit2(&zs, 15 + 16) == Z_OK;
        if (!ok)
            return {};
        std::string out;
        char buf[65536];
        zs.next_in = reinterpret_cast<Bytef*>(const_cast<char*>(data.data()));
        zs.avail_in = uInt(data.size());
        int rc;
        do
        {
            zs.next_out = reinterpret_cast<Bytef*>(buf);
            zs.avail_out = sizeof(buf);
            rc = inflate(&zs, Z_NO_FLUSH);
            if (rc != Z_OK && rc != Z_STREAM_END)
                break;
            out.append(buf, sizeof(buf) - zs.avail_out);
        } while (rc != Z_STREAM_END);
        ok = rc == Z_STREAM_END;
        inflateEnd(&zs);
        return out;
    }

    std::string Sha256Hex(std::string const& data)
    {
        auto d = Acore::Crypto::SHA256::GetDigestOf(reinterpret_cast<uint8 const*>(data.data()), data.size());
        std::string hex = ByteArrayToHexStr(d);
        std::transform(hex.begin(), hex.end(), hex.begin(), ::tolower);
        return hex;
    }

    /// Write a file and fsync it. False on any error.
    bool WriteFileSync(fs::path const& p, std::string const& data)
    {
        int fd = ::open(p.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0644);
        if (fd < 0)
            return false;
        size_t off = 0;
        while (off < data.size())
        {
            ssize_t n = ::write(fd, data.data() + off, data.size() - off);
            if (n <= 0)
            {
                ::close(fd);
                return false;
            }
            off += size_t(n);
        }
        bool ok = ::fsync(fd) == 0;
        return ::close(fd) == 0 && ok;
    }

    void FsyncDir(fs::path const& p)
    {
        int fd = ::open(p.c_str(), O_RDONLY);
        if (fd >= 0)
        {
            ::fsync(fd);
            ::close(fd);
        }
    }

    bool ReadFile(fs::path const& p, std::string& out)
    {
        FILE* f = std::fopen(p.c_str(), "rb");
        if (!f)
            return false;
        out.clear();
        char buf[65536];
        size_t n;
        while ((n = std::fread(buf, 1, sizeof(buf), f)) > 0)
            out.append(buf, n);
        bool ok = !std::ferror(f);
        std::fclose(f);
        return ok;
    }

    std::string AccountFolder(std::string name)
    {
        std::string out;
        for (char c : name)
            if (std::isalnum(static_cast<unsigned char>(c)))
                out += char(std::toupper(static_cast<unsigned char>(c)));
        return out;
    }

    std::string AccountName(uint32 accountId)
    {
        std::string name;
        if (!AccountMgr::GetName(accountId, name))
            return {};
        return AccountFolder(name);
    }

    fs::path CharDir(std::string const& account, uint32 guid)
    {
        return g_cfg.dir / account / std::to_string(guid);
    }

    std::string ZoneName(uint32 zone)
    {
        if (AreaTableEntry const* a = sAreaTableStore.LookupEntry(zone))
            if (char const* n = a->area_name[sWorld->GetDefaultDbcLocale()])
                if (*n)
                    return n;
        if (AreaTableEntry const* a = sAreaTableStore.LookupEntry(zone))
            if (char const* n = a->area_name[LOCALE_enUS])
                if (*n)
                    return n;
        return "Somewhere";
    }

    std::string FormatTime(time_t t, char const* fmt, bool utc)
    {
        tm tmv{};
        if (utc)
            gmtime_r(&t, &tmv);
        else
            localtime_r(&t, &tmv);
        char buf[64];
        std::strftime(buf, sizeof(buf), fmt, &tmv);
        return buf;
    }

    /// A save's label as the menus show it: the player's name for it, else "<Zone> · Level <n> · <Ddd HH:MM>";
    /// an autosave starts with its moment ("Rested: Goldshire · Level 5 · Tue 21:04").
    std::string DefaultLabel(std::string const& moment, uint32 zone, uint32 level, time_t t)
    {
        std::string s;
        if (!moment.empty())
            s = moment + ": ";
        s += ZoneName(zone) + " " + std::string(MIDDOT) + " Level " + std::to_string(level) + " " + std::string(MIDDOT)
            + " " + FormatTime(t, "%a %H:%M", false);
        return s;
    }

    /// A player's name for a save: printable, no tabs or '|' (the add-on's separators), at most 48 bytes.
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

    struct Manifest
    {
        std::string id, kind, label, createdUtc, createdLocal, account, name, sha256, schema, coreSha;
        uint32 accountId = 0, guid = 0, level = 0, cls = 0, race = 0, zone = 0, map = 0, playTime = 0, money = 0;
        int64 created = 0;
        std::string zoneName;
    };

    std::string ManifestJson(Manifest const& m)
    {
        std::string s = "{\n";
        s += "  \"format\": 1,\n";
        s += "  \"id\": " + Q(m.id) + ",\n";
        s += "  \"kind\": " + Q(m.kind) + ",\n";
        s += "  \"label\": " + Q(m.label) + ",\n";
        s += "  \"created\": " + std::to_string(m.created) + ",\n";
        s += "  \"created_utc\": " + Q(m.createdUtc) + ",\n";
        s += "  \"created_local\": " + Q(m.createdLocal) + ",\n";
        s += "  \"account\": " + Q(m.account) + ",\n";
        s += "  \"account_id\": " + std::to_string(m.accountId) + ",\n";
        s += "  \"character\": " + std::to_string(m.guid) + ",\n";
        s += "  \"name\": " + Q(m.name) + ",\n";
        s += "  \"level\": " + std::to_string(m.level) + ",\n";
        s += "  \"class\": " + std::to_string(m.cls) + ",\n";
        s += "  \"race\": " + std::to_string(m.race) + ",\n";
        s += "  \"zone\": " + std::to_string(m.zone) + ",\n";
        s += "  \"zone_name\": " + Q(m.zoneName) + ",\n";
        s += "  \"map\": " + std::to_string(m.map) + ",\n";
        s += "  \"play_time\": " + std::to_string(m.playTime) + ",\n";
        s += "  \"money\": " + std::to_string(m.money) + ",\n";
        s += "  \"core_sha\": " + Q(m.coreSha) + ",\n";
        s += "  \"schema\": " + Q(m.schema) + ",\n";
        s += "  \"sha256\": " + Q(m.sha256) + "\n";
        s += "}\n";
        return s;
    }

    std::string SnapshotJson(Snapshot const& snap)
    {
        std::string s = "{\"format\":1,\"schema\":{";
        bool first = true;
        for (SaveTable const& t : SAVE_SET)
        {
            std::string name(t.name);
            s += (first ? "" : ",") + Q(name) + ":[";
            first = false;
            auto const& cols = g_columns[name];
            auto const& types = g_types[name];
            for (size_t i = 0; i < cols.size(); ++i)
                s += (i ? "," : "") + Q(cols[i] + " " + types[i]);
            s += "]";
        }
        s += "},\"rows\":{";
        for (size_t ti = 0; ti < std::size(SAVE_SET); ++ti)
        {
            s += (ti ? "," : "") + Q(SAVE_SET[ti].name) + ":[";
            bool firstRow = true;
            for (Row const& row : snap.tables[ti])
            {
                s += firstRow ? "[" : ",[";
                firstRow = false;
                for (size_t i = 0; i < row.size(); ++i)
                    s += (i ? "," : "") + (row[i] ? Q(*row[i]) : std::string("null"));
                s += "]";
            }
            s += "]";
        }
        s += "}}\n";
        return s;
    }

    std::string ColumnValue(Snapshot const& snap, std::string_view column)
    {
        auto const& cols = g_columns["characters"];
        if (snap.tables.empty() || snap.tables[0].empty())
            return {};
        for (size_t i = 0; i < cols.size(); ++i)
            if (cols[i] == column)
                return snap.tables[0][0][i].value_or("");
        return {};
    }

    uint32 ColumnU32(Snapshot const& snap, std::string_view column)
    {
        return uint32(std::strtoul(ColumnValue(snap, column).c_str(), nullptr, 10));
    }

    struct SaveRequest
    {
        uint32 guid = 0;
        std::string kind;                         // manual | quick | auto | before-load
        std::string label;                        // the player's name for it ("" = the default label)
        std::string moment;                       // autosaves: "Rested", "Flight", "Arrived"
        bool online = false;                      // SaveToDB first (the character is in the world)
        std::function<void(bool, std::string const&, Manifest const&)> done;   // (ok, reason, manifest)
    };

    /// Rotation after a save: keep the newest N saves of `kind` for the character (manual saves never rotate).
    void Rotate(fs::path const& charDir, std::string const& kind)
    {
        uint32 keep = kind == "auto" ? g_cfg.keepAuto : kind == "quick" ? g_cfg.keepQuick
            : kind == "before-load" ? g_cfg.keepBeforeLoad : 0;
        if (kind == "manual" || keep == 0)
            return;
        std::vector<std::string> ids;
        std::error_code ec;
        std::string suffix = "-" + kind;
        for (auto const& e : fs::directory_iterator(charDir, ec))
        {
            std::string n = e.path().filename().string();
            if (n.rfind(".tmp-", 0) == 0 || !e.is_directory(ec))
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
            auto n = [](std::string const& x) { size_t p = x.rfind('-'); return std::isdigit(static_cast<unsigned char>(x[p + 1])) && p > 15 ? std::stoul(x.substr(p + 1)) : 1ul; };
            return n(a) < n(b);
        });
        while (ids.size() > keep)
        {
            fs::remove_all(charDir / ids.front(), ec);
            LOG_INFO("module", "[mod-moonglade] saves: rotated out {}", ids.front());
            ids.erase(ids.begin());
        }
    }

    /// Serialize and write one save atomically: .tmp-<id>/ (files fsynced, sha256 re-read) -> rename to <id>/, then
    /// rotation. World thread.
    void FinishSave(SaveRequest const& req, Snapshot const& snap)
    {
        Manifest m;
        auto fail = [&](std::string const& why)
        {
            LOG_ERROR("module", "[mod-moonglade] saves: save of {} failed: {}", req.guid, why);
            if (req.done)
                req.done(false, why, m);
        };
        if (snap.failed || snap.tables.empty() || snap.tables[0].size() != 1)
            return fail("couldn't read the character");
        m.guid = req.guid;
        m.kind = req.kind;
        m.accountId = ColumnU32(snap, "account");
        m.account = AccountName(m.accountId);
        if (m.account.empty())
            return fail("no account name");
        m.name = ColumnValue(snap, "name");
        m.level = ColumnU32(snap, "level");
        m.cls = ColumnU32(snap, "class");
        m.race = ColumnU32(snap, "race");
        m.zone = ColumnU32(snap, "zone");
        m.zoneName = ZoneName(m.zone);
        m.map = ColumnU32(snap, "map");
        m.playTime = ColumnU32(snap, "totaltime");
        m.money = ColumnU32(snap, "money");
        time_t now = time(nullptr);
        m.created = int64(now);
        m.createdUtc = FormatTime(now, "%Y-%m-%dT%H:%M:%SZ", true);
        m.createdLocal = FormatTime(now, "%Y-%m-%dT%H:%M:%S%z", false);
        m.label = req.label.empty() ? DefaultLabel(req.moment, m.zone, m.level, now) : req.label;
        m.schema = g_schemaFingerprint;
        m.coreSha = GitRevision::GetHash();

        fs::path charDir = CharDir(m.account, m.guid);
        std::error_code ec;
        fs::create_directories(charDir, ec);
        if (ec)
            return fail("can't create " + charDir.string() + ": " + ec.message());
        std::string base = FormatTime(now, "%Y%m%d-%H%M%S", true) + "-" + req.kind;
        // unique and increasing within one second: one more than the highest -<n> of this second and kind
        // (a bare id counts as 1), so an id freed by rotation is never reused for a newer save
        uint32 highest = 0;
        for (auto const& e : fs::directory_iterator(charDir, ec))
        {
            std::string n = e.path().filename().string();
            if (n.rfind(".tmp-", 0) == 0)
                n = n.substr(5);
            if (n == base)
                highest = std::max<uint32>(highest, 1);
            else if (n.rfind(base + "-", 0) == 0)
                highest = std::max<uint32>(highest, uint32(std::strtoul(n.c_str() + base.size() + 1, nullptr, 10)));
        }
        m.id = highest ? base + "-" + std::to_string(highest + 1) : base;

        bool ok = false;
        std::string gz = Gzip(SnapshotJson(snap), ok);
        if (!ok)
            return fail("compression failed");
        m.sha256 = Sha256Hex(gz);
        fs::path tmp = charDir / (".tmp-" + m.id);
        fs::create_directories(tmp, ec);
        if (ec || !WriteFileSync(tmp / "character.json.gz", gz))
        {
            fs::remove_all(tmp, ec);
            return fail("can't write the save file");
        }
        if (g_test.pauseAfterFirstFileS && m.account == TEST_ACCOUNT)
        {
            LOG_INFO("module", "[mod-moonglade] saves: test switch: pausing {} s after the first file of {}",
                g_test.pauseAfterFirstFileS, m.id);
            std::this_thread::sleep_for(std::chrono::seconds(g_test.pauseAfterFirstFileS));
        }
        std::string reread;
        if (!ReadFile(tmp / "character.json.gz", reread) || Sha256Hex(reread) != m.sha256
            || !WriteFileSync(tmp / "manifest.json", ManifestJson(m)))
        {
            fs::remove_all(tmp, ec);
            return fail("the save file didn't read back the same");
        }
        FsyncDir(tmp);
        fs::rename(tmp, charDir / m.id, ec);
        if (ec)
        {
            fs::remove_all(tmp, ec);
            return fail("can't finish the save folder");
        }
        FsyncDir(charDir);
        Rotate(charDir, req.kind);
        LOG_INFO("module", "[mod-moonglade] saves: {} save {} of {} ({}) written", req.kind, m.id, m.name, m.guid);
        if (req.done)
            req.done(true, "", m);
    }

    /// Take a save. Online: the character's state is saved to the database first (SaveToDB into one transaction),
    /// then one SELECT per save-set table is queued right behind it on the same FIFO async queue, back to back from
    /// the world thread, so the rows read are that save's. World thread only.
    void StartSave(SaveRequest req)
    {
        auto refuse = [&](std::string const& why)
        {
            if (req.done)
                req.done(false, why, Manifest{});
        };
        if (!g_cfg.enabled)
            return refuse("Saving is turned off.");
        std::error_code ec;
        fs::space_info sp = fs::space(g_cfg.dir, ec);
        if (ec || sp.available < MIN_FREE_BYTES)
            return refuse("There isn't enough free disk space to save.");
        if (req.online)
        {
            Player* player = ObjectAccessor::FindConnectedPlayer(ObjectGuid::Create<HighGuid::Player>(req.guid));
            if (!player || !player->IsInWorld())
                return refuse("The character isn't in the world.");
            CharacterDatabaseTransaction trans = CharacterDatabase.BeginTransaction();
            player->SaveToDB(trans, false, false);
            CharacterDatabase.CommitTransaction(trans);
        }
        auto snap = std::make_shared<Snapshot>();
        snap->guid = req.guid;
        snap->tables.resize(std::size(SAVE_SET));
        snap->pending = uint32(std::size(SAVE_SET));
        auto request = std::make_shared<SaveRequest>(std::move(req));
        for (size_t ti = 0; ti < std::size(SAVE_SET); ++ti)
        {
            std::string table(SAVE_SET[ti].name);
            std::string sql = "SELECT " + SelectList(table) + " FROM `" + table + "` WHERE "
                + Where(SAVE_SET[ti].where, snap->guid);
            size_t ncols = g_columns[table].size();
            g_queries.AddCallback(CharacterDatabase.AsyncQuery(sql).WithCallback(
                [snap, request, ti, ncols](QueryResult result)
            {
                if (result)
                {
                    do
                    {
                        Field* f = result->Fetch();
                        Row row;
                        row.reserve(ncols);
                        for (size_t i = 0; i < ncols; ++i)
                            row.push_back(f[i].IsNull() ? std::nullopt : std::optional<std::string>(f[i].Get<std::string>()));
                        snap->tables[ti].push_back(std::move(row));
                    } while (result->NextRow());
                }
                if (--snap->pending == 0)
                    FinishSave(*request, *snap);
            }));
        }
    }

    /// Why a player-requested save is refused now ("" = allowed). Bots never get here.
    std::string RefuseSave(Player* player)
    {
        if (player->IsInCombat())
            return "You can't save in combat.";
        if (!player->IsAlive() || player->HasPlayerFlag(PLAYER_FLAGS_GHOST))
            return "You can't save while dead.";
        if (player->m_movementInfo.HasMovementFlag(MovementFlags(MOVEMENTFLAG_FALLING | MOVEMENTFLAG_FALLING_FAR)))
            return "You can't save while falling.";
        if (player->InBattleground() || player->InArena())
            return "You can't save in a battleground or arena.";
        if (player->IsBeingTeleported())
            return "You can't save while travelling between places.";
        return {};
    }

    void Configure()
    {
        Settings s;
        s.dir = sConfigMgr->GetOption<std::string>("Moonglade.Saves.Dir", "");
        s.autoMinIntervalS = sConfigMgr->GetOption<uint32>("Moonglade.Saves.AutoMinIntervalS", 120);
        s.keepAuto = sConfigMgr->GetOption<uint32>("Moonglade.Saves.KeepAuto", 5);
        s.keepQuick = sConfigMgr->GetOption<uint32>("Moonglade.Saves.KeepQuick", 1);
        s.keepBeforeLoad = sConfigMgr->GetOption<uint32>("Moonglade.Saves.KeepBeforeLoad", 3);
        s.workerThreads = sConfigMgr->GetOption<int32>("CharacterDatabase.WorkerThreads", 1);
        bool enable = sConfigMgr->GetOption<bool>("Moonglade.Saves.Enable", true);
        std::error_code ec;
        if (!enable)
            s.why = "Moonglade.Saves.Enable = 0";
        else if (s.workerThreads != 1)
            s.why = "CharacterDatabase.WorkerThreads = " + std::to_string(s.workerThreads)
                + " (saves need 1: one FIFO async worker orders a load after every earlier write)";
        else if (s.dir.empty())
            s.why = "Moonglade.Saves.Dir is empty";
        else if (!s.dir.is_absolute())
            s.why = "Moonglade.Saves.Dir is not an absolute path";
        else if (fs::create_directories(s.dir, ec), ec)
            s.why = "Moonglade.Saves.Dir can't be created (" + ec.message() + ")";
        else
            s.why.clear();
        s.enabled = s.why.empty();
        g_cfg = s;
        if (g_cfg.enabled)
        {
            std::string why;
            if (!LoadSchema(why))
            {
                g_cfg.enabled = false;
                g_cfg.why = "schema: " + why;
            }
        }
        uint32 cleaned = g_cfg.enabled ? CleanTmp() : 0;
        if (g_cfg.enabled)
            LOG_INFO("module", "[mod-moonglade] saves enabled: dir {}, auto interval {} s, keep auto {} quick {} "
                "before-load {}, schema {}, {} unfinished save(s) removed", g_cfg.dir.string(), g_cfg.autoMinIntervalS,
                g_cfg.keepAuto, g_cfg.keepQuick, g_cfg.keepBeforeLoad, g_schemaFingerprint.substr(0, 12), cleaned);
        else
            LOG_WARN("module", "[mod-moonglade] saves disabled: {}", g_cfg.why);
    }
}

using namespace MoongladeSaves;

// ---------------------------------------------------------------------------------------------- requests
namespace MoongladeSaves
{
    constexpr std::string_view SAVE_PREFIX = "MGSAVE\t";       // SendAddonMessage prefix + the client's tab

    /// An add-on whisper to the player ("MGSAVE" + "\t" + line), as MoongladePause.cpp SendAddonState does.
    void Reply(Player* player, std::string const& line)
    {
        WorldPacket data;
        std::string text(SAVE_PREFIX);
        text += line;
        if (text.size() > 255)
            text.resize(255);
        ChatHandler::BuildChatPacket(data, CHAT_MSG_WHISPER, LANG_ADDON, player, player, text);
        player->SendDirectMessage(&data);
    }

    void ReplyTo(ObjectGuid guid, std::string const& line)
    {
        if (Player* p = ObjectAccessor::FindConnectedPlayer(guid))
            if (p->IsInWorld())
                Reply(p, line);
    }

    struct AddonRequest
    {
        ObjectGuid guid;
        std::string verb;
        std::string arg;
    };

    std::vector<AddonRequest> g_addonRequests;      // world thread only (CMSG_MESSAGECHAT runs in UpdateSessions)

    /// A player-requested save (manual or quick) from the add-on: refusals first, then the save; the result line
    /// is "OK\t<verb>\t<label>" or "NO\t<verb>\t<reason>".
    void PlayerSave(Player* player, std::string const& verb, std::string const& kind, std::string const& label)
    {
        if (std::string why = RefuseSave(player); !why.empty())
        {
            Reply(player, "NO\t" + verb + "\t" + why);
            return;
        }
        SaveRequest req;
        req.guid = player->GetGUID().GetCounter();
        req.kind = kind;
        req.label = CleanLabel(label);
        req.online = true;
        ObjectGuid g = player->GetGUID();
        req.done = [g, verb](bool ok, std::string const& why, Manifest const& m)
        {
            ReplyTo(g, ok ? "OK\t" + verb + "\t" + m.label : "NO\t" + verb + "\t" + why);
        };
        StartSave(std::move(req));
    }

    void HandleAddonRequest(AddonRequest const& r)
    {
        Player* player = ObjectAccessor::FindConnectedPlayer(r.guid);
        if (!player || !player->IsInWorld())
            return;
        if (r.verb == "SAVE")
        {
            // "SAVE manual <name>" (the name may be empty)
            std::string_view arg(r.arg);
            if (arg.substr(0, 6) != "manual")
            {
                Reply(player, "NO\tSAVE\tUnknown save kind.");
                return;
            }
            arg.remove_prefix(6);
            if (!arg.empty() && arg.front() == ' ')
                arg.remove_prefix(1);
            PlayerSave(player, "SAVE", "manual", std::string(arg));
        }
        else if (r.verb == "QUICK")
            PlayerSave(player, "QUICK", "quick", "");
        else
            Reply(player, "NO\t" + r.verb + "\tUnknown request.");
    }
}

class MoongladeSavesPlayerScript : public PlayerScript
{
public:
    MoongladeSavesPlayerScript() : PlayerScript("MoongladeSavesPlayerScript", { PLAYERHOOK_CAN_PLAYER_USE_PRIVATE_CHAT }) { }

    // "MGSAVE" add-on whispers to oneself: handled on the next world tick (a load logs the character out, which
    // must not happen inside the chat handler) and swallowed. Bots never save (design.md 4, "Who may save").
    bool OnPlayerCanUseChat(Player* player, uint32 type, uint32 language, std::string& msg, Player* receiver) override
    {
        if (type != CHAT_MSG_WHISPER || language != LANG_ADDON || receiver != player
            || std::string_view(msg).substr(0, SAVE_PREFIX.size()) != SAVE_PREFIX)
            return true;
        if (player->GetSession()->IsBot())
            return false;
        std::string_view rest = std::string_view(msg).substr(SAVE_PREFIX.size());
        size_t sp = rest.find(' ');
        AddonRequest r;
        r.guid = player->GetGUID();
        r.verb = std::string(rest.substr(0, sp));
        r.arg = sp == std::string_view::npos ? std::string() : std::string(rest.substr(sp + 1));
        if (g_addonRequests.size() < 64)
            g_addonRequests.push_back(std::move(r));
        return false;
    }
};

class MoongladeSavesWorldScript : public WorldScript
{
public:
    MoongladeSavesWorldScript() : WorldScript("MoongladeSavesWorldScript",
        { WORLDHOOK_ON_AFTER_CONFIG_LOAD, WORLDHOOK_ON_UPDATE }) { }

    void OnAfterConfigLoad(bool /*reload*/) override
    {
        Configure();
    }

    void OnUpdate(uint32 /*diff*/) override
    {
        g_queries.ProcessReadyCallbacks();
        g_transactions.ProcessReadyCallbacks();
        if (!g_addonRequests.empty())
        {
            std::vector<AddonRequest> reqs;
            reqs.swap(g_addonRequests);
            for (AddonRequest const& r : reqs)
                HandleAddonRequest(r);
        }
    }
};

class MoongladeSavesCommandScript : public CommandScript
{
public:
    MoongladeSavesCommandScript() : CommandScript("MoongladeSavesCommandScript") { }

    ChatCommandTable GetCommands() const override
    {
        static ChatCommandTable saveTable =
        {
            { "status",    HandleStatus,    SEC_GAMEMASTER, Console::Yes },
            { "tables",    HandleTables,    SEC_GAMEMASTER, Console::Yes },
            { "take",      HandleTake,      SEC_GAMEMASTER, Console::Yes },
            { "test",      HandleTest,      SEC_GAMEMASTER, Console::Yes },
        };
        static ChatCommandTable moongladeTable = { { "save", saveTable } };
        static ChatCommandTable commandTable = { { "moonglade", moongladeTable } };
        return commandTable;
    }

    /// `.moonglade save status`: enabled or the reason it isn't, the store, the schema fingerprint and the settings.
    static bool HandleStatus(ChatHandler* handler)
    {
        handler->PSendSysMessage("Moonglade saves: {} | why: {} | dir: {} | schema: {} | workers: {} | auto interval: {} "
            "| keep auto {} quick {} before-load {}", g_cfg.enabled ? "enabled" : "disabled",
            g_cfg.why.empty() ? "-" : g_cfg.why, g_cfg.dir.string(), g_schemaFingerprint.empty() ? "-" : g_schemaFingerprint,
            g_cfg.workerThreads, g_cfg.autoMinIntervalS, g_cfg.keepAuto, g_cfg.keepQuick, g_cfg.keepBeforeLoad);
        return true;
    }

    /// `.moonglade save tables`: the classification, one line per list (saves.table_coverage).
    static bool HandleTables(ChatHandler* handler)
    {
        std::string save, removed, kept;
        for (SaveTable const& t : SAVE_SET)
            save += (save.empty() ? "" : ",") + std::string(t.name);
        for (RemovedTable const& t : REMOVED_ON_LOAD)
            removed += (removed.empty() ? "" : ",") + std::string(t.name);
        for (std::string_view t : KEPT)
            kept += (kept.empty() ? "" : ",") + std::string(t);
        handler->PSendSysMessage("Moonglade save tables: in-save {}", save);
        handler->PSendSysMessage("Moonglade save tables: removed-on-load {}", removed);
        handler->PSendSysMessage("Moonglade save tables: kept {}", kept);
        return true;
    }

    /// `.moonglade save take <guid> [label]`: a manual save of a character, online (saved to the database first)
    /// or offline. Asynchronous: the result is logged; `.moonglade save list` shows it.
    static bool HandleTake(ChatHandler* handler, uint32 guid, Optional<Tail> label)
    {
        if (!g_cfg.enabled)
        {
            handler->PSendSysMessage("Moonglade save: refused: saves are disabled ({})", g_cfg.why);
            return true;
        }
        Player* player = ObjectAccessor::FindConnectedPlayer(ObjectGuid::Create<HighGuid::Player>(guid));
        if (player && player->GetSession()->IsBot())
        {
            handler->SendSysMessage("Moonglade save: refused: AI players never save");
            return true;
        }
        if (player)
            if (std::string why = RefuseSave(player); !why.empty())
            {
                handler->PSendSysMessage("Moonglade save: refused: {}", why);
                return true;
            }
        SaveRequest req;
        req.guid = guid;
        req.kind = "manual";
        req.label = label ? CleanLabel(std::string(*label)) : "";
        req.online = player && player->IsInWorld();
        StartSave(std::move(req));
        handler->PSendSysMessage("Moonglade save: queued a save of {}", guid);
        return true;
    }

    /// `.moonglade save test failafter <n> | pause <s> | off` (console only; affects characters of the test account
    /// only): the switches saves.load_atomic and saves.atomic use.
    static bool HandleTest(ChatHandler* handler, std::string what, Optional<uint32> n)
    {
        if (handler->GetSession())
        {
            handler->SendSysMessage("Moonglade save: test switches are console-only");
            return true;
        }
        if (what == "failafter")
            g_test.failAfter = n.value_or(0);
        else if (what == "pause")
            g_test.pauseAfterFirstFileS = std::min<uint32>(n.value_or(0), 120);
        else if (what == "off")
            g_test = TestSwitch{};
        else
        {
            handler->SendSysMessage("Moonglade save: test failafter <n> | pause <s> | off");
            return true;
        }
        handler->PSendSysMessage("Moonglade save: test failafter {} pause {} (account {} only)", g_test.failAfter,
            g_test.pauseAfterFirstFileS, TEST_ACCOUNT);
        return true;
    }
};

void AddMoongladeSavesScripts()
{
    new MoongladeSavesPlayerScript();
    new MoongladeSavesWorldScript();
    new MoongladeSavesCommandScript();
}
