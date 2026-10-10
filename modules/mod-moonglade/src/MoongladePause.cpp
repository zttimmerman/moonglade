/*
 * mod-moonglade: true pause (openspec/changes/true-pause, ADR-0013). Opening the game menu (Esc) stops the whole
 * world; closing it resumes exactly where it stopped.
 *
 * The core patch patches/core/0003-world-pause.patch adds the switch (WorldPause::SetPaused, game/Time/WorldPause.h):
 * while it is on, the simulation gets zero elapsed time, maps only process their players' packets and GameTime stands
 * still. Everything about *when* and *how* to pause lives here:
 *   - Toggle: the Moonglade add-on sends SendAddonMessage("MGPAUSE", "1" | "0", "WHISPER", <self>) when the game menu
 *     opens or closes. It arrives as CMSG_MESSAGECHAT, a thread-unsafe opcode handled in World::UpdateSessions (so it
 *     is processed while the maps are paused); Player::Whisper calls OnPlayerCanUseChat(..., receiver), where the
 *     shared add-on parser (MoongladeAddon.h, also used by the saves' "MGSAVE") hands it here and swallows it. The server answers with an add-on message "MGPAUSE" "paused" | "running" | "refused" |
 *     "disabled" so the add-on shows its banner only when the world really paused. "paused" is sent from the first
 *     world tick that runs paused (the switch takes effect at the start of a tick), so nothing the simulation sends
 *     follows it until the resume.
 *   - `.moonglade pause on|off|status` (GM; also over SOAP/console). In game, `on` pauses for the GM's own character;
 *     from the console it pauses the world without a character (nobody to root or resync).
 *   - On pause: refused on a flight path or a transport (boat, zeppelin), with a message to the player; otherwise the
 *     player's cast or channel is cancelled (Zach, 2026-10-09) and the player is rooted until resume.
 *   - On resume: the player is unrooted, and the client is sent the server's aura durations (player, target, party,
 *     pet) and spell cooldowns, which its own clock ran ahead on while paused (design.md "Prototype (task 1.1)").
 *   - Logging out unpauses first (CMSG_LOGOUT_REQUEST, before its handler starts the logout countdown, which reads
 *     GameTime), and any logout or disconnect of the pausing player resumes the world (a disconnect is seen on the
 *     next world tick: WorldScript::OnUpdate).
 *   - Moonglade.Pause.Enable = 0: pause requests do nothing.
 *   - Harness read-outs (GM): `.moonglade pause cooldown <spellId>`, the selected unit's remaining cooldown on the
 *     server (Player::GetSpellCooldownDelay / Creature::GetSpellCooldown); `.moonglade pause unit`, the selected
 *     unit's health, power, position and current spells.
 * All hooks used here run on the world thread (World::UpdateSessions, command processing, logout).
 */

#include "Chat.h"
#include "CommandScript.h"
#include "Config.h"
#include "Creature.h"
#include "GameTime.h"
#include "Group.h"
#include "Log.h"
#include "MoongladeAddon.h"
#include "ObjectAccessor.h"
#include "Opcodes.h"
#include "Pet.h"
#include "Player.h"
#include "ScriptMgr.h"
#include "Spell.h"
#include "SpellAuras.h"
#include "Timer.h"
#include "WorldPacket.h"
#include "WorldPause.h"
#include "WorldSession.h"

#include <string>
#include <string_view>

using namespace Acore::ChatCommands;

namespace
{
    char const* const OPT_ENABLE = "Moonglade.Pause.Enable";

    bool g_enabled = true;
    ObjectGuid g_pausedBy;            // the character whose request paused the world (empty: console pause)
    bool g_weRooted = false;          // we rooted g_pausedBy and must unroot it on resume
    uint32 g_pauseStartMS = 0;        // getMSTime() when the pause began (the client's clock ran on from here)
    bool g_ackPending = false;        // tell g_pausedBy "paused" once a world tick runs paused

    void SendAddonState(Player* player, std::string_view state)
    {
        MoongladeAddon::Send(player, "MGPAUSE", state);
    }

    /// Re-send a unit's visible auras: SMSG_AURA_UPDATE carries the server's (frozen) remaining duration.
    void ResyncAuras(Unit* unit)
    {
        if (!unit || !unit->IsInWorld())
            return;
        for (auto const& [slot, aurApp] : *unit->GetVisibleAuras())
            aurApp->ClientUpdate();
    }

    /// The client's cooldown timers ran on for pausedMs: add it back to those it still runs (SMSG_MODIFY_COOLDOWN, as
    /// Player::ModifySpellCooldown sends it) and re-send the ones it has already finished (SMSG_SPELL_COOLDOWN with
    /// the server's remaining time). The server's cooldowns themselves are frozen and are not touched.
    void ResyncCooldowns(Player* player, uint32 pausedMs)
    {
        uint32 now = uint32(GameTime::GetGameTimeMS().count());
        for (auto const& [spellId, cd] : player->GetSpellCooldownMap())
        {
            if (!cd.needSendToClient || cd.end <= now)
                continue;
            uint32 left = cd.end - now;
            WorldPacket data;
            if (left > pausedMs)
            {
                data.Initialize(SMSG_MODIFY_COOLDOWN, 4 + 8 + 4);
                data << uint32(spellId);
                data << player->GetGUID();
                data << int32(pausedMs);
            }
            else
                player->BuildCooldownPacket(data, SPELL_COOLDOWN_FLAG_NONE, spellId, left);
            player->SendDirectMessage(&data);
        }
    }

    void ResyncClient(Player* player, uint32 pausedMs)
    {
        ResyncAuras(player);
        if (Unit* target = player->GetSelectedUnit())
            if (target != player)
                ResyncAuras(target);
        if (Pet* pet = player->GetPet())
            ResyncAuras(pet);
        if (Group* group = player->GetGroup())
            for (GroupReference* ref = group->GetFirstMember(); ref; ref = ref->next())
                if (Player* member = ref->GetSource())
                    if (member != player && member->IsInWorld() && member->GetMap() == player->GetMap())
                        ResyncAuras(member);
        ResyncCooldowns(player, pausedMs);
    }

    enum class PauseResult { Paused, AlreadyPaused, Refused, Disabled };

    PauseResult Pause(Player* player)
    {
        if (!g_enabled)
            return PauseResult::Disabled;
        if (WorldPause::IsPaused())
            return PauseResult::AlreadyPaused;
        if (player)
        {
            // the client moves a passenger itself; pausing there would desync it (spike Q3)
            if (player->IsInFlight() || player->GetTransport())
            {
                ChatHandler(player->GetSession()).SendSysMessage("You can't pause while travelling.");
                SendAddonState(player, "refused");
                return PauseResult::Refused;
            }
            player->InterruptNonMeleeSpells(false);
            g_weRooted = !player->HasUnitState(UNIT_STATE_ROOT);
            if (g_weRooted)
                player->SetControlled(true, UNIT_STATE_ROOT);
            g_pausedBy = player->GetGUID();
        }
        else
        {
            g_pausedBy.Clear();
            g_weRooted = false;
        }
        g_pauseStartMS = getMSTime();
        WorldPause::SetPaused(true);
        g_ackPending = player != nullptr;
        LOG_INFO("module", "[mod-moonglade] world paused by {}", player ? player->GetName() : "the console");
        return PauseResult::Paused;
    }

    bool Resume()
    {
        if (!WorldPause::IsPaused())
            return false;
        WorldPause::SetPaused(false);
        g_ackPending = false;
        uint32 pausedMs = getMSTimeDiff(g_pauseStartMS, getMSTime());
        Player* player = g_pausedBy ? ObjectAccessor::FindConnectedPlayer(g_pausedBy) : nullptr;
        if (player && player->IsInWorld())
        {
            // only the root this module applied is lifted (SetControlled keeps a root aura's or a vehicle's)
            if (g_weRooted)
                player->SetControlled(false, UNIT_STATE_ROOT);
            ResyncClient(player, pausedMs);
            SendAddonState(player, "running");
        }
        LOG_INFO("module", "[mod-moonglade] world resumed after {} ms", pausedMs);
        g_pausedBy.Clear();
        g_weRooted = false;
        return true;
    }
}

class MoongladePauseWorldScript : public WorldScript
{
public:
    MoongladePauseWorldScript() : WorldScript("MoongladePauseWorldScript",
        { WORLDHOOK_ON_AFTER_CONFIG_LOAD, WORLDHOOK_ON_UPDATE }) { }

    // runs every world tick (with zero elapsed time while paused): the first paused tick acknowledges the pause, and a
    // pause whose player has gone or lost its connection ends. (A dropped client's session waits as an "offline
    // session" until 60 s of GameTime have passed, WorldSessionMgr::UpdateSessions, which never happens while the
    // clock is stopped; so a crash or a killed client would otherwise leave the world paused for good.)
    void OnUpdate(uint32 /*diff*/) override
    {
        if (!WorldPause::IsPaused() || !g_pausedBy)
            return;
        Player* player = ObjectAccessor::FindConnectedPlayer(g_pausedBy);
        if (!player || player->GetSession()->IsSocketClosed())
        {
            LOG_INFO("module", "[mod-moonglade] the pausing player is gone or disconnected; resuming");
            Resume();
            return;
        }
        if (g_ackPending && WorldPause::IsTickPaused())
        {
            g_ackPending = false;
            SendAddonState(player, "paused");
        }
    }

    void OnAfterConfigLoad(bool /*reload*/) override
    {
        g_enabled = sConfigMgr->GetOption<bool>(OPT_ENABLE, true);
        LOG_INFO("module", "[mod-moonglade] {} = {}", OPT_ENABLE, g_enabled ? 1 : 0);
        if (!g_enabled)
            Resume();
    }
};

class MoongladePausePlayerScript : public PlayerScript
{
public:
    MoongladePausePlayerScript() : PlayerScript("MoongladePausePlayerScript", { PLAYERHOOK_ON_LOGOUT }) { }

    void OnPlayerLogout(Player* player) override
    {
        if (WorldPause::IsPaused() && player->GetGUID() == g_pausedBy)
            Resume();
    }
};

class MoongladePauseServerScript : public ServerScript
{
public:
    MoongladePauseServerScript() : ServerScript("MoongladePauseServerScript", { SERVERHOOK_CAN_PACKET_RECEIVE }) { }

    // CMSG_LOGOUT_REQUEST is thread-unsafe (World::UpdateSessions, the world thread); this runs right before its handler
    bool CanPacketReceive(WorldSession* /*session*/, WorldPacket const& packet) override
    {
        if (packet.GetOpcode() == CMSG_LOGOUT_REQUEST && WorldPause::IsPaused())
            Resume();
        return true;
    }
};

class MoongladePauseCommandScript : public CommandScript
{
public:
    MoongladePauseCommandScript() : CommandScript("MoongladePauseCommandScript") { }

    ChatCommandTable GetCommands() const override
    {
        static ChatCommandTable pauseTable =
        {
            { "on",       HandleOn,       SEC_GAMEMASTER, Console::Yes },
            { "off",      HandleOff,      SEC_GAMEMASTER, Console::Yes },
            { "status",   HandleStatus,   SEC_GAMEMASTER, Console::Yes },
            { "cooldown", HandleCooldown, SEC_GAMEMASTER, Console::No  },
            { "unit",     HandleUnit,     SEC_GAMEMASTER, Console::No  },
        };
        static ChatCommandTable moongladeTable = { { "pause", pauseTable } };
        static ChatCommandTable commandTable = { { "moonglade", moongladeTable } };
        return commandTable;
    }

    static bool HandleOn(ChatHandler* handler)
    {
        switch (Pause(handler->GetPlayer()))
        {
            case PauseResult::Paused:        handler->SendSysMessage("Moonglade pause: paused"); return true;
            case PauseResult::AlreadyPaused: handler->SendSysMessage("Moonglade pause: already paused"); return true;
            case PauseResult::Refused:       handler->SendSysMessage("Moonglade pause: refused (travelling)"); return true;
            case PauseResult::Disabled:      handler->SendSysMessage("Moonglade pause: disabled"); return true;
        }
        return true;
    }

    static bool HandleOff(ChatHandler* handler)
    {
        handler->SendSysMessage(Resume() ? "Moonglade pause: running" : "Moonglade pause: already running");
        return true;
    }

    static bool HandleStatus(ChatHandler* handler)
    {
        handler->PSendSysMessage("Moonglade pause: {} (paused total {} ms, enabled {})",
            WorldPause::IsPaused() ? "paused" : "running", WorldPause::GetPausedTotal().count(), g_enabled ? 1 : 0);
        return true;
    }

    static bool HandleUnit(ChatHandler* handler)
    {
        Unit* unit = handler->getSelectedUnit();
        if (!unit)
        {
            handler->SendErrorMessage(LANG_SELECT_CHAR_OR_CREATURE);
            return false;
        }
        Spell const* generic = unit->GetCurrentSpell(CURRENT_GENERIC_SPELL);
        Spell const* channel = unit->GetCurrentSpell(CURRENT_CHANNELED_SPELL);
        handler->PSendSysMessage("Moonglade unit: guid {} health {} power {} powertype {} x {:.3f} y {:.3f} z {:.3f} "
            "spell {} channel {} combat {}", unit->GetGUID().GetRawValue(), unit->GetHealth(),
            unit->GetPower(unit->getPowerType()), uint32(unit->getPowerType()), unit->GetPositionX(),
            unit->GetPositionY(), unit->GetPositionZ(), generic ? generic->m_spellInfo->Id : 0,
            channel ? channel->m_spellInfo->Id : 0, unit->IsInCombat() ? 1 : 0);
        return true;
    }

    static bool HandleCooldown(ChatHandler* handler, uint32 spellId)
    {
        Unit* unit = handler->getSelectedUnit();
        if (!unit)
        {
            handler->SendErrorMessage(LANG_SELECT_CHAR_OR_CREATURE);
            return false;
        }
        uint32 left = 0;
        if (Player* player = unit->ToPlayer())
            left = player->GetSpellCooldownDelay(spellId);
        else if (Creature* creature = unit->ToCreature())
            left = creature->GetSpellCooldown(spellId);
        handler->PSendSysMessage("Moonglade cooldown: spell {} remaining {} ms", spellId, left);
        return true;
    }
};

void AddMoongladePauseScripts()
{
    // "MGPAUSE" "1" | "0" from the add-on (the shared parser, MoongladeAddon.h)
    MoongladeAddon::Register("MGPAUSE", [](Player* player, std::string_view arg)
    {
        if (arg == "1")
        {
            if (Pause(player) == PauseResult::Disabled)
                SendAddonState(player, "disabled");
        }
        else if (arg == "0")
        {
            if (!Resume())
                SendAddonState(player, "running");
        }
    });
    new MoongladePauseWorldScript();
    new MoongladePausePlayerScript();
    new MoongladePauseServerScript();
    new MoongladePauseCommandScript();
}
