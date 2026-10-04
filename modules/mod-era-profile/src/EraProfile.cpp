/*
 * mod-era-profile: Hearthstone "vanilla mode" (openspec/changes/m3-vanilla-mode).
 *
 * One switch, EraProfile.Enable (default 1), read once at server start. Two mechanisms (design.md "Hook research"):
 *
 *  1. Startup apply/revert, for values AzerothCore caches from world tables with no hook in between (e.g. the XP
 *     table, ObjectMgr::LoadPlayerInfo). In OnLoadCustomDatabaseTable, which World::SetInitialWorldSettings calls
 *     before any ObjectMgr load (World.cpp:379 vs LoadItemTemplates :528, LoadCreatureTemplates :540, LoadQuests :612,
 *     LoadPlayerInfo :699, LoadTrainers :784), the module
 *       a. captures the stock value of every in-scope row into era_baseline_<table> (INSERT IGNORE: the first capture
 *          wins; a row whose live value already equals the vanilla value is not captured and is reported), then
 *       b. sets every captured row to the vanilla value (mode 1, rows present in the era_<table> side table) or to its
 *          baseline (mode 0, or rows no longer in the side table) in one UPDATE, then
 *       c. verifies that no captured row differs from what the mode wants.
 *     Each step is idempotent and the UPDATE is a single statement, so a crash mid-switch heals on the next start.
 *  2. Runtime hooks where AzerothCore offers one: OnPlayerQuestComputeXP for quest XP (no world row changes).
 *
 * The values come only from the generated side tables (tools/era-diff/era_diff.py --emit-sql, CLAUDE.md §7); this
 * file holds no game data. The quest XP formula reproduces VMaNGOS Quest::XPValue (docs/gate1-era-profile-memo.md,
 * "RewXP semantics (verified)").
 */

#include "Config.h"
#include "DatabaseEnv.h"
#include "Log.h"
#include "Player.h"
#include "QuestDef.h"
#include "ScriptMgr.h"
#include "SpellAuraDefines.h"

#include <cmath>
#include <string>
#include <unordered_map>
#include <vector>

namespace
{
    char const* const OPT_ENABLE = "EraProfile.Enable";

    // An AC world table the module overwrites in place at startup (mechanism 1). Value columns of the side table are
    // NULL where a row keeps its stock value for that column. Keep in sync with tools/era-diff BASELINE_COLS.
    struct ApplySpec
    {
        std::string target;                // live AC table
        std::string side;                  // generated side table with the vanilla values
        std::vector<std::string> keys;
        std::vector<std::string> cols;
    };

    // m3 task 2.2: the XP curve. Tasks 2.3/2.4 add creature_template, item_template and trainer_spell here.
    std::vector<ApplySpec> const APPLY_SPECS = {
        { "player_xp_for_level", "era_player_xp_for_level", { "Level" }, { "Experience" } },
    };

    struct QuestXpRow
    {
        int32 questLevel;
        uint32 rewXp;
    };

    bool g_enabled = true;          // latched at startup
    bool g_active = false;          // vanilla mode fully in effect (all applies verified, quest table loaded)
    std::unordered_map<uint32, QuestXpRow> g_questXp;

    std::string Quoted(std::string const& n) { return "`" + n + "`"; }

    std::string Join(std::vector<std::string> const& parts, std::string const& sep)
    {
        std::string out;
        for (size_t i = 0; i < parts.size(); ++i)
            out += (i ? sep : "") + parts[i];
        return out;
    }

    std::string OnKeys(ApplySpec const& s, char const* a, char const* b)
    {
        std::vector<std::string> p;
        for (auto const& k : s.keys)
            p.push_back(std::string(a) + "." + Quoted(k) + " = " + b + "." + Quoted(k));
        return Join(p, " AND ");
    }

    // value the mode wants for column c of a captured row (aliases: v = side table, b = baseline)
    std::string Wanted(std::string const& c, bool enabled)
    {
        return std::string("IF(") + (enabled ? "1" : "0") + " AND v." + Quoted(c) + " IS NOT NULL, v." + Quoted(c) +
               ", b." + Quoted(c) + ")";
    }

    bool TableExists(std::string const& name)
    {
        QueryResult r = WorldDatabase.Query("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = "
                                            "DATABASE() AND table_name = '" + name + "'");
        return r && r->Fetch()[0].Get<uint64>() > 0;
    }

    uint64 Count(std::string const& sql)
    {
        QueryResult r = WorldDatabase.Query(sql);
        return r ? r->Fetch()[0].Get<uint64>() : 0;
    }

    // Mechanism 1 for one table. Returns true when every captured row holds the value the mode wants and (mode 1)
    // every side-table row was captured and applied.
    bool ApplyOne(ApplySpec const& s, bool enabled)
    {
        std::string const base = "era_baseline_" + s.target;
        for (std::string const& t : { s.side, base })
        {
            if (!TableExists(t))
            {
                LOG_ERROR("server.loading", "[mod-era-profile] {}: table `{}` is missing (module SQL not applied?); "
                          "nothing changed", s.target, t);
                return false;
            }
        }

        std::vector<std::string> keyT, colT, notVanilla;
        for (auto const& k : s.keys)
            keyT.push_back("t." + Quoted(k));
        for (auto const& c : s.cols)
        {
            colT.push_back("t." + Quoted(c));
            notVanilla.push_back("(v." + Quoted(c) + " IS NULL OR t." + Quoted(c) + " <=> v." + Quoted(c) + ")");
        }
        std::string const from = " FROM " + Quoted(s.target) + " t JOIN " + Quoted(s.side) + " v ON " +
                                 OnKeys(s, "t", "v") + " LEFT JOIN " + Quoted(base) + " b ON " + OnKeys(s, "t", "b");
        std::string const uncaptured = "b." + Quoted(s.keys[0]) + " IS NULL";
        std::string const looksApplied = "(" + Join(notVanilla, " AND ") + ")";

        // a. capture stock values (first capture wins). A row already equal to its vanilla value is not stock.
        uint64 suspicious = Count("SELECT COUNT(*)" + from + " WHERE " + uncaptured + " AND " + looksApplied);
        uint64 before = Count("SELECT COUNT(*) FROM " + Quoted(base));
        WorldDatabase.DirectExecute("INSERT IGNORE INTO " + Quoted(base) + " (" + Join(s.keys, ", ") + ", " +
                                    Join(s.cols, ", ") + ") SELECT " + Join(keyT, ", ") + ", " + Join(colT, ", ") +
                                    from + " WHERE " + uncaptured + " AND NOT " + looksApplied);
        uint64 captured = Count("SELECT COUNT(*) FROM " + Quoted(base)) - before;

        // b. apply (mode 1) or revert (mode 0) every captured row in one statement
        std::vector<std::string> sets, mismatch;
        for (auto const& c : s.cols)
        {
            sets.push_back("t." + Quoted(c) + " = " + Wanted(c, enabled));
            mismatch.push_back("NOT (t." + Quoted(c) + " <=> " + Wanted(c, enabled) + ")");
        }
        std::string const fromBase = " FROM " + Quoted(s.target) + " t JOIN " + Quoted(base) + " b ON " +
                                     OnKeys(s, "t", "b") + " LEFT JOIN " + Quoted(s.side) + " v ON " +
                                     OnKeys(s, "t", "v");
        WorldDatabase.DirectExecute("UPDATE " + Quoted(s.target) + " t JOIN " + Quoted(base) + " b ON " +
                                    OnKeys(s, "t", "b") + " LEFT JOIN " + Quoted(s.side) + " v ON " +
                                    OnKeys(s, "t", "v") + " SET " + Join(sets, ", "));

        // c. verify
        uint64 wrong = Count("SELECT COUNT(*)" + fromBase + " WHERE " + Join(mismatch, " OR "));
        uint64 sideRows = Count("SELECT COUNT(*) FROM " + Quoted(s.side));
        uint64 sideCaptured = Count("SELECT COUNT(*) FROM " + Quoted(s.side) + " v JOIN " + Quoted(base) + " b ON " +
                                    OnKeys(s, "v", "b"));
        uint64 atVanilla = Count("SELECT COUNT(*)" + from + " WHERE " + looksApplied);
        bool ok = wrong == 0 && (!enabled || sideCaptured == sideRows);
        LOG_INFO("server.loading", "[mod-era-profile] {}: mode {}, {} side-table rows, {} captured into {} ({} new), "
                 "{} rows at vanilla values, {} rows differing from the mode", s.target, enabled ? "vanilla" : "stock",
                 sideRows, sideCaptured, base, captured, atVanilla, wrong);
        if (suspicious)
            LOG_ERROR("server.loading", "[mod-era-profile] {}: {} in-scope rows already hold their vanilla value but "
                      "have no captured baseline (was vanilla data applied outside the module?); they were not "
                      "captured and stay as they are", s.target, suspicious);
        if (!ok)
            LOG_ERROR("server.loading", "[mod-era-profile] {}: apply did not verify", s.target);
        return ok;
    }

    bool LoadQuestXp()
    {
        g_questXp.clear();
        if (!TableExists("era_quest_xp"))
        {
            LOG_ERROR("server.loading", "[mod-era-profile] table `era_quest_xp` is missing; quest XP stays stock");
            return false;
        }
        if (QueryResult r = WorldDatabase.Query("SELECT QuestId, QuestLevel, RewXP FROM era_quest_xp"))
        {
            do
            {
                Field* f = r->Fetch();
                g_questXp[f[0].Get<uint32>()] = { f[1].Get<int32>(), f[2].Get<uint32>() };
            } while (r->NextRow());
        }
        LOG_INFO("server.loading", "[mod-era-profile] quest XP: {} vanilla overrides loaded", g_questXp.size());
        return !g_questXp.empty();
    }

    // VMaNGOS Quest::XPValue (src/game/QuestDef.cpp:180-202 at vmangos/core 4b3d241, paraphrased in the memo): full
    // RewXP up to 5 levels above the quest, then float32 multipliers with ceilf, no step rounding.
    uint32 VanillaQuestXp(uint32 rewXp, int32 levelsAbove)
    {
        if (levelsAbove <= 5)
            return rewXp;
        float const f = levelsAbove == 6 ? 0.8f : levelsAbove == 7 ? 0.6f : levelsAbove == 8 ? 0.4f
                      : levelsAbove == 9 ? 0.2f : 0.1f;
        return uint32(std::ceil(float(rewXp) * f));
    }
}

class EraProfileWorldScript : public WorldScript
{
public:
    EraProfileWorldScript() : WorldScript("EraProfileWorldScript",
        { WORLDHOOK_ON_AFTER_CONFIG_LOAD, WORLDHOOK_ON_LOAD_CUSTOM_DATABASE_TABLE }) { }

    void OnAfterConfigLoad(bool reload) override
    {
        if (!reload)
            return;
        bool now = sConfigMgr->GetOption<bool>(OPT_ENABLE, true);
        if (now != g_enabled)
            LOG_WARN("server.loading", "[mod-era-profile] {} changed to {} by a config reload; it takes effect at the "
                     "next server restart (still {})", OPT_ENABLE, now ? 1 : 0, g_enabled ? 1 : 0);
    }

    // Before every ObjectMgr world-data load (see the file comment), after the DB updater applied the module SQL.
    void OnLoadCustomDatabaseTable() override
    {
        g_enabled = sConfigMgr->GetOption<bool>(OPT_ENABLE, true);
        bool ok = true;
        for (ApplySpec const& s : APPLY_SPECS)
            ok = ApplyOne(s, g_enabled) && ok;
        bool quest = LoadQuestXp();
        g_active = g_enabled && ok && quest;
        if (g_active)
            LOG_INFO("server.loading", "[mod-era-profile] Vanilla mode ACTIVE ({} = 1): levels 1-60 use VMaNGOS 1.12 "
                     "values (XP per level 11-59, quest XP)", OPT_ENABLE);
        else if (!g_enabled && ok)
            LOG_INFO("server.loading", "[mod-era-profile] Vanilla mode OFF ({} = 0): stock WotLK values", OPT_ENABLE);
        else
            LOG_ERROR("server.loading", "[mod-era-profile] Vanilla mode FAILED to {} ({} = {}): see the errors above",
                      g_enabled ? "apply" : "revert", OPT_ENABLE, g_enabled ? 1 : 0);
    }
};

class EraProfilePlayerScript : public PlayerScript
{
public:
    EraProfilePlayerScript() : PlayerScript("EraProfilePlayerScript", { PLAYERHOOK_ON_QUEST_COMPUTE_EXP }) { }

    // Called with AC's finished quest XP: on reward (PlayerQuest.cpp:764) and for the quest details / offer-reward
    // windows (GossipDef.cpp:473, :725). Replaces it for in-scope quests, keeping AC's rate, aura and "no XP"
    // (repeat completion, money instead of XP: incoming 0) semantics.
    void OnPlayerQuestComputeXP(Player* player, Quest const* quest, uint32& xp) override
    {
        if (!g_active || !player || !quest || xp == 0)
            return;
        auto it = g_questXp.find(quest->GetQuestId());
        if (it == g_questXp.end() || it->second.questLevel != quest->GetQuestLevel())
            return;
        uint8 level = player->GetLevel();
        sScriptMgr->OnPlayerBeforeGetLevelForXPGain(player, level);    // as Player::CalculateQuestRewardXP
        uint32 out = uint32(VanillaQuestXp(it->second.rewXp, int32(level) - quest->GetQuestLevel()) *
                            player->GetQuestRate(quest->IsDFQuest()));
        out *= player->GetTotalAuraMultiplier(SPELL_AURA_MOD_XP_QUEST_PCT);
        xp = out;
    }
};

void AddEraProfileScripts()
{
    new EraProfileWorldScript();
    new EraProfilePlayerScript();
}
