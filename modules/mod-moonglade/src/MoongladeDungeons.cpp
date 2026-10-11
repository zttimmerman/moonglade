/*
 * mod-moonglade: relaxed 5-player dungeon lockouts (openspec/changes/m4-dungeon-scaling, spec dungeon-lockouts;
 * design.md §3 and "As built").
 *
 * 1. Fresh after clear (Moonglade.Dungeons.FreshAfterClear). When a real (non-bot) player is about to enter a 5-player
 *    dungeon (normal or heroic) whose saved copy has every final encounter done (each DungeonEncounter of the map and
 *    difficulty with lastEncounterDungeon != 0; its bit is dbcEntry->encounterIndex) and no real player is inside it,
 *    the save is cleared first, so the entry creates a fresh copy: the way Player::ResetInstances(INSTANCE_RESET_ALL)
 *    does it (PlayerMisc.cpp:191-230) when the map can be reset, which unbinds everyone bound to it (the player and the
 *    companions); otherwise (companions still inside the old copy) the player and the group's bots bound to it are
 *    unbound. Hook: PlayerScript::OnPlayerCanEnterMap (MapMgr.cpp:165), which runs before the destination instance is
 *    chosen (MapMgr.cpp:225, InstanceSaveMgr::PlayerGetDestinationInstanceId). It never refuses an entry. Logging in
 *    (loginCheck) and bots are skipped.
 *
 * 2. Keep an unfinished dungeon when the party breaks up (same key). Stock WotLK drops a player's non-permanent binds
 *    of maps they aren't in when they leave or disband a group (Group.cpp:736/842 -> Player::ResetInstances(
 *    INSTANCE_RESET_GROUP_LEAVE), PlayerMisc.cpp:295), and 5-player normal binds stay non-permanent after kills. So
 *    when a real player leaves a 5-player dungeon whose save has progress (completed-encounter mask != 0) but isn't
 *    finished, their bind is made permanent, as the core does for heroics after a boss kill (InstanceMap::
 *    PermBindAllPlayers). It is bound without a Player (no calendar lockout packet, no bind hooks). Fresh after clear
 *    still clears it once the dungeon is finished. Hook: AllMapScript::OnPlayerLeaveAll (Map::RemovePlayerFromMap, a
 *    map thread); the bind happens on the next world tick (WorldScript::OnUpdate), where the bind storage is changed
 *    by the world thread only.
 *
 * 3. Bring stranded companions out (Moonglade.Dungeons.FreeStrandedCompanions). A bot that logged out inside an older
 *    copy of a dungeon logs back into that copy, and the playerbots summon (SummonAction::Teleport -> TeleportTo with
 *    the same map id) only moves it within that copy. Once a second, for each real player leading a group: a bot
 *    member that is in a 5-player dungeon copy which is neither the leader's current map nor the leader's bound copy
 *    of that map is sent to its homebind; from there the summon (or playerbots' follow teleport) takes it to the
 *    leader's copy, or to the leader outside (the destination of a grouped bot is the leader's bind,
 *    InstanceSaveMgr::PlayerGetDestinationInstanceId).
 *
 * Every action writes one "[mod-moonglade] dungeons:" line to the module log.
 */

#include "Config.h"
#include "logic/DungeonsLogic.h"
#include "Group.h"
#include "InstanceSaveMgr.h"
#include "Log.h"
#include "Map.h"
#include "MapMgr.h"
#include "ObjectAccessor.h"
#include "ObjectMgr.h"
#include "Player.h"
#include "ScriptMgr.h"
#include "WorldSession.h"
#include "WorldSessionMgr.h"

#include <mutex>
#include <vector>

namespace
{
    char const* const OPT_FRESH = "Moonglade.Dungeons.FreshAfterClear";
    char const* const OPT_STRANDED = "Moonglade.Dungeons.FreeStrandedCompanions";
    uint32 const STRANDED_SWEEP_MS = 1000;

    bool g_fresh = true;
    bool g_stranded = true;

    struct PendingKeep
    {
        ObjectGuid guid;
        std::string name;
        uint32 mapId;
        Difficulty difficulty;
        uint32 instanceId;
    };
    std::mutex g_keepLock;
    std::vector<PendingKeep> g_keep;     // map threads add, the world thread drains
    uint32 g_sweepTimer = 0;

    // Every final encounter of the map and difficulty is in the save's completed-encounter mask. False when the
    // map has no final encounter (nothing to finish).
    // (the rule is logic/DungeonsLogic.cpp's FinalDone, unit-tested on the host; this passes it the encounter table)
    bool FinalDone(uint32 mask, uint32 mapId, Difficulty diff)
    {
        DungeonEncounterList const* list = sObjectMgr->GetDungeonEncounterList(mapId, diff);
        if (!list)
            return false;
        std::vector<MoongladeLogic::Encounter> encs;
        for (DungeonEncounter const* enc : *list)
            encs.push_back({ enc->lastEncounterDungeon != 0, uint32(enc->dbcEntry->encounterIndex) });
        return MoongladeLogic::FinalDone(mask, encs);
    }

    bool IsBot(Player const* p)
    {
        return p->GetSession() && p->GetSession()->IsBot();
    }

    bool RealPlayerInside(Map* map)
    {
        for (auto const& ref : map->GetPlayers())
            if (Player* p = ref.GetSource())
                if (!IsBot(p))
                    return true;
        return false;
    }

    void KeepPending()
    {
        std::vector<PendingKeep> todo;
        {
            std::lock_guard<std::mutex> lg(g_keepLock);
            todo.swap(g_keep);
        }
        for (PendingKeep const& k : todo)
        {
            InstancePlayerBind* bind = sInstanceSaveMgr->PlayerGetBoundInstance(k.guid, k.mapId, k.difficulty);
            if (!bind || !bind->save || bind->perm || bind->save->GetInstanceId() != k.instanceId)
                continue;
            uint32 mask = bind->save->GetCompletedEncounterMask();
            if (!MoongladeLogic::KeepOnLeave(mask, mask && FinalDone(mask, k.mapId, k.difficulty)))
                continue;
            sInstanceSaveMgr->PlayerBindToInstance(k.guid, bind->save, true, nullptr);
            LOG_INFO("module", "[mod-moonglade] dungeons: {} left map {} instance {} (mask {:#x}) unfinished: bind kept "
                     "(permanent)", k.name, k.mapId, k.instanceId, mask);
        }
    }

    void FreeStranded()
    {
        for (auto const& [id, session] : sWorldSessionMgr->GetAllSessions())
        {
            Player* leader = session ? session->GetPlayer() : nullptr;
            if (!leader || !leader->IsInWorld() || IsBot(leader))
                continue;
            Group* group = leader->GetGroup();
            if (!group || group->GetLeaderGUID() != leader->GetGUID())
                continue;
            for (GroupReference* ref = group->GetFirstMember(); ref; ref = ref->next())
            {
                Player* bot = ref->GetSource();
                if (!bot || bot == leader || !IsBot(bot) || !bot->IsInWorld() || bot->IsBeingTeleported())
                    continue;
                Map* map = bot->GetMap();
                if (!map || !map->IsNonRaidDungeon() || map == leader->GetMap())
                    continue;
                InstancePlayerBind* lb = sInstanceSaveMgr->PlayerGetBoundInstance(leader->GetGUID(), map->GetId(),
                                                                                   map->GetDifficulty());
                if (lb && lb->save && lb->save->GetInstanceId() == map->GetInstanceId())
                    continue;           // the leader's own copy (they just stepped out): not stranded
                LOG_INFO("module", "[mod-moonglade] dungeons: companion {} is in map {} instance {}, not {}'s copy "
                         "({}): sent to its homebind", bot->GetName(), map->GetId(), map->GetInstanceId(),
                         leader->GetName(), lb && lb->save ? lb->save->GetInstanceId() : 0);
                bot->TeleportTo(bot->m_homebindMapId, bot->m_homebindX, bot->m_homebindY, bot->m_homebindZ,
                                bot->GetOrientation());
            }
        }
    }
}

class MoongladeDungeonsWorldScript : public WorldScript
{
public:
    MoongladeDungeonsWorldScript() : WorldScript("MoongladeDungeonsWorldScript",
        { WORLDHOOK_ON_AFTER_CONFIG_LOAD, WORLDHOOK_ON_UPDATE }) { }

    void OnAfterConfigLoad(bool /*reload*/) override
    {
        g_fresh = sConfigMgr->GetOption<bool>(OPT_FRESH, true);
        g_stranded = sConfigMgr->GetOption<bool>(OPT_STRANDED, true);
        LOG_INFO("module", "[mod-moonglade] {} = {}, {} = {}", OPT_FRESH, g_fresh ? 1 : 0, OPT_STRANDED,
                 g_stranded ? 1 : 0);
    }

    void OnUpdate(uint32 diff) override
    {
        KeepPending();
        if (!g_stranded)
            return;
        g_sweepTimer += diff;
        if (g_sweepTimer < STRANDED_SWEEP_MS)
            return;
        g_sweepTimer = 0;
        FreeStranded();
    }
};

class MoongladeDungeonsMapScript : public AllMapScript
{
public:
    MoongladeDungeonsMapScript() : AllMapScript("MoongladeDungeonsMapScript", { ALLMAPHOOK_ON_PLAYER_LEAVE_ALL }) { }

    void OnPlayerLeaveAll(Map* map, Player* player) override
    {
        if (!g_fresh || !map || !player || !map->IsNonRaidDungeon() || IsBot(player))
            return;
        std::lock_guard<std::mutex> lg(g_keepLock);
        g_keep.push_back({ player->GetGUID(), player->GetName(), map->GetId(), map->GetDifficulty(),
                           map->GetInstanceId() });
    }
};

class MoongladeDungeonsPlayerScript : public PlayerScript
{
public:
    MoongladeDungeonsPlayerScript() : PlayerScript("MoongladeDungeonsPlayerScript", { PLAYERHOOK_CAN_ENTER_MAP }) { }

    bool OnPlayerCanEnterMap(Player* player, MapEntry const* entry, InstanceTemplate const* /*instance*/,
                             MapDifficulty const* /*mapDiff*/, bool loginCheck) override
    {
        if (!g_fresh || loginCheck || !entry || !entry->IsNonRaidDungeon() || IsBot(player))
            return true;
        Difficulty diff = player->GetDifficulty(false);
        InstanceSave* save = sInstanceSaveMgr->PlayerGetInstanceSave(player->GetGUID(), entry->MapID, diff);
        if (!save)
            return true;
        uint32 mapId = entry->MapID, instanceId = save->GetInstanceId(), mask = save->GetCompletedEncounterMask();
        // the decision is logic/DungeonsLogic.cpp's DecideEnter (unit-tested on the host)
        bool finished = FinalDone(mask, mapId, diff);
        Map* map = finished ? sMapMgr->FindMap(mapId, instanceId) : nullptr;
        bool inside = map && RealPlayerInside(map);
        bool reset = finished && !inside && map ? map->ToInstanceMap()->Reset(INSTANCE_RESET_ALL) : false;
        MoongladeLogic::EnterDecision d = MoongladeLogic::DecideEnter(finished, inside, map != nullptr, reset);
        if (d == MoongladeLogic::EnterDecision::Keep)
        {
            LOG_INFO("module", "[mod-moonglade] dungeons: {} enters map {} instance {} (mask {:#x}): not finished, "
                     "kept", player->GetName(), mapId, instanceId, mask);
            return true;
        }
        if (d == MoongladeLogic::EnterDecision::KeepPlayerInside)
        {
            LOG_INFO("module", "[mod-moonglade] dungeons: {} enters map {} instance {} (mask {:#x}): finished but a "
                     "player is inside, kept", player->GetName(), mapId, instanceId, mask);
            return true;
        }
        if (d == MoongladeLogic::EnterDecision::ResetAll)
        {
            sInstanceSaveMgr->DeleteInstanceSavedData(instanceId);
            sInstanceSaveMgr->UnbindAllFor(save);   // the player and every companion bound to it
            LOG_INFO("module", "[mod-moonglade] dungeons: {} enters map {} instance {} (mask {:#x}): finished, reset "
                     "(all unbound)", player->GetName(), mapId, instanceId, mask);
            return true;
        }
        // companions still inside the old copy: unbind the player and the group's bots bound to it; the stranded
        // sweep then brings the bots out, and the map unloads after Instance.UnloadDelay
        std::vector<ObjectGuid> bots;
        if (Group* group = player->GetGroup())
            for (GroupReference* ref = group->GetFirstMember(); ref; ref = ref->next())
                if (Player* m = ref->GetSource())
                    if (m != player && IsBot(m))
                        if (InstanceSave* s = sInstanceSaveMgr->PlayerGetInstanceSave(m->GetGUID(), mapId, diff))
                            if (s == save)
                                bots.push_back(m->GetGUID());
        sInstanceSaveMgr->PlayerUnbindInstance(player->GetGUID(), mapId, diff, true, player);
        for (ObjectGuid const& g : bots)
            sInstanceSaveMgr->PlayerUnbindInstance(g, mapId, diff, true, ObjectAccessor::FindConnectedPlayer(g));
        LOG_INFO("module", "[mod-moonglade] dungeons: {} enters map {} instance {} (mask {:#x}): finished, companions "
                 "inside: player and {} companion(s) unbound", player->GetName(), mapId, instanceId, mask, bots.size());
        return true;
    }
};

void AddMoongladeDungeonsScripts()
{
    new MoongladeDungeonsWorldScript();
    new MoongladeDungeonsMapScript();
    new MoongladeDungeonsPlayerScript();
}
