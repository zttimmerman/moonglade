/*
 * SPIKE PROTOTYPE (m4-dungeon-scaling task 1.1, branch spike/m4-dungeon; not on main).
 *
 * Fresh after clear: when a real (non-bot) player is about to enter a 5-player dungeon whose saved copy has its final
 * encounter done (DungeonEncounter list entry with lastEncounterDungeon != 0, bit = dbcEntry->encounterIndex) and no
 * real player is inside it, reset it the way Player::ResetInstances(INSTANCE_RESET_ALL) does (PlayerMisc.cpp:191-230).
 * If the map can't be reset (bots still inside), only this player is unbound, so they get a new copy.
 * Hook: PlayerScript::OnPlayerCanEnterMap (MapMgr.cpp:165), before the destination instance is chosen (:225).
 */

#include "Config.h"
#include "InstanceSaveMgr.h"
#include "Log.h"
#include "Map.h"
#include "MapMgr.h"
#include "ObjectMgr.h"
#include "Player.h"
#include "ScriptMgr.h"
#include "WorldSession.h"

namespace
{
    char const* const OPT_FRESH = "Moonglade.Dungeons.FreshAfterClear";
    bool g_fresh = true;

    bool FinalDone(InstanceSave const* save, uint32 mapId, Difficulty diff)
    {
        DungeonEncounterList const* list = sObjectMgr->GetDungeonEncounterList(mapId, diff);
        if (!list)
            return false;
        bool any = false;
        for (DungeonEncounter const* enc : *list)
        {
            if (!enc->lastEncounterDungeon)
                continue;
            any = true;
            if (!(save->GetCompletedEncounterMask() & (1u << enc->dbcEntry->encounterIndex)))
                return false;
        }
        return any;
    }

    bool RealPlayerInside(Map* map)
    {
        for (auto const& ref : map->GetPlayers())
            if (Player* p = ref.GetSource())
                if (!p->GetSession()->IsBot())
                    return true;
        return false;
    }
}

class MoongladeDungeonsWorldScript : public WorldScript
{
public:
    MoongladeDungeonsWorldScript() : WorldScript("MoongladeDungeonsWorldScript", { WORLDHOOK_ON_AFTER_CONFIG_LOAD }) { }

    void OnAfterConfigLoad(bool /*reload*/) override
    {
        g_fresh = sConfigMgr->GetOption<bool>(OPT_FRESH, true);
        LOG_INFO("module", "[mod-moonglade] {} = {}", OPT_FRESH, g_fresh ? 1 : 0);
    }
};

class MoongladeDungeonsPlayerScript : public PlayerScript
{
public:
    MoongladeDungeonsPlayerScript() : PlayerScript("MoongladeDungeonsPlayerScript", { PLAYERHOOK_CAN_ENTER_MAP }) { }

    bool OnPlayerCanEnterMap(Player* player, MapEntry const* entry, InstanceTemplate const* /*instance*/,
                             MapDifficulty const* /*mapDiff*/, bool loginCheck) override
    {
        if (!g_fresh || loginCheck || !entry || !entry->IsNonRaidDungeon() || player->GetSession()->IsBot())
            return true;
        Difficulty diff = player->GetDifficulty(false);
        InstanceSave* save = sInstanceSaveMgr->PlayerGetInstanceSave(player->GetGUID(), entry->MapID, diff);
        if (!save)
            return true;
        uint32 mapId = entry->MapID, instanceId = save->GetInstanceId(), mask = save->GetCompletedEncounterMask();
        if (!FinalDone(save, mapId, diff))
        {
            LOG_INFO("module", "[mod-moonglade] dungeons: {} enters map {} instance {} (mask {:#x}): not finished, kept",
                     player->GetName(), mapId, instanceId, mask);
            return true;
        }
        Map* map = sMapMgr->FindMap(mapId, instanceId);
        if (map && RealPlayerInside(map))
            return true;
        if (!map || map->ToInstanceMap()->Reset(INSTANCE_RESET_ALL))
        {
            sInstanceSaveMgr->DeleteInstanceSavedData(instanceId);
            sInstanceSaveMgr->UnbindAllFor(save);
            LOG_INFO("module", "[mod-moonglade] dungeons: {} map {} instance {} (mask {:#x}) finished: reset, all unbound",
                     player->GetName(), mapId, instanceId, mask);
        }
        else
        {
            sInstanceSaveMgr->PlayerUnbindInstance(player->GetGUID(), mapId, diff, true, player);
            LOG_INFO("module", "[mod-moonglade] dungeons: {} map {} instance {} (mask {:#x}) finished, bots inside: "
                     "player unbound only", player->GetName(), mapId, instanceId, mask);
        }
        return true;
    }
};

void AddMoongladeDungeonsScripts()
{
    new MoongladeDungeonsWorldScript();
    new MoongladeDungeonsPlayerScript();
}
