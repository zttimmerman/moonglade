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
#include "CryptoHash.h"
#include "DatabaseEnv.h"
#include "Log.h"
#include "ObjectAccessor.h"
#include "Player.h"
#include "ScriptMgr.h"
#include "Util.h"
#include "WorldSession.h"

#include <filesystem>
#include <string>
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
        g_schemaFingerprint = ByteArrayToHexStr(Acore::Crypto::SHA256::GetDigestOf(fp));
        return true;
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

// ---------------------------------------------------------------------------------------------- PROTOTYPE (1.1)
namespace
{
    constexpr std::string_view SAVE_PREFIX = "MGSAVE\t";

    struct ProtoSnap
    {
        uint8 level = 0;
        uint32 xp = 0;
        uint32 money = 0;
        uint32 map = 0;
        uint32 zone = 0;
        float x = 0, y = 0, z = 0, o = 0;
    };

    std::unordered_map<uint32, ProtoSnap> g_snaps;     // character guid counter -> snapshot (world thread only)
    std::vector<ObjectGuid> g_pendingLoads;             // world thread only

    void ProtoLoad(ObjectGuid guid)
    {
        Player* player = ObjectAccessor::FindConnectedPlayer(guid);
        auto it = g_snaps.find(guid.GetCounter());
        if (!player || it == g_snaps.end())
            return;
        WorldSession* session = player->GetSession();
        WorldPackets::Character::LogoutResponse resp;
        resp.LogoutResult = 0;
        resp.Instant = true;
        session->SendPacket(resp.Write());
        session->LogoutPlayer(false);

        ProtoSnap const& s = it->second;
        CharacterDatabaseTransaction trans = CharacterDatabase.BeginTransaction();
        trans->Append("UPDATE characters SET level = {}, xp = {}, money = {}, map = {}, zone = {}, position_x = {}, "
            "position_y = {}, position_z = {}, orientation = {}, online = 0 WHERE guid = {}",
            s.level, s.xp, s.money, s.map, s.zone, s.x, s.y, s.z, s.o, guid.GetCounter());
        CharacterDatabase.CommitTransaction(trans);
    }
}

class MoongladeSavesPlayerScript : public PlayerScript
{
public:
    MoongladeSavesPlayerScript() : PlayerScript("MoongladeSavesPlayerScript", { PLAYERHOOK_CAN_PLAYER_USE_PRIVATE_CHAT }) { }

    bool OnPlayerCanUseChat(Player* player, uint32 type, uint32 language, std::string& msg, Player* receiver) override
    {
        if (type != CHAT_MSG_WHISPER || language != LANG_ADDON || receiver != player
            || std::string_view(msg).substr(0, SAVE_PREFIX.size()) != SAVE_PREFIX)
            return true;
        std::string_view arg = std::string_view(msg).substr(SAVE_PREFIX.size());
        if (arg == "LOAD")
            g_pendingLoads.push_back(player->GetGUID());
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
        if (g_pendingLoads.empty())
            return;
        std::vector<ObjectGuid> loads;
        loads.swap(g_pendingLoads);
        for (ObjectGuid const& g : loads)
            ProtoLoad(g);
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
            { "protosnap", HandleProtoSnap, SEC_GAMEMASTER, Console::Yes },
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

    static bool HandleProtoSnap(ChatHandler* handler, std::string name)
    {
        Player* player = ObjectAccessor::FindPlayerByName(name, false);
        if (!player)
        {
            handler->SendSysMessage("Moonglade save: no such online character");
            return true;
        }
        ProtoSnap s;
        s.level = player->GetLevel();
        s.xp = player->GetUInt32Value(PLAYER_XP);
        s.money = player->GetMoney();
        s.map = player->GetMapId();
        s.zone = player->GetZoneId();
        s.x = player->GetPositionX();
        s.y = player->GetPositionY();
        s.z = player->GetPositionZ();
        s.o = player->GetOrientation();
        g_snaps[player->GetGUID().GetCounter()] = s;
        handler->PSendSysMessage("Moonglade save: protosnap level {} xp {} money {} map {} x {:.3f} y {:.3f} z {:.3f}",
            s.level, s.xp, s.money, s.map, s.x, s.y, s.z);
        return true;
    }
};

void AddMoongladeSavesScripts()
{
    new MoongladeSavesPlayerScript();
    new MoongladeSavesWorldScript();
    new MoongladeSavesCommandScript();
}
