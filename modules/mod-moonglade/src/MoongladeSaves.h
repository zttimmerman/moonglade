/*
 * mod-moonglade: per-character saves (openspec/changes/front-menu-saves, design.md sections 1-5).
 *
 * The save set (design.md 1) is declared here once, as data: which character-database tables a save holds and how
 * their rows are found for one character, which tables are deliberately left as they are on load, and which rows a
 * load removes. Every table of the character database with a character or item column is on exactly one list
 * (`saves.table_coverage` compares the running database with `.moonglade save tables`).
 */

#ifndef MOONGLADE_SAVES_H
#define MOONGLADE_SAVES_H

#include <string_view>
#include <vector>

namespace MoongladeSaves
{
    /// How a save-set table's rows for character {g} are found (and deleted on load). `where` is SQL with {g} for the
    /// character's guid counter; `idCols` are the columns whose values are renumbered on load (item, pet, set ids).
    enum class Renumber { None, Item, ItemInSet, ItemRaw, Pet, EquipmentSet };

    struct RenumberColumn
    {
        std::string_view column;
        Renumber kind;
    };

    struct SaveTable
    {
        std::string_view name;
        std::string_view where;
        std::vector<RenumberColumn> renumber;
    };

    // Order matters twice: rows are read in this order, and deleted in reverse order (dependants first), then
    // inserted in this order. Items are found through this save's own character_inventory rows only, so items in
    // mail or at the auction house (also owned by the character in item_instance.owner_guid) are never touched.
    // Source: the core's own per-character table list, PlayerDump.cpp DumpTables (:88-121) and InitializeTables
    // (:253-376), at the pinned core_sha; classification in design.md section 1.
    inline constexpr std::string_view INV_ITEMS = "(SELECT item FROM character_inventory WHERE guid = {g})";
    inline constexpr std::string_view OWN_PETS = "(SELECT id FROM character_pet WHERE owner = {g})";

    inline SaveTable const SAVE_SET[] =
    {
        { "characters",                     "guid = {g}", {} },
        { "character_spell",                "guid = {g}", {} },
        { "character_talent",               "guid = {g}", {} },
        { "character_glyphs",               "guid = {g}", {} },
        { "character_action",               "guid = {g}", {} },
        { "character_skills",               "guid = {g}", {} },
        { "character_queststatus",          "guid = {g}", {} },
        { "character_queststatus_rewarded", "guid = {g}", {} },
        { "character_queststatus_daily",    "guid = {g}", {} },
        { "character_queststatus_weekly",   "guid = {g}", {} },
        { "character_queststatus_monthly",  "guid = {g}", {} },
        { "character_queststatus_seasonal", "guid = {g}", {} },
        { "character_reputation",           "guid = {g}", {} },
        { "character_achievement",          "guid = {g}", {} },
        { "character_achievement_progress", "guid = {g}", {} },
        { "character_aura",                 "guid = {g}", { { "itemGuid", Renumber::ItemRaw } } },
        { "character_spell_cooldown",       "guid = {g}", {} },
        { "character_homebind",             "guid = {g}", {} },
        { "character_declinedname",         "guid = {g}", {} },
        { "character_inventory",            "guid = {g}", { { "bag", Renumber::Item }, { "item", Renumber::Item } } },
        { "item_instance",                  "guid IN (SELECT item FROM character_inventory WHERE guid = {g})",
                                            { { "guid", Renumber::Item } } },
        { "character_gifts",                "item_guid IN (SELECT item FROM character_inventory WHERE guid = {g})",
                                            { { "item_guid", Renumber::Item } } },
        { "character_equipmentsets",        "guid = {g}", { { "setguid", Renumber::EquipmentSet },
            { "item0", Renumber::ItemInSet }, { "item1", Renumber::ItemInSet }, { "item2", Renumber::ItemInSet },
            { "item3", Renumber::ItemInSet }, { "item4", Renumber::ItemInSet }, { "item5", Renumber::ItemInSet },
            { "item6", Renumber::ItemInSet }, { "item7", Renumber::ItemInSet }, { "item8", Renumber::ItemInSet },
            { "item9", Renumber::ItemInSet }, { "item10", Renumber::ItemInSet }, { "item11", Renumber::ItemInSet },
            { "item12", Renumber::ItemInSet }, { "item13", Renumber::ItemInSet }, { "item14", Renumber::ItemInSet },
            { "item15", Renumber::ItemInSet }, { "item16", Renumber::ItemInSet }, { "item17", Renumber::ItemInSet },
            { "item18", Renumber::ItemInSet } } },
        { "character_pet",                  "owner = {g}", { { "id", Renumber::Pet } } },
        { "character_pet_declinedname",     "owner = {g}", { { "id", Renumber::Pet } } },
        { "pet_aura",                       "guid IN (SELECT id FROM character_pet WHERE owner = {g})",
                                            { { "guid", Renumber::Pet } } },
        { "pet_spell",                      "guid IN (SELECT id FROM character_pet WHERE owner = {g})",
                                            { { "guid", Renumber::Pet } } },
        { "pet_spell_cooldown",             "guid IN (SELECT id FROM character_pet WHERE owner = {g})",
                                            { { "guid", Renumber::Pet } } },
    };

    /// Rows a load removes besides the save set's own: the character's corpse (a save is never taken while dead) and
    /// the short-lived item rows of the items the load replaces (they name the old item ids). Deleted before the
    /// save set's rows (the item ones read character_inventory).
    struct RemovedTable
    {
        std::string_view name;
        std::string_view where;
    };

    inline RemovedTable const REMOVED_ON_LOAD[] =
    {
        { "corpse",                    "guid = {g}" },
        { "item_refund_instance",      "item_guid IN (SELECT item FROM character_inventory WHERE guid = {g})" },
        { "item_soulbound_trade_data", "itemGuid IN (SELECT item FROM character_inventory WHERE guid = {g})" },
        { "item_loot_storage",         "containerGUID IN (SELECT item FROM character_inventory WHERE guid = {g})" },
    };

    /// Tables with a character or item column that a load deliberately leaves as they are (world or social state,
    /// interface settings, another character's, or derived), design.md section 1.
    inline std::string_view const KEPT[] =
    {
        // in the core's dump, not in a save
        "character_account_data", "mail", "mail_items",
        // social, group, guild, arena, calendar, petitions
        "character_social", "guild", "guild_member", "guild_member_withdraw", "guild_bank_item", "guild_bank_eventlog",
        "guild_eventlog", "group_member", "groups", "arena_team", "arena_team_member", "character_arena_stats",
        "petition", "petition_sign", "calendar_events", "calendar_invites", "channels_bans",
        // world state: lockouts, auctions, respawns, instances
        "character_instance", "auctionhouse", "creature_respawn", "gameobject_respawn", "instance_saved_go_state_data",
        // short-lived or bookkeeping state that stays with the world
        "character_battleground_random", "character_entry_point", "battleground_deserters", "lfg_data",
        "character_settings", "character_brew_of_the_month", "character_achievement_offline_updates",
        "character_banned", "mail_server_character", "recovery_item", "quest_tracker",
        // derived (the server rewrites it)
        "character_stats",
        // reports and logs
        "gm_ticket", "gm_survey", "lag_reports", "spam_reports", "log_arena_memberstats", "pvpstats_players",
        "log_encounter",
        // server-mail templates (their item column is an item entry, not an item)
        "mail_server_template_items",
    };
}

#endif
