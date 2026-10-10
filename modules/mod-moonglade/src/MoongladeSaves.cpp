/*
 * mod-moonglade: per-character saves (openspec/changes/front-menu-saves). PROTOTYPE (task 1.1, Q3): the smallest
 * in-game load that answers "does the character list after SMSG_LOGOUT_COMPLETE show the restored level?".
 *   - `.moonglade save protosnap <name>` (SOAP): remember an online character's level, XP, money and position.
 *   - Add-on whisper "MGSAVE\tLOAD" (to itself): on the next world tick the module sends the instant-logout response,
 *     logs the character out WITHOUT saving it (LogoutPlayer(false)), and queues one transaction that writes the
 *     remembered values back. The character database's async queue is FIFO with one worker, so the client's next
 *     character-list query is answered after it.
 */

#include "CharacterPackets.h"
#include "Chat.h"
#include "CommandScript.h"
#include "DatabaseEnv.h"
#include "Log.h"
#include "ObjectAccessor.h"
#include "Player.h"
#include "ScriptMgr.h"
#include "WorldSession.h"

#include <mutex>
#include <string>
#include <string_view>
#include <unordered_map>
#include <vector>

using namespace Acore::ChatCommands;

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
        {
            LOG_INFO("module", "[mod-moonglade] proto load: no player or no snapshot for {}", guid.ToString());
            return;
        }
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
        LOG_INFO("module", "[mod-moonglade] proto load queued for {} (level {})", guid.ToString(), s.level);
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
    MoongladeSavesWorldScript() : WorldScript("MoongladeSavesWorldScript", { WORLDHOOK_ON_UPDATE }) { }

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
            { "protosnap", HandleProtoSnap, SEC_GAMEMASTER, Console::Yes },
        };
        static ChatCommandTable moongladeTable = { { "save", saveTable } };
        static ChatCommandTable commandTable = { { "moonglade", moongladeTable } };
        return commandTable;
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
