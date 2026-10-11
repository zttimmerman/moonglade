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
#include "logic/SavesLogic.h"

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
#include "MoongladeAddon.h"
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
#include <mutex>
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

    /// An add-on whisper to the player: "MGSAVE\t<line>" (the shared channel, MoongladeAddon.h).
    void Reply(Player* player, std::string const& line)
    {
        MoongladeAddon::Send(player, "MGSAVE", line);
    }

    void ReplyTo(ObjectGuid guid, std::string const& line)
    {
        if (Player* p = ObjectAccessor::FindConnectedPlayer(guid))
            if (p->IsInWorld())
                Reply(p, line);
    }


    constexpr uint64 MIN_FREE_BYTES = 2ull * 1024 * 1024 * 1024;     // harness/hslib/backups.py MIN_FREE_BYTES
    // the harness's test accounts (paths.TEST_USER, paths.SAVES_USER): the only ones the test switches apply to
    bool IsTestAccount(std::string_view account) { return account == "HSTEST" || account == "HSSAVES"; }

    AsyncCallbackProcessor<QueryCallback> g_queries;                 // world thread (OnUpdate)
    AsyncCallbackProcessor<TransactionCallback> g_transactions;      // world thread (OnUpdate)

    /// `.moonglade save test ...` (console only, characters of the test account only): a load's transaction fails
    /// after N statements (saves.load_atomic); a save pauses S seconds after its first file (saves.atomic).
    struct TestSwitch
    {
        uint32 failAfter = 0;
        uint32 pauseAfterFirstFileS = 0;
    } g_test;

    // pure logic (logic/SavesLogic.cpp, unit-tested on the host): the module calls it, never a copy
    using MoongladeLogic::JsonEscape;
    using MoongladeLogic::IsHexType;
    using MoongladeLogic::IsFloatType;
    using MoongladeLogic::SqlValue;
    using MoongladeLogic::FormatTime;

    std::string Q(std::string_view s) { return "\"" + JsonEscape(s) + "\""; }

    /// SELECT list for a save-set table (alias `t`): every column as text. FLOAT/DOUBLE via DOUBLE, so positions are
    /// exact (the shortest text that reads back as the same double, which is the stored float); binary as hex.
    std::string SelectList(std::string const& table)
    {
        std::string out;
        auto const& cols = g_columns[table];
        auto const& types = g_types[table];
        for (size_t i = 0; i < cols.size(); ++i)
        {
            if (i)
                out += ", ";
            std::string c = "`t`.`" + cols[i] + "`";
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

    /// A save's label as the menus show it: the player's name for it, else "<Zone> · Level <n> · <Ddd HH:MM>";
    /// an autosave starts with its moment ("Rested: Goldshire · Level 5 · Tue 21:04").
    std::string DefaultLabel(std::string const& moment, uint32 zone, uint32 level, time_t t)
    {
        return MoongladeLogic::DefaultLabel(moment, ZoneName(zone), level, t);
    }

    /// A player's name for a save: printable, no tabs or '|' (the add-on's separators), at most 48 bytes.
    /// (logic/SavesLogic.cpp, unit-tested on the host.)
    std::string CleanLabel(std::string_view in)
    {
        return MoongladeLogic::CleanLabel(in);
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
        // the retention choice is logic/SavesLogic.cpp's RetentionDeletes (unit-tested on the host); this walks the
        // folder and deletes
        std::vector<std::string> names;
        std::error_code ec;
        for (auto const& e : fs::directory_iterator(charDir, ec))
            if (e.is_directory(ec))
                names.push_back(e.path().filename().string());
        for (std::string const& id : MoongladeLogic::RetentionDeletes(names, kind, keep))
        {
            fs::remove_all(charDir / id, ec);
            LOG_INFO("module", "[mod-moonglade] saves: rotated out {}", id);
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
        if (g_test.pauseAfterFirstFileS && IsTestAccount(m.account))
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
            // Fail closed: the core hands an empty result both for "no rows" and for a failed query, so every read
            // returns at least one row: a one-row derived table LEFT JOINed with the table. A row whose table columns
            // are all NULL is that marker (every save-set table has a NOT NULL key); no result at all is a failure,
            // and a failed read fails the whole save (never a save with a table silently missing).
            std::string sql = "SELECT 1, " + SelectList(table) + " FROM (SELECT 1 AS hs_one) AS hs LEFT JOIN `" + table
                + "` AS t ON " + Where(SAVE_SET[ti].where, snap->guid);
            size_t ncols = g_columns[table].size();
            g_queries.AddCallback(CharacterDatabase.AsyncQuery(sql).WithCallback(
                [snap, request, ti, ncols](QueryResult result)
            {
                if (!result)
                    snap->failed = true;
                else
                {
                    do
                    {
                        Field* f = result->Fetch();
                        Row row;
                        row.reserve(ncols);
                        bool any = false;
                        for (size_t i = 1; i <= ncols; ++i)
                        {
                            any = any || !f[i].IsNull();
                            row.push_back(f[i].IsNull() ? std::nullopt : std::optional<std::string>(f[i].Get<std::string>()));
                        }
                        if (any)
                            snap->tables[ti].push_back(std::move(row));
                    } while (result->NextRow());
                }
                if (--snap->pending == 0)
                    FinishSave(*request, *snap);
            }));
        }
    }


    // ------------------------------------------------------------------------------------------ reader and list

    /// A small JSON reader for the two files this module writes (manifest.json, character.json.gz): objects,
    /// arrays, strings (with \" \\ \/ \b \f \n \r \t \uXXXX escapes), numbers, true/false/null.
    struct JVal
    {
        enum Type { TNull, TBool, TNum, TStr, TArr, TObj } type = TNull;
        bool b = false;
        double n = 0;
        std::string s;
        std::vector<JVal> a;
        std::vector<std::pair<std::string, JVal>> o;

        JVal const* Get(std::string_view key) const
        {
            for (auto const& [k, v] : o)
                if (k == key)
                    return &v;
            return nullptr;
        }
        std::string Str(std::string_view key) const
        {
            JVal const* v = Get(key);
            return v && v->type == TStr ? v->s : std::string();
        }
        int64 Int(std::string_view key) const
        {
            JVal const* v = Get(key);
            return v && v->type == TNum ? int64(v->n) : 0;
        }
    };

    class JReader
    {
    public:
        explicit JReader(std::string_view src) : _s(src) { }

        bool Parse(JVal& out)
        {
            out = Value();
            Ws();
            return _ok && _i == _s.size();
        }

    private:
        std::string_view _s;
        size_t _i = 0;
        bool _ok = true;
        int _depth = 0;

        void Ws() { while (_i < _s.size() && (_s[_i] == ' ' || _s[_i] == '\n' || _s[_i] == '\r' || _s[_i] == '\t')) ++_i; }
        bool Eat(char c) { Ws(); if (_i < _s.size() && _s[_i] == c) { ++_i; return true; } return false; }

        JVal Value()
        {
            JVal v;
            Ws();
            if (!_ok || _i >= _s.size() || ++_depth > 64)
            {
                _ok = false;
                return v;
            }
            char c = _s[_i];
            if (c == '{')
            {
                ++_i;
                v.type = JVal::TObj;
                if (!Eat('}'))
                {
                    do
                    {
                        Ws();
                        JVal k = String();
                        if (!_ok || !Eat(':'))
                        {
                            _ok = false;
                            break;
                        }
                        v.o.emplace_back(std::move(k.s), Value());
                    } while (_ok && Eat(','));
                    if (!Eat('}'))
                        _ok = false;
                }
            }
            else if (c == '[')
            {
                ++_i;
                v.type = JVal::TArr;
                if (!Eat(']'))
                {
                    do
                        v.a.push_back(Value());
                    while (_ok && Eat(','));
                    if (!Eat(']'))
                        _ok = false;
                }
            }
            else if (c == '"')
                v = String();
            else if (_s.substr(_i, 4) == "null")
                _i += 4;
            else if (_s.substr(_i, 4) == "true")
            {
                _i += 4;
                v.type = JVal::TBool;
                v.b = true;
            }
            else if (_s.substr(_i, 5) == "false")
            {
                _i += 5;
                v.type = JVal::TBool;
            }
            else
            {
                size_t start = _i;
                while (_i < _s.size() && (std::isdigit(static_cast<unsigned char>(_s[_i])) || _s[_i] == '-'
                       || _s[_i] == '+' || _s[_i] == '.' || _s[_i] == 'e' || _s[_i] == 'E'))
                    ++_i;
                if (start == _i)
                    _ok = false;
                v.type = JVal::TNum;
                v.n = std::strtod(std::string(_s.substr(start, _i - start)).c_str(), nullptr);
            }
            --_depth;
            return v;
        }

        JVal String()
        {
            JVal v;
            v.type = JVal::TStr;
            if (_i >= _s.size() || _s[_i] != '"')
            {
                _ok = false;
                return v;
            }
            ++_i;
            while (_i < _s.size() && _s[_i] != '"')
            {
                char c = _s[_i++];
                if (c != '\\')
                {
                    v.s += c;
                    continue;
                }
                if (_i >= _s.size())
                    break;
                char e = _s[_i++];
                switch (e)
                {
                    case '"': v.s += '"'; break;
                    case '\\': v.s += '\\'; break;
                    case '/': v.s += '/'; break;
                    case 'b': v.s += '\b'; break;
                    case 'f': v.s += '\f'; break;
                    case 'n': v.s += '\n'; break;
                    case 'r': v.s += '\r'; break;
                    case 't': v.s += '\t'; break;
                    case 'u':
                    {
                        if (_i + 4 > _s.size())
                        {
                            _ok = false;
                            return v;
                        }
                        uint32 cp = uint32(std::strtoul(std::string(_s.substr(_i, 4)).c_str(), nullptr, 16));
                        _i += 4;
                        if (cp < 0x80)
                            v.s += char(cp);
                        else if (cp < 0x800)
                        {
                            v.s += char(0xC0 | (cp >> 6));
                            v.s += char(0x80 | (cp & 0x3F));
                        }
                        else
                        {
                            v.s += char(0xE0 | (cp >> 12));
                            v.s += char(0x80 | ((cp >> 6) & 0x3F));
                            v.s += char(0x80 | (cp & 0x3F));
                        }
                        break;
                    }
                    default: _ok = false; return v;
                }
            }
            if (_i >= _s.size())
                _ok = false;
            else
                ++_i;
            return v;
        }
    };

    bool ReadManifest(fs::path const& dir, Manifest& m)
    {
        std::string text;
        JVal j;
        if (!ReadFile(dir / "manifest.json", text) || !JReader(text).Parse(j) || j.type != JVal::TObj
            || j.Int("format") != 1)
            return false;
        m.id = j.Str("id");
        m.kind = j.Str("kind");
        m.label = j.Str("label");
        m.created = j.Int("created");
        m.createdUtc = j.Str("created_utc");
        m.createdLocal = j.Str("created_local");
        m.account = j.Str("account");
        m.accountId = uint32(j.Int("account_id"));
        m.guid = uint32(j.Int("character"));
        m.name = j.Str("name");
        m.level = uint32(j.Int("level"));
        m.cls = uint32(j.Int("class"));
        m.race = uint32(j.Int("race"));
        m.zone = uint32(j.Int("zone"));
        m.zoneName = j.Str("zone_name");
        m.map = uint32(j.Int("map"));
        m.playTime = uint32(j.Int("play_time"));
        m.money = uint32(j.Int("money"));
        m.coreSha = j.Str("core_sha");
        m.schema = j.Str("schema");
        m.sha256 = j.Str("sha256");
        return !m.id.empty() && m.id == dir.filename().string();
    }

    /// A character's saves, newest first (finished folders only: .tmp-* never counts).
    std::vector<Manifest> ListSaves(std::string const& account, uint32 guid)
    {
        std::vector<Manifest> out;
        std::error_code ec;
        fs::path charDir = CharDir(account, guid);
        if (!fs::is_directory(charDir, ec))
            return out;
        for (auto const& e : fs::directory_iterator(charDir, ec))
        {
            std::string n = e.path().filename().string();
            Manifest m;
            if (n.rfind(".tmp-", 0) != 0 && e.is_directory(ec) && ReadManifest(e.path(), m))
                out.push_back(std::move(m));
        }
        std::sort(out.begin(), out.end(), [](Manifest const& a, Manifest const& b)
        {
            return a.created != b.created ? a.created > b.created : a.id > b.id;
        });
        return out;
    }

    /// The list's generation: 4 hex digits of the sha256 of its ids in order. A request naming another generation
    /// is refused ("the list changed").
    std::string ListGen(std::vector<Manifest> const& list)
    {
        std::string ids;
        for (Manifest const& m : list)
            ids += m.id + "\n";
        return Sha256Hex(ids).substr(0, 4);
    }

    // ------------------------------------------------------------------------------------------ loader

    /// One load's progress, for `.moonglade save status <job>` (the launcher polls it).
    struct Job
    {
        std::string state = "pending";            // pending | done | refused | failed
        std::string why;
        std::string label;
        time_t started = 0;
    };

    std::unordered_map<uint32, Job> g_jobs;
    uint32 g_nextJob = 1;
    std::unordered_map<uint32, uint32> g_loading;  // character guid -> job (one load per character at a time)

    struct ParsedSave
    {
        Manifest m;
        std::vector<std::vector<Row>> tables;    // SAVE_SET order, columns in the running schema's order
    };

    /// Read and check a save: the file's sha256 equals the manifest's, the format and schema are ours, and every
    /// table's columns are the running database's. "" or the refusal reason.
    std::string ReadSave(std::string const& account, uint32 guid, std::string const& id, ParsedSave& out)
    {
        fs::path dir = CharDir(account, guid) / id;
        std::error_code ec;
        if (id.empty() || id.find('/') != std::string::npos || id[0] == '.' || !fs::is_directory(dir, ec))
            return "That save doesn't exist.";
        if (!ReadManifest(dir, out.m))
            return "That save is damaged.";
        if (out.m.account != account)
            return "That save belongs to another account.";
        if (out.m.guid != guid)
            return "That save belongs to another character.";
        std::string gz;
        if (!ReadFile(dir / "character.json.gz", gz) || Sha256Hex(gz) != out.m.sha256)
            return "That save is damaged.";
        if (out.m.schema != g_schemaFingerprint)
            return "This save was made by a different version of Moonglade.";
        bool ok = false;
        std::string text = Gunzip(gz, ok);
        JVal j;
        if (!ok || !JReader(text).Parse(j) || j.Int("format") != 1)
            return "That save is damaged.";
        JVal const* schema = j.Get("schema");
        JVal const* rows = j.Get("rows");
        if (!schema || !rows)
            return "That save is damaged.";
        out.tables.assign(std::size(SAVE_SET), {});
        for (size_t ti = 0; ti < std::size(SAVE_SET); ++ti)
        {
            std::string name(SAVE_SET[ti].name);
            JVal const* cols = schema->Get(name);
            JVal const* trows = rows->Get(name);
            auto const& want = g_columns[name];
            auto const& types = g_types[name];
            if (!cols || !trows || cols->a.size() != want.size())
                return "This save was made by a different version of Moonglade.";
            for (size_t i = 0; i < want.size(); ++i)
                if (cols->a[i].s != want[i] + " " + types[i])
                    return "This save was made by a different version of Moonglade.";
            for (JVal const& r : trows->a)
            {
                if (r.a.size() != want.size())
                    return "That save is damaged.";
                Row row;
                row.reserve(r.a.size());
                for (JVal const& v : r.a)
                    row.push_back(v.type == JVal::TStr ? std::optional<std::string>(v.s) : std::nullopt);
                out.tables[ti].push_back(std::move(row));
            }
        }
        if (out.tables[0].size() != 1)
            return "That save is damaged.";
        return {};
    }

    /// The restore as one transaction (design.md 3): the removed-on-load rows and the save set's current rows go,
    /// then the saved rows come back under the same character id, with fresh item, pet and equipment-set ids from
    /// the server's own generators, the character's current name and online = 0. `failAfter` (test switch) appends
    /// a failing statement after that many statements, so the whole transaction rolls back. It must be an error the
    /// core treats as a failed statement (here ER_SUBQUERY_NO_1_ROW, a read): ER_NO_SUCH_TABLE, ER_BAD_FIELD_ERROR and
    /// ER_PARSE_ERROR abort the worldserver (MySQLConnection::_HandleMySQLErrno), so the restore's own statements
    /// only ever name the running schema's tables and columns (the schema check before a load).
    CharacterDatabaseTransaction BuildRestore(ParsedSave const& save, uint32 guid, std::string const& currentName,
        uint32 failAfter, uint32& statements)
    {
        CharacterDatabaseTransaction trans = CharacterDatabase.BeginTransaction();
        statements = 0;
        auto add = [&](std::string const& sql)
        {
            if (failAfter && statements == failAfter)
                trans->Append(std::string("SELECT 1 FROM DUAL WHERE (SELECT 1 UNION SELECT 2) = 1"));
            trans->Append(sql);
            ++statements;
        };

        // fresh ids for everything this save brings back (never an id another row may hold now)
        std::unordered_map<std::string, std::string> items, pets, sets;
        auto colIndex = [](std::string const& table, std::string_view col) -> int
        {
            auto const& cols = g_columns[table];
            for (size_t i = 0; i < cols.size(); ++i)
                if (cols[i] == col)
                    return int(i);
            return -1;
        };
        for (size_t ti = 0; ti < std::size(SAVE_SET); ++ti)
        {
            std::string name(SAVE_SET[ti].name);
            if (name == "item_instance")
                for (Row const& r : save.tables[ti])
                    items[*r[colIndex(name, "guid")]] = std::to_string(sObjectMgr->GetGenerator<HighGuid::Item>().Generate());
            else if (name == "character_pet")
                for (Row const& r : save.tables[ti])
                    pets[*r[colIndex(name, "id")]] = std::to_string(sObjectMgr->GeneratePetNumber());
            else if (name == "character_equipmentsets")
                for (Row const& r : save.tables[ti])
                    sets[*r[colIndex(name, "setguid")]] = std::to_string(sObjectMgr->GenerateEquipmentSetGuid());
        }

        for (RemovedTable const& t : REMOVED_ON_LOAD)
            add("DELETE FROM `" + std::string(t.name) + "` WHERE " + Where(t.where, guid));
        for (size_t ti = std::size(SAVE_SET); ti-- > 0; )
            add("DELETE FROM `" + std::string(SAVE_SET[ti].name) + "` WHERE " + Where(SAVE_SET[ti].where, guid));

        for (size_t ti = 0; ti < std::size(SAVE_SET); ++ti)
        {
            SaveTable const& t = SAVE_SET[ti];
            std::string name(t.name);
            auto const& cols = g_columns[name];
            auto const& types = g_types[name];
            std::string head = "INSERT INTO `" + name + "` (";
            for (size_t i = 0; i < cols.size(); ++i)
                head += (i ? ", `" : "`") + cols[i] + "`";
            head += ") VALUES ";
            std::string values;
            uint32 n = 0;
            for (Row row : save.tables[ti])
            {
                bool keep = true;
                for (RenumberColumn const& rc : t.renumber)
                {
                    int ci = colIndex(name, rc.column);
                    if (ci < 0 || !row[ci])
                        continue;
                    std::string& v = *row[ci];
                    switch (rc.kind)
                    {
                        case Renumber::Item:              // bag 0 = not in a bag; an item not in this save: drop the row
                            if (v == "0" && rc.column == "bag")
                                break;
                            if (auto it = items.find(v); it != items.end())
                                v = it->second;
                            else
                                keep = false;
                            break;
                        case Renumber::ItemInSet:
                            if (auto it = items.find(v); it != items.end())
                                v = it->second;
                            else
                                v = "0";
                            break;
                        case Renumber::ItemRaw:
                        {
                            uint64 raw = std::strtoull(v.c_str(), nullptr, 10);
                            if (raw)
                                if (auto it = items.find(std::to_string(raw & 0xFFFFFFFF)); it != items.end())
                                    v = std::to_string(ObjectGuid::Create<HighGuid::Item>(
                                        uint32(std::strtoul(it->second.c_str(), nullptr, 10))).GetRawValue());
                            break;
                        }
                        case Renumber::Pet:
                            if (auto it = pets.find(v); it != pets.end())
                                v = it->second;
                            else
                                keep = false;
                            break;
                        case Renumber::EquipmentSet:
                            if (auto it = sets.find(v); it != sets.end())
                                v = it->second;
                            else
                                keep = false;
                            break;
                        case Renumber::None:
                            break;
                    }
                }
                if (!keep)
                    continue;
                if (name == "characters")
                {
                    row[colIndex(name, "name")] = currentName;
                    row[colIndex(name, "online")] = std::string("0");
                }
                std::string tuple = "(";
                for (size_t i = 0; i < row.size(); ++i)
                    tuple += (i ? ", " : "") + SqlValue(row[i], types[i]);
                tuple += ")";
                values += (n ? ", " : "") + tuple;
                if (++n == 200)
                {
                    add(head + values);
                    values.clear();
                    n = 0;
                }
            }
            if (n)
                add(head + values);
        }
        if (failAfter && statements <= failAfter)
            trans->Append(std::string("SELECT 1 FROM DUAL WHERE (SELECT 1 UNION SELECT 2) = 1"));
        return trans;
    }

    struct LoadRequest
    {
        std::string account;
        uint32 guid = 0;
        std::string id;
        ObjectGuid inGame;            // set: the in-game path (log this player out without saving it)
        uint32 job = 0;
    };

    void FinishJob(uint32 job, uint32 guid, std::string const& state, std::string const& why)
    {
        g_loading.erase(guid);
        auto it = g_jobs.find(job);
        if (it != g_jobs.end())
        {
            it->second.state = state;
            it->second.why = why;
        }
        LOG_INFO("module", "[mod-moonglade] saves: load job {} of {}: {}{}{}", job, guid, state, why.empty() ? "" : ": ", why);
    }

    /// Checks that hold for both paths: the character exists, isn't deleted, belongs to the account; returns its
    /// current name through `name`.
    std::string CheckCharacter(std::string const& account, uint32 guid, std::string& name)
    {
        QueryResult r = CharacterDatabase.Query("SELECT account, name, deleteDate IS NOT NULL FROM characters WHERE guid = {}", guid);
        if (!r)
            return "That character doesn't exist.";
        if ((*r)[2].Get<int64>() != 0)
            return "That character was deleted.";
        if (AccountName((*r)[0].Get<uint32>()) != account)
            return "That save belongs to another account.";
        name = (*r)[1].Get<std::string>();
        return {};
    }

    /// The load, in order (design.md 3): checks; the "before loading" safety save of the current state; then (in
    /// game) the instant logout without the character save; then the restore transaction, queued in the same world
    /// tick on the FIFO queue behind every earlier write of the character.
    void StartLoad(LoadRequest req)
    {
        auto refuse = [&](std::string const& why)
        {
            FinishJob(req.job, req.guid, "refused", why);
            if (req.inGame)
                ReplyTo(req.inGame, "NO\tLOAD\t" + why);
        };
        if (!g_cfg.enabled)
            return refuse("Loading is turned off.");
        if (g_loading.count(req.guid))
            return refuse("A load is already running for this character.");
        std::string currentName;
        if (std::string why = CheckCharacter(req.account, req.guid, currentName); !why.empty())
            return refuse(why);
        Player* online = ObjectAccessor::FindPlayer(ObjectGuid::Create<HighGuid::Player>(req.guid));
        if (!req.inGame && (online || ObjectAccessor::FindConnectedPlayer(ObjectGuid::Create<HighGuid::Player>(req.guid))))
            return refuse("That character is in the world.");
        if (req.inGame && (!online || online->GetGUID() != req.inGame || online->GetSession()->IsBot()))
            return refuse("That character isn't yours to load here.");
        auto save = std::make_shared<ParsedSave>();
        if (std::string why = ReadSave(req.account, req.guid, req.id, *save); !why.empty())
            return refuse(why);
        g_loading[req.guid] = req.job;
        g_jobs[req.job].label = save->m.label;

        SaveRequest safety;
        safety.guid = req.guid;
        safety.kind = "before-load";
        safety.online = bool(req.inGame);
        safety.done = [req, save, currentName](bool ok, std::string const& why, Manifest const&)
        {
            if (!ok)
            {
                FinishJob(req.job, req.guid, "failed", "Couldn't make the safety save first (" + why + ").");
                if (req.inGame)
                    ReplyTo(req.inGame, "NO\tLOAD\tCouldn't make the safety save first.");
                return;
            }
            if (req.inGame)
            {
                Player* player = ObjectAccessor::FindConnectedPlayer(req.inGame);
                if (!player || !player->IsInWorld())
                {
                    FinishJob(req.job, req.guid, "failed", "The character left the world before the load.");
                    return;
                }
                Reply(player, "OK\tLOAD\t" + save->m.label);
                WorldPackets::Character::LogoutResponse resp;
                resp.LogoutResult = 0;
                resp.Instant = true;
                player->GetSession()->SendPacket(resp.Write());
                player->GetSession()->LogoutPlayer(false);     // no character save after this point (design.md 3)
            }
            else if (ObjectAccessor::FindPlayer(ObjectGuid::Create<HighGuid::Player>(req.guid)))
            {
                FinishJob(req.job, req.guid, "refused", "That character entered the world.");
                return;
            }
            uint32 failAfter = (g_test.failAfter && IsTestAccount(req.account)) ? g_test.failAfter : 0;
            uint32 statements = 0;
            CharacterDatabaseTransaction trans = BuildRestore(*save, req.guid, currentName, failAfter, statements);
            uint32 guid = req.guid, job = req.job;
            g_transactions.AddCallback(CharacterDatabase.AsyncCommitTransaction(trans)).AfterComplete(
                [guid, job, statements](bool success)
            {
                if (success)
                    sCharacterCache->RefreshCacheEntry(guid);
                FinishJob(job, guid, success ? "done" : "failed", success ? "" : "The load didn't go through; nothing changed.");
                LOG_INFO("module", "[mod-moonglade] saves: restore of {} ({} statements): {}", guid, statements,
                    success ? "committed" : "rolled back");
            });
        };
        StartSave(std::move(safety));
    }

    uint32 NewJob()
    {
        uint32 id = g_nextJob++;
        Job j;
        j.started = time(nullptr);
        g_jobs[id] = j;
        if (g_jobs.size() > 256)                       // keep the table small: drop the oldest finished
            for (auto it = g_jobs.begin(); it != g_jobs.end(); ++it)
                if (it->second.state != "pending" && it->first + 200 < id)
                {
                    g_jobs.erase(it);
                    break;
                }
        return id;
    }

    /// Why a player-requested save is refused now ("" = allowed). Bots never get here.
    std::string RefuseSave(Player* player)
    {
        MoongladeLogic::SaveState st;
        st.inCombat = player->IsInCombat();
        st.alive = player->IsAlive();
        st.ghost = player->HasPlayerFlag(PLAYER_FLAGS_GHOST);
        st.falling = player->m_movementInfo.HasMovementFlag(MovementFlags(MOVEMENTFLAG_FALLING | MOVEMENTFLAG_FALLING_FAR));
        st.inBattlegroundOrArena = player->InBattleground() || player->InArena();
        st.teleporting = player->IsBeingTeleported();
        return MoongladeLogic::SaveRefusal(st);
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
        s.why = MoongladeLogic::DisabledReason(enable, s.workerThreads, s.dir.empty());
        if (s.why.empty())
        {
            if (!s.dir.is_absolute())
                s.why = "Moonglade.Saves.Dir is not an absolute path";
            else if (fs::create_directories(s.dir, ec), ec)
                s.why = "Moonglade.Saves.Dir can't be created (" + ec.message() + ")";
        }
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
    struct AddonRequest
    {
        ObjectGuid guid;
        std::string verb;
        std::string arg;
    };

    std::vector<AddonRequest> g_addonRequests;      // world thread only (CMSG_MESSAGECHAT runs in UpdateSessions)

    /// "LIST": one line per save, newest first ("E\t<gen>\t<n>\t<kind>\t<when>\t<level>\t<zone>\t<label>", at
    /// most 255 bytes with the prefix; never the save's id or path), then "END\t<gen>\t<count>".
    void SendList(Player* player)
    {
        std::vector<Manifest> list = ListSaves(AccountName(player->GetSession()->GetAccountId()),
            player->GetGUID().GetCounter());
        std::string gen = ListGen(list);
        uint32 n = 0;
        for (Manifest const& m : list)
        {
            ++n;
            Reply(player, "E\t" + gen + "\t" + std::to_string(n) + "\t" + m.kind + "\t"
                + FormatTime(time_t(m.created), "%a %d %b %H:%M", false) + "\t" + std::to_string(m.level) + "\t"
                + m.zoneName + "\t" + m.label);
        }
        Reply(player, "END\t" + gen + "\t" + std::to_string(n));
    }

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

    struct AutoState
    {
        uint32 loginMs = 0;
        uint32 lastCheckMs = 0;
        bool initialised = false;
        bool resting = false;
        bool flying = false;
    };

    struct AutoTrigger
    {
        ObjectGuid guid;
        std::string moment;
        uint32 dueMs;
    };

    std::mutex g_autoLock;                                    // map threads (OnPlayerUpdate) and the world thread
    std::unordered_map<uint32, AutoState> g_auto;
    std::vector<AutoTrigger> g_autoQueue;
    std::unordered_map<uint32, time_t> g_lastAuto;           // world thread: the last autosave per character

    void QueueAuto(ObjectGuid guid, std::string moment)      // caller holds g_autoLock
    {
        LOG_INFO("module", "[mod-moonglade] saves: autosave trigger ({}) of {}", moment, guid.GetCounter());
        if (g_autoQueue.size() < 256)
            g_autoQueue.push_back({ guid, std::move(moment), getMSTime() + 1000 });
    }

    void ProcessAutosaves()
    {
        std::vector<AutoTrigger> due;
        {
            std::lock_guard<std::mutex> lock(g_autoLock);
            uint32 now = getMSTime();
            auto keep = std::remove_if(g_autoQueue.begin(), g_autoQueue.end(), [&](AutoTrigger const& t)
            {
                if (int32(now - t.dueMs) < 0)
                    return false;
                due.push_back(t);
                return true;
            });
            g_autoQueue.erase(keep, g_autoQueue.end());
        }
        for (AutoTrigger const& t : due)
        {
            Player* player = ObjectAccessor::FindConnectedPlayer(t.guid);
            uint32 g = t.guid.GetCounter();
            if (!player || !player->IsInWorld() || player->GetSession()->IsBot())
            {
                LOG_INFO("module", "[mod-moonglade] saves: autosave ({}) of {} skipped: not in the world", t.moment, g);
                continue;
            }
            time_t now = time(nullptr);
            auto last = g_lastAuto.find(g);
            if (last != g_lastAuto.end() && now - last->second < time_t(g_cfg.autoMinIntervalS))
            {
                LOG_INFO("module", "[mod-moonglade] saves: autosave ({}) of {} skipped: inside the interval", t.moment, g);
                continue;
            }
            if (std::string why = RefuseSave(player); !why.empty())
            {
                LOG_INFO("module", "[mod-moonglade] saves: autosave ({}) of {} skipped: {}", t.moment, g, why);
                continue;
            }
            g_lastAuto[g] = now;
            SaveRequest req;
            req.guid = g;
            req.kind = "auto";
            req.moment = t.moment;
            req.online = true;
            StartSave(std::move(req));
        }
    }

    /// "DELETE <gen> <index>": manual saves only (the others rotate by themselves).
    void DeleteSave(Player* player, std::string const& arg)
    {
        std::string account = AccountName(player->GetSession()->GetAccountId());
        uint32 guid = player->GetGUID().GetCounter();
        std::vector<Manifest> list = ListSaves(account, guid);
        size_t sp = arg.find(' ');
        std::string gen = sp == std::string::npos ? arg : arg.substr(0, sp);
        uint32 idx = sp == std::string::npos ? 0 : uint32(std::strtoul(arg.c_str() + sp + 1, nullptr, 10));
        if (gen != ListGen(list))
            return Reply(player, "NO\tDELETE\tThe list of saves changed. Open Saves again.");
        if (idx < 1 || idx > list.size())
            return Reply(player, "NO\tDELETE\tThat save doesn't exist.");
        Manifest const& m = list[idx - 1];
        if (m.kind != "manual")
            return Reply(player, "NO\tDELETE\tOnly your own saves can be deleted.");
        std::error_code ec;
        fs::remove_all(CharDir(account, guid) / m.id, ec);
        if (ec)
            return Reply(player, "NO\tDELETE\tCouldn't delete that save.");
        LOG_INFO("module", "[mod-moonglade] saves: deleted {} of {} (player's request)", m.id, guid);
        Reply(player, "OK\tDELETE\t" + m.label);
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
        else if (r.verb == "LIST")
            SendList(player);
        else if (r.verb == "DELETE")
            DeleteSave(player, r.arg);
        else if (r.verb == "LOAD" || r.verb == "QLOAD")
        {
            std::string account = AccountName(player->GetSession()->GetAccountId());
            uint32 guid = player->GetGUID().GetCounter();
            std::vector<Manifest> list = ListSaves(account, guid);
            Manifest const* pick = nullptr;
            if (r.verb == "QLOAD")
            {
                for (Manifest const& m : list)
                    if (m.kind == "quick")
                    {
                        pick = &m;
                        break;
                    }
                if (!pick)
                {
                    Reply(player, "NO\tQLOAD\tThere is no quicksave yet.");
                    return;
                }
            }
            else
            {
                // "LOAD <generation> <index>" (index 1 = newest, as LIST numbered them)
                std::string gen;
                uint32 idx = 0;
                size_t sp = r.arg.find(' ');
                if (sp != std::string::npos)
                {
                    gen = r.arg.substr(0, sp);
                    idx = uint32(std::strtoul(r.arg.c_str() + sp + 1, nullptr, 10));
                }
                if (gen != ListGen(list))
                {
                    Reply(player, "NO\tLOAD\tThe list of saves changed. Open Saves again.");
                    return;
                }
                if (idx < 1 || idx > list.size())
                {
                    Reply(player, "NO\tLOAD\tThat save doesn't exist.");
                    return;
                }
                pick = &list[idx - 1];
            }
            LoadRequest lr;
            lr.account = account;
            lr.guid = guid;
            lr.id = pick->id;
            lr.inGame = player->GetGUID();
            lr.job = NewJob();
            StartLoad(std::move(lr));
        }
        else
            Reply(player, "NO\t" + r.verb + "\tUnknown request.");
    }
}

// Autosave triggers (design.md 4): the rising edge of the resting flag (an inn or a city) and of being on a flight,
// checked at most once a second per character in OnPlayerUpdate (map threads), and arriving on a new map
// (OnPlayerMapChanged, not the map the character logs in to). A trigger is queued with a short delay (the arrival
// finishes first) and handled on the world thread: skipped, not queued again, inside AutoMinIntervalS, in combat or
// whenever a save would be refused. Bots never trigger.
class MoongladeSavesPlayerScript : public PlayerScript
{
public:
    MoongladeSavesPlayerScript() : PlayerScript("MoongladeSavesPlayerScript",
        { PLAYERHOOK_ON_UPDATE, PLAYERHOOK_ON_LOGIN, PLAYERHOOK_ON_LOGOUT }) { }

    void OnPlayerLogin(Player* player) override
    {
        if (player->GetSession()->IsBot())
            return;
        std::lock_guard<std::mutex> lock(g_autoLock);
        AutoState& st = g_auto[player->GetGUID().GetCounter()];
        st = AutoState{};
        st.loginMs = getMSTime();
    }

    void OnPlayerLogout(Player* player) override
    {
        std::lock_guard<std::mutex> lock(g_autoLock);
        auto it = g_auto.find(player->GetGUID().GetCounter());
        if (it != g_auto.end())
        {
            it->second.loginMs = 0;
            it->second.initialised = false;
        }
    }

    void OnPlayerUpdate(Player* player, uint32 /*diff*/) override
    {
        if (!g_cfg.enabled || player->GetSession()->IsBot())
            return;
        uint32 now = getMSTime();
        std::lock_guard<std::mutex> lock(g_autoLock);
        auto it = g_auto.find(player->GetGUID().GetCounter());
        if (it == g_auto.end() || !it->second.loginMs)
            return;
        AutoState& st = it->second;
        if (st.initialised && getMSTimeDiff(st.lastCheckMs, now) < 1000)
            return;
        st.lastCheckMs = now;
        bool resting = player->HasPlayerFlag(PLAYER_FLAGS_RESTING);
        bool flying = player->IsInFlight();
        if (st.initialised)
        {
            if (resting && !st.resting)
                QueueAuto(player->GetGUID(), "Rested");
            if (flying && !st.flying)
                QueueAuto(player->GetGUID(), "Took a flight");
        }
        st.resting = resting;
        st.flying = flying;
        st.initialised = true;
    }

    void OnPlayerMapChanged(Player* player) override
    {
        if (!g_cfg.enabled || player->GetSession()->IsBot())
            return;
        std::lock_guard<std::mutex> lock(g_autoLock);
        auto it = g_auto.find(player->GetGUID().GetCounter());
        // the map the character logs in to isn't an arrival (OnPlayerMapChanged runs from Map::AddPlayerToMap)
        if (it == g_auto.end() || !it->second.loginMs || getMSTimeDiff(it->second.loginMs, getMSTime()) < 5000)
            return;
        QueueAuto(player->GetGUID(), "Arrived");
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
        ProcessAutosaves();
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
            { "load",      HandleLoad,      SEC_GAMEMASTER, Console::Yes },
            { "list",      HandleList,      SEC_GAMEMASTER, Console::Yes },
            { "interval",  HandleInterval,  SEC_GAMEMASTER, Console::Yes },
            { "who",       HandleWho,       SEC_GAMEMASTER, Console::Yes },
            { "trigger",   HandleTrigger,   SEC_GAMEMASTER, Console::Yes },
            { "test",      HandleTest,      SEC_GAMEMASTER, Console::Yes },
        };
        static ChatCommandTable moongladeTable = { { "save", saveTable } };
        static ChatCommandTable commandTable = { { "moonglade", moongladeTable } };
        return commandTable;
    }

    /// `.moonglade save status`: enabled or the reason it isn't, the store, the schema fingerprint and the settings.
    static bool HandleStatus(ChatHandler* handler, Optional<uint32> job)
    {
        if (job)
            return HandleJobStatus(handler, *job);
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

    /// `.moonglade save load <guid> <save id>` (the launcher, for a load at the character screen): queues the load
    /// and answers "job <n>"; `.moonglade save status <n>` reports it.
    static bool HandleLoad(ChatHandler* handler, uint32 guid, std::string id)
    {
        QueryResult r = CharacterDatabase.Query("SELECT account FROM characters WHERE guid = {}", guid);
        LoadRequest lr;
        lr.account = r ? AccountName((*r)[0].Get<uint32>()) : "";
        lr.guid = guid;
        lr.id = id;
        lr.job = NewJob();
        uint32 job = lr.job;
        StartLoad(std::move(lr));
        handler->PSendSysMessage("Moonglade save: job {}", job);
        return true;
    }

    static bool HandleJobStatus(ChatHandler* handler, uint32 job)
    {
        auto it = g_jobs.find(job);
        if (it == g_jobs.end())
            handler->PSendSysMessage("Moonglade save: job {} unknown", job);
        else
            handler->PSendSysMessage("Moonglade save: job {} {}{}{}", job, it->second.state,
                it->second.why.empty() ? "" : ": ", it->second.why);
        return true;
    }

    /// `.moonglade save list <guid>`: one line per save, newest first: "<id> <kind> <level> <label>".
    static bool HandleList(ChatHandler* handler, uint32 guid)
    {
        QueryResult r = CharacterDatabase.Query("SELECT account FROM characters WHERE guid = {}", guid);
        if (!r)
        {
            handler->SendSysMessage("Moonglade save: no such character");
            return true;
        }
        std::vector<Manifest> list = ListSaves(AccountName((*r)[0].Get<uint32>()), guid);
        for (Manifest const& m : list)
            handler->PSendSysMessage("Moonglade save: {} {} {} {}", m.id, m.kind, m.level, m.label);
        handler->PSendSysMessage("Moonglade save: {} save(s), generation {}", list.size(), ListGen(list));
        return true;
    }

    /// `.moonglade save trigger <guid> <moment>` (console only, harness): an autosave trigger for an online character,
    /// queued exactly as the hooks queue theirs (saves.refused_in_combat fires one during a fight; saves.bots_skip one
    /// for a companion), so it is skipped or saved by the same rules.
    static bool HandleTrigger(ChatHandler* handler, uint32 guid, std::string moment)
    {
        if (handler->GetSession())
        {
            handler->SendSysMessage("Moonglade save: the trigger switch is console-only");
            return true;
        }
        Player* p = ObjectAccessor::FindConnectedPlayer(ObjectGuid::Create<HighGuid::Player>(guid));
        if (!p)
        {
            handler->PSendSysMessage("Moonglade save: trigger: {} not in the world", guid);
            return true;
        }
        if (p->GetSession()->IsBot())
        {
            LOG_INFO("module", "[mod-moonglade] saves: autosave trigger ({}) of {} ignored: AI players never save", moment, guid);
            handler->PSendSysMessage("Moonglade save: trigger ({}) of {} ignored: AI players never save", moment, guid);
            return true;
        }
        {
            std::lock_guard<std::mutex> lock(g_autoLock);
            QueueAuto(p->GetGUID(), moment);
        }
        handler->PSendSysMessage("Moonglade save: trigger ({}) of {} queued", moment, guid);
        return true;
    }

    /// `.moonglade save who <guid>` (harness read-out): what the server holds in memory for an online character:
    /// level, money, position and quest statuses ("<quest>:<status>").
    static bool HandleWho(ChatHandler* handler, uint32 guid)
    {
        Player* p = ObjectAccessor::FindConnectedPlayer(ObjectGuid::Create<HighGuid::Player>(guid));
        if (!p || !p->IsInWorld())
        {
            handler->PSendSysMessage("Moonglade save who: {} not in the world", guid);
            return true;
        }
        std::string quests;
        for (auto const& [qid, st] : p->getQuestStatusMap())
            quests += (quests.empty() ? "" : ",") + std::to_string(qid) + ":" + std::to_string(uint32(st.Status));
        handler->PSendSysMessage("Moonglade save who: {} level {} money {} map {} x {:.3f} y {:.3f} z {:.3f} combat {} "
            "dead {} bot {} resting {} flying {} quests {}", guid, p->GetLevel(), p->GetMoney(), p->GetMapId(),
            p->GetPositionX(), p->GetPositionY(), p->GetPositionZ(), p->IsInCombat() ? 1 : 0, p->IsAlive() ? 0 : 1,
            p->GetSession()->IsBot() ? 1 : 0, p->HasPlayerFlag(PLAYER_FLAGS_RESTING) ? 1 : 0, p->IsInFlight() ? 1 : 0,
            quests.empty() ? "-" : quests);
        return true;
    }

    /// `.moonglade save interval <s>` (console only): the autosave interval until the next restart or config
    /// reload (saves.autosave_triggers uses a short one).
    static bool HandleInterval(ChatHandler* handler, uint32 seconds)
    {
        if (handler->GetSession())
        {
            handler->SendSysMessage("Moonglade save: the interval switch is console-only");
            return true;
        }
        g_cfg.autoMinIntervalS = seconds;
        g_lastAuto.clear();
        handler->PSendSysMessage("Moonglade save: autosave interval {} s", seconds);
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
        handler->PSendSysMessage("Moonglade save: test failafter {} pause {} (test accounts HSTEST, HSSAVES only)",
            g_test.failAfter, g_test.pauseAfterFirstFileS);
        return true;
    }
};

void AddMoongladeSavesScripts()
{
    // "MGSAVE" add-on whispers (the shared parser, MoongladeAddon.h): handled on the next world tick (a load logs the
    // character out, which must not happen inside the chat handler). Bots never save (design.md 4, "Who may save").
    MoongladeAddon::Register("MGSAVE", [](Player* player, std::string_view text)
    {
        if (player->GetSession()->IsBot())
            return;
        size_t sp = text.find(' ');
        AddonRequest r;
        r.guid = player->GetGUID();
        r.verb = std::string(text.substr(0, sp));
        r.arg = sp == std::string_view::npos ? std::string() : std::string(text.substr(sp + 1));
        if (g_addonRequests.size() < 64)
            g_addonRequests.push_back(std::move(r));
    });
    new MoongladeSavesPlayerScript();
    new MoongladeSavesWorldScript();
    new MoongladeSavesCommandScript();
}
