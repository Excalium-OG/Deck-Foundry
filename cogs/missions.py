import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncpg
import random
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List
import math

from utils.card_helpers import get_player_deck_state, update_player_credits

RARITY_HIERARCHY = ['Common', 'Uncommon', 'Exceptional', 'Rare', 'Epic', 'Legendary', 'Mythic']

RARITY_WEIGHTS = {
    'Common': 35,
    'Uncommon': 25,
    'Exceptional': 18,
    'Rare': 12,
    'Epic': 6,
    'Legendary': 3,
    'Mythic': 1
}

RARITY_COLORS = {
    'Common': 0x9CA3AF,
    'Uncommon': 0x10B981,
    'Exceptional': 0x3B82F6,
    'Rare': 0x8B5CF6,
    'Epic': 0xA855F7,
    'Legendary': 0xF59E0B,
    'Mythic': 0xEF4444
}

SUCCESS_RATE_MATRIX = {
    'Common': {'Common': 90, 'Uncommon': 92, 'Exceptional': 94, 'Rare': 95, 'Epic': 96, 'Legendary': 97, 'Mythic': 99},
    'Uncommon': {'Common': 60, 'Uncommon': 90, 'Exceptional': 92, 'Rare': 94, 'Epic': 95, 'Legendary': 97, 'Mythic': 99},
    'Exceptional': {'Common': 45, 'Uncommon': 65, 'Exceptional': 90, 'Rare': 92, 'Epic': 94, 'Legendary': 96, 'Mythic': 99},
    'Rare': {'Common': 30, 'Uncommon': 45, 'Exceptional': 60, 'Rare': 90, 'Epic': 95, 'Legendary': 97, 'Mythic': 99},
    'Epic': {'Common': 15, 'Uncommon': 30, 'Exceptional': 50, 'Rare': 70, 'Epic': 90, 'Legendary': 95, 'Mythic': 97},
    'Legendary': {'Common': 10, 'Uncommon': 20, 'Exceptional': 40, 'Rare': 60, 'Epic': 75, 'Legendary': 90, 'Mythic': 95},
    'Mythic': {'Common': 5, 'Uncommon': 10, 'Exceptional': 20, 'Rare': 40, 'Epic': 60, 'Legendary': 75, 'Mythic': 90}
}

SLOT_EMOJIS = ['1️⃣', '2️⃣', '3️⃣']
MAX_PLAYER_MISSIONS = 3
BOARD_VISIBLE_SLOTS = 3
BOARD_TOTAL_SLOTS = 10

def get_success_rate(mission_rarity: str, card_rarity: str) -> int:
    """Get success rate based on mission rarity vs card rarity"""
    return SUCCESS_RATE_MATRIX.get(mission_rarity, {}).get(card_rarity, 50)

def format_success_rates_for_mission(mission_rarity: str) -> str:
    """Format success rates for display in mission embed"""
    rates = SUCCESS_RATE_MATRIX.get(mission_rarity, {})
    return " | ".join([f"{r[:3]} {rates.get(r, 50)}%" for r in RARITY_HIERARCHY])

class MissionCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db_pool: asyncpg.Pool = bot.db_pool
        self.backlog_refill_loop.start()
        self.mission_lifecycle_loop.start()

    def cog_unload(self):
        self.backlog_refill_loop.cancel()
        self.mission_lifecycle_loop.cancel()

    @tasks.loop(minutes=5)
    async def backlog_refill_loop(self):
        """Refill mission board backlogs every 30 minutes"""
        try:
            now = datetime.now(timezone.utc)
            if now.minute in [0, 30]:
                await self.refill_all_mission_boards()
        except Exception as e:
            print(f"Backlog refill loop error: {e}")

    @backlog_refill_loop.before_loop
    async def before_backlog_refill(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=5)
    async def mission_lifecycle_loop(self):
        """Handle mission expiration and completion"""
        try:
            await self.process_mission_lifecycle()
        except Exception as e:
            print(f"Mission lifecycle loop error: {e}")

    @mission_lifecycle_loop.before_loop
    async def before_lifecycle_check(self):
        await self.bot.wait_until_ready()

    async def refill_all_mission_boards(self):
        """Refill all deck mission boards to 10 slots"""
        async with self.db_pool.acquire() as conn:
            decks = await conn.fetch(
                """SELECT DISTINCT d.deck_id FROM decks d
                   JOIN mission_templates mt ON d.deck_id = mt.deck_id
                   WHERE mt.is_active = TRUE"""
            )
            
            for deck in decks:
                await self.refill_mission_board(conn, deck['deck_id'])

    async def refill_mission_board(self, conn, deck_id: int):
        """Refill a deck's mission board to 10 slots"""
        current_slots = await conn.fetch(
            """SELECT slot_position FROM mission_board_slots WHERE deck_id = $1""",
            deck_id
        )
        existing_positions = {s['slot_position'] for s in current_slots}
        
        templates = await conn.fetch(
            """SELECT * FROM mission_templates WHERE deck_id = $1 AND is_active = TRUE""",
            deck_id
        )
        
        if not templates:
            return
        
        for position in range(1, BOARD_TOTAL_SLOTS + 1):
            if position not in existing_positions:
                await self.generate_mission_slot(conn, deck_id, position, templates)
        
        await conn.execute(
            "UPDATE decks SET last_mission_refill = $1 WHERE deck_id = $2",
            datetime.now(timezone.utc), deck_id
        )

    async def generate_mission_slot(self, conn, deck_id: int, position: int, templates: List):
        """Generate a new mission for a specific board slot"""
        template = random.choice(templates)
        
        total_weight = sum(RARITY_WEIGHTS.values())
        roll = random.uniform(0, total_weight)
        cumulative = 0
        selected_rarity = 'Common'
        for rarity in RARITY_HIERARCHY:
            cumulative += RARITY_WEIGHTS[rarity]
            if roll <= cumulative:
                selected_rarity = rarity
                break
        
        scaling = await conn.fetchrow(
            """SELECT * FROM mission_rarity_scaling 
               WHERE mission_template_id = $1 AND rarity = $2""",
            template['mission_template_id'], selected_rarity
        )
        
        if not scaling:
            return
        
        variance = template['variance_pct'] / 100.0
        
        base_req = template['min_value_base'] * scaling['requirement_multiplier']
        req_variance = base_req * random.uniform(-variance, variance)
        requirement_rolled = max(1, base_req + req_variance)
        
        base_reward = template['reward_base'] * scaling['reward_multiplier']
        reward_variance = base_reward * random.uniform(-variance, variance)
        reward_rolled = max(1, int(base_reward + reward_variance))
        
        base_duration = template['duration_base_hours'] * scaling['duration_multiplier']
        dur_variance = base_duration * random.uniform(-variance, variance)
        duration_rolled = max(1, int(base_duration + dur_variance))
        
        await conn.execute(
            """INSERT INTO mission_board_slots 
               (deck_id, mission_template_id, slot_position, rarity_rolled, 
                requirement_rolled, reward_rolled, duration_rolled_hours)
               VALUES ($1, $2, $3, $4, $5, $6, $7)
               ON CONFLICT (deck_id, slot_position) DO UPDATE SET
                   mission_template_id = $2, rarity_rolled = $4,
                   requirement_rolled = $5, reward_rolled = $6,
                   duration_rolled_hours = $7, created_at = NOW()""",
            deck_id, template['mission_template_id'], position,
            selected_rarity, requirement_rolled, reward_rolled, duration_rolled
        )

    @commands.hybrid_command(name='missionboard', description="View the mission board for this server's deck")
    async def mission_board(self, ctx):
        """Display the mission board with 3 available missions"""
        if ctx.interaction:
            await ctx.defer()
        
        guild_id = ctx.guild.id if ctx.guild else None
        if not guild_id:
            await ctx.send("This command can only be used in a server!")
            return
        
        async with self.db_pool.acquire() as conn:
            deck = await self.bot.get_server_deck(guild_id)
            if not deck:
                await ctx.send("This server doesn't have a deck assigned! An admin needs to set one up first.")
                return
            
            deck_id = deck['deck_id']
            
            current_slots = await conn.fetch(
                """SELECT slot_position FROM mission_board_slots WHERE deck_id = $1""",
                deck_id
            )
            
            if len(current_slots) < BOARD_TOTAL_SLOTS:
                templates = await conn.fetch(
                    """SELECT * FROM mission_templates WHERE deck_id = $1 AND is_active = TRUE""",
                    deck_id
                )
                if templates:
                    await self.refill_mission_board(conn, deck_id)
            
            missions = await conn.fetch(
                """SELECT mbs.*, mt.name as template_name, mt.description, mt.requirement_field
                   FROM mission_board_slots mbs
                   JOIN mission_templates mt ON mbs.mission_template_id = mt.mission_template_id
                   WHERE mbs.deck_id = $1 AND mbs.slot_position <= $2
                   ORDER BY mbs.slot_position""",
                deck_id, BOARD_VISIBLE_SLOTS
            )
            
            if not missions:
                templates = await conn.fetch(
                    """SELECT * FROM mission_templates WHERE deck_id = $1 AND is_active = TRUE""",
                    deck_id
                )
                if not templates:
                    await ctx.send("No mission templates configured for this deck! The deck creator needs to add some via the web portal.")
                    return
                await ctx.send("The mission board is being set up. Please try again in a moment!")
                return
            
            player_missions = await conn.fetchval(
                """SELECT COUNT(*) FROM active_missions 
                   WHERE accepted_by = $1 AND status IN ('pending', 'active')""",
                ctx.author.id
            )
            slots_available = MAX_PLAYER_MISSIONS - player_missions
            
            embed = discord.Embed(
                title=f"🎯 Mission Board - {deck['name']}",
                description=f"React with 1️⃣ 2️⃣ 3️⃣ to accept a mission!\n\n"
                           f"**Your Mission Slots:** {player_missions}/{MAX_PLAYER_MISSIONS} used",
                color=0x667EEA
            )
            
            for i, mission in enumerate(missions):
                if i >= 3:
                    break
                    
                emoji = SLOT_EMOJIS[i]
                rarity = mission['rarity_rolled']
                color_indicator = {'Common': '⚪', 'Uncommon': '🟢', 'Exceptional': '🔵', 
                                   'Rare': '🟣', 'Epic': '💜', 'Legendary': '🟠', 'Mythic': '🔴'}
                
                acceptance_cost = int(mission['reward_rolled'] * 0.05)
                
                field_value = (
                    f"{color_indicator.get(rarity, '⚪')} **{rarity}**\n"
                    f"📋 {mission['requirement_field']} >= {mission['requirement_rolled']:,.0f}\n"
                    f"💰 Reward: {mission['reward_rolled']:,} credits\n"
                    f"⏱️ Duration: {mission['duration_rolled_hours']}h\n"
                    f"🎫 Cost: {acceptance_cost} credits"
                )
                
                embed.add_field(
                    name=f"{emoji} {mission['template_name']}",
                    value=field_value,
                    inline=True
                )
            
            embed.set_footer(text=f"Missions refresh every 30 minutes | Slot ID: {deck_id}")
            embed.timestamp = datetime.now(timezone.utc)
            
            message = await ctx.send(embed=embed)
            
            for i in range(min(len(missions), 3)):
                await message.add_reaction(SLOT_EMOJIS[i])
            
            await conn.execute(
                """INSERT INTO mission_board_messages (guild_id, deck_id, channel_id, message_id)
                   VALUES ($1, $2, $3, $4)
                   ON CONFLICT (guild_id, deck_id) DO UPDATE SET
                       channel_id = $3, message_id = $4, updated_at = NOW()""",
                guild_id, deck_id, ctx.channel.id, message.id
            )

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Handle mission selection via 1️⃣ 2️⃣ 3️⃣ reactions"""
        if payload.user_id == self.bot.user.id:
            return
        
        emoji_str = str(payload.emoji)
        if emoji_str not in SLOT_EMOJIS:
            return
        
        slot_index = SLOT_EMOJIS.index(emoji_str)
        
        async with self.db_pool.acquire() as conn:
            board_msg = await conn.fetchrow(
                """SELECT * FROM mission_board_messages 
                   WHERE message_id = $1 AND guild_id = $2""",
                payload.message_id, payload.guild_id
            )
            
            if not board_msg:
                return
            
            deck_id = board_msg['deck_id']
            
            player_missions = await conn.fetchval(
                """SELECT COUNT(*) FROM active_missions 
                   WHERE accepted_by = $1 AND status IN ('pending', 'active')""",
                payload.user_id
            )
            
            if player_missions >= MAX_PLAYER_MISSIONS:
                try:
                    user = self.bot.get_user(payload.user_id)
                    if user:
                        await user.send(
                            f"❌ **Unable to Accept Mission**\n"
                            f"You don't have enough mission slots available. "
                            f"You have {player_missions}/{MAX_PLAYER_MISSIONS} missions active.\n"
                            f"Complete or abandon a mission first!"
                        )
                    channel = self.bot.get_channel(payload.channel_id)
                    if channel:
                        message = await channel.fetch_message(payload.message_id)
                        await message.remove_reaction(emoji_str, discord.Object(id=payload.user_id))
                except:
                    pass
                return
            
            missions = await conn.fetch(
                """SELECT mbs.*, mt.name as template_name, mt.requirement_field
                   FROM mission_board_slots mbs
                   JOIN mission_templates mt ON mbs.mission_template_id = mt.mission_template_id
                   WHERE mbs.deck_id = $1 AND mbs.slot_position <= $2
                   ORDER BY mbs.slot_position""",
                deck_id, BOARD_VISIBLE_SLOTS
            )
            
            if slot_index >= len(missions):
                return
            
            mission = missions[slot_index]
            
            # Get player's deck-specific credits
            state = await get_player_deck_state(conn, payload.user_id, deck_id)
            current_credits = state['credits']
            
            acceptance_cost = int(mission['reward_rolled'] * 0.05)
            
            if current_credits < acceptance_cost:
                try:
                    user = self.bot.get_user(payload.user_id)
                    if user:
                        await user.send(
                            f"❌ **Unable to Accept Mission**\n"
                            f"You need **{acceptance_cost}** credits to accept this mission, "
                            f"but you only have **{current_credits}** credits."
                        )
                    channel = self.bot.get_channel(payload.channel_id)
                    if channel:
                        message = await channel.fetch_message(payload.message_id)
                        await message.remove_reaction(emoji_str, discord.Object(id=payload.user_id))
                except:
                    pass
                return
            
            try:
                has_qualifying_card = await conn.fetchval(
                    """SELECT COUNT(*) FROM user_cards uc
                       JOIN cards c ON uc.card_id = c.card_id
                       JOIN card_template_fields ctf ON c.card_id = ctf.card_id
                       JOIN card_templates ct ON ctf.template_id = ct.template_id
                       LEFT JOIN user_card_field_overrides ucfo ON uc.instance_id = ucfo.instance_id 
                           AND ct.template_id = ucfo.template_id
                       WHERE uc.user_id = $1 AND uc.recycled_at IS NULL
                       AND ct.field_name = $2 AND ct.field_type = 'number'
                       AND ctf.field_value ~ '^[0-9.]+$'
                       AND COALESCE(ucfo.effective_numeric_value, CAST(ctf.field_value AS FLOAT)) >= $3""",
                    payload.user_id, mission['requirement_field'], mission['requirement_rolled']
                )
            except Exception as e:
                print(f"Error checking qualifying card: {e}")
                has_qualifying_card = 0
            
            if not has_qualifying_card:
                try:
                    user = self.bot.get_user(payload.user_id)
                    if user:
                        await user.send(
                            f"❌ **Unable to Accept Mission**\n"
                            f"You don't have a card with **{mission['requirement_field']}** >= "
                            f"**{mission['requirement_rolled']:,.0f}**.\n"
                            f"Collect or merge cards to meet this requirement!"
                        )
                    channel = self.bot.get_channel(payload.channel_id)
                    if channel:
                        message = await channel.fetch_message(payload.message_id)
                        await message.remove_reaction(emoji_str, discord.Object(id=payload.user_id))
                except:
                    pass
                return
            
            now = datetime.now(timezone.utc)
            mission_expires = now + timedelta(days=1)
            
            try:
                async with conn.transaction():
                    # Deduct acceptance cost from deck-specific credits
                    await update_player_credits(conn, payload.user_id, deck_id, -acceptance_cost)
                    
                    result = await conn.fetchrow(
                        """INSERT INTO active_missions 
                           (mission_template_id, guild_id, deck_id, spawned_at,
                            mission_expires_at, status, rarity_rolled, requirement_rolled,
                            reward_rolled, duration_rolled_hours, accepted_by, accepted_at,
                            board_slot_id)
                           VALUES ($1, $2, $3, $4, $5, 'active', $6, $7, $8, $9, $10, $11, $12)
                           RETURNING active_mission_id""",
                        mission['mission_template_id'], payload.guild_id, deck_id,
                        now, mission_expires, mission['rarity_rolled'], mission['requirement_rolled'],
                        mission['reward_rolled'], mission['duration_rolled_hours'],
                        payload.user_id, now, mission['slot_id']
                    )
                    
                    mission_id = result['active_mission_id']
                    
                    await conn.execute(
                        """INSERT INTO user_missions 
                           (user_id, guild_id, active_mission_id, status, acceptance_cost, accepted_at)
                           VALUES ($1, $2, $3, 'active', $4, $5)""",
                        payload.user_id, payload.guild_id, mission_id, acceptance_cost, now
                    )
                    
                    await conn.execute(
                        "DELETE FROM mission_board_slots WHERE slot_id = $1",
                        mission['slot_id']
                    )
                    
                    removed_position = mission['slot_position']
                    await conn.execute(
                        """UPDATE mission_board_slots 
                           SET slot_position = slot_position - 1
                           WHERE deck_id = $1 AND slot_position > $2""",
                        deck_id, removed_position
                    )
                    
                    templates = await conn.fetch(
                        """SELECT * FROM mission_templates WHERE deck_id = $1 AND is_active = TRUE""",
                        deck_id
                    )
                    if templates:
                        await self.generate_mission_slot(conn, deck_id, BOARD_TOTAL_SLOTS, templates)
                    
            except Exception as e:
                print(f"Error accepting mission: {e}")
                import traceback
                traceback.print_exc()
                return
            
            try:
                user = await self.bot.fetch_user(payload.user_id)
                if user:
                    await user.send(
                        f"✅ **Mission Accepted!** {mission['template_name']} [{mission['rarity_rolled']}]\n\n"
                        f"💰 **Cost:** {acceptance_cost} credits deducted\n"
                        f"📋 **Next Step:** Use `/startmission` within 24 hours to begin!\n"
                        f"⏱️ **Mission Duration:** {mission['duration_rolled_hours']} hours\n\n"
                        f"📊 **Your Missions:** {player_missions + 1}/{MAX_PLAYER_MISSIONS}"
                    )
            except Exception as e:
                print(f"Error sending acceptance DM: {e}")
            
            try:
                channel = self.bot.get_channel(payload.channel_id)
                if channel:
                    message = await channel.fetch_message(payload.message_id)
                    await message.remove_reaction(emoji_str, discord.Object(id=payload.user_id))
            except:
                pass

    @commands.hybrid_command(name='startmission', description="Start an accepted mission with a qualifying card")
    @app_commands.describe(
        mission_name="The mission to start",
        card_name="The card to use for the mission"
    )
    async def start_mission(self, ctx, mission_name: str, card_name: str):
        """Start an accepted mission using a qualifying card"""
        if ctx.interaction:
            await ctx.defer()
        
        user_id = ctx.author.id
        guild_id = ctx.guild.id if ctx.guild else None
        
        if not guild_id:
            await ctx.send("This command can only be used in a server!")
            return
        
        actual_card_name = card_name
        target_merge_level = None
        if '|' in card_name:
            parts = card_name.rsplit('|', 1)
            actual_card_name = parts[0]
            try:
                target_merge_level = int(parts[1])
            except ValueError:
                pass
        
        target_mission_id = None
        if '|' in mission_name:
            parts = mission_name.rsplit('|', 1)
            try:
                target_mission_id = int(parts[1])
            except ValueError:
                pass
        
        async with self.db_pool.acquire() as conn:
            if target_mission_id:
                mission = await conn.fetchrow(
                    """SELECT am.*, mt.name as template_name, mt.requirement_field,
                              mrs.success_rate
                       FROM active_missions am
                       JOIN mission_templates mt ON am.mission_template_id = mt.mission_template_id
                       JOIN mission_rarity_scaling mrs ON mt.mission_template_id = mrs.mission_template_id
                       WHERE am.active_mission_id = $1 AND am.accepted_by = $2
                       AND am.status = 'active' AND am.started_at IS NULL
                       AND mrs.rarity = am.rarity_rolled""",
                    target_mission_id, user_id
                )
            else:
                mission = await conn.fetchrow(
                    """SELECT am.*, mt.name as template_name, mt.requirement_field,
                              mrs.success_rate
                       FROM active_missions am
                       JOIN mission_templates mt ON am.mission_template_id = mt.mission_template_id
                       JOIN mission_rarity_scaling mrs ON mt.mission_template_id = mrs.mission_template_id
                       WHERE am.accepted_by = $1 AND am.status = 'active'
                       AND am.started_at IS NULL
                       AND mrs.rarity = am.rarity_rolled
                       ORDER BY am.accepted_at DESC
                       LIMIT 1""",
                    user_id
                )
            
            if not mission:
                await ctx.send("You don't have any accepted missions waiting to start! Use `/missionboard` to accept one.")
                return
            
            if target_merge_level is not None:
                qualifying_card = await conn.fetchrow(
                    """SELECT uc.instance_id, uc.card_id, c.name, c.rarity,
                              uc.merge_level, 
                              COALESCE(ucfo.effective_numeric_value, CAST(ctf.field_value AS FLOAT)) as effective_value
                       FROM user_cards uc
                       JOIN cards c ON uc.card_id = c.card_id
                       JOIN card_template_fields ctf ON c.card_id = ctf.card_id
                       JOIN card_templates ct ON ctf.template_id = ct.template_id
                       LEFT JOIN user_card_field_overrides ucfo ON uc.instance_id = ucfo.instance_id 
                           AND ct.template_id = ucfo.template_id
                       WHERE uc.user_id = $1 AND uc.recycled_at IS NULL
                       AND LOWER(c.name) = LOWER($2)
                       AND uc.merge_level = $5
                       AND ct.field_name = $3 AND ct.field_type = 'number'
                       AND ctf.field_value ~ '^[0-9.]+$'
                       AND COALESCE(ucfo.effective_numeric_value, CAST(ctf.field_value AS FLOAT)) >= $4
                       AND uc.instance_id NOT IN (
                           SELECT card_instance_id FROM active_missions 
                           WHERE status = 'active' AND started_at IS NOT NULL 
                           AND card_instance_id IS NOT NULL
                       )
                       LIMIT 1""",
                    user_id, actual_card_name, mission['requirement_field'], mission['requirement_rolled'], target_merge_level
                )
            else:
                qualifying_card = await conn.fetchrow(
                    """SELECT uc.instance_id, uc.card_id, c.name, c.rarity,
                              uc.merge_level,
                              COALESCE(ucfo.effective_numeric_value, CAST(ctf.field_value AS FLOAT)) as effective_value
                       FROM user_cards uc
                       JOIN cards c ON uc.card_id = c.card_id
                       JOIN card_template_fields ctf ON c.card_id = ctf.card_id
                       JOIN card_templates ct ON ctf.template_id = ct.template_id
                       LEFT JOIN user_card_field_overrides ucfo ON uc.instance_id = ucfo.instance_id 
                           AND ct.template_id = ucfo.template_id
                       WHERE uc.user_id = $1 AND uc.recycled_at IS NULL
                       AND LOWER(c.name) = LOWER($2)
                       AND ct.field_name = $3 AND ct.field_type = 'number'
                       AND ctf.field_value ~ '^[0-9.]+$'
                       AND COALESCE(ucfo.effective_numeric_value, CAST(ctf.field_value AS FLOAT)) >= $4
                       AND uc.instance_id NOT IN (
                           SELECT card_instance_id FROM active_missions 
                           WHERE status = 'active' AND started_at IS NOT NULL 
                           AND card_instance_id IS NOT NULL
                       )
                       ORDER BY uc.merge_level DESC
                       LIMIT 1""",
                    user_id, actual_card_name, mission['requirement_field'], mission['requirement_rolled']
                )
            
            if not qualifying_card:
                await ctx.send(
                    f"**{card_name}** doesn't qualify for this mission!\n"
                    f"Need: {mission['requirement_field']} >= {mission['requirement_rolled']:,.0f}"
                )
                return
            
            now = datetime.now(timezone.utc)
            mission_end = now + timedelta(hours=mission['duration_rolled_hours'])
            
            card_rarity = qualifying_card['rarity']
            mission_rarity = mission['rarity_rolled']
            
            base_success_rate = get_success_rate(mission_rarity, card_rarity)
            merge_bonus = qualifying_card['merge_level'] * 5
            final_success_rate = min(99, base_success_rate + merge_bonus)
            
            success_roll = random.uniform(0, 100)
            
            async with conn.transaction():
                await conn.execute(
                    """UPDATE active_missions 
                       SET status = 'active', started_at = $1, 
                           mission_expires_at = $2, card_instance_id = $3,
                           success_roll = $4
                       WHERE active_mission_id = $5""",
                    now, mission_end, qualifying_card['instance_id'],
                    success_roll, mission['active_mission_id']
                )
                
                await conn.execute(
                    """UPDATE user_missions 
                       SET status = 'active', started_at = $1, card_instance_id = $2
                       WHERE active_mission_id = $3 AND user_id = $4""",
                    now, qualifying_card['instance_id'], mission['active_mission_id'], user_id
                )
            
            embed = discord.Embed(
                title=f"🚀 Mission Started!",
                description=f"**{mission['template_name']}** [{mission['rarity_rolled']}]",
                color=0x667EEA
            )
            
            embed.add_field(
                name="Card Used",
                value=f"**{qualifying_card['name']}** [{card_rarity}]" + 
                      (f" ★{qualifying_card['merge_level']}" if qualifying_card['merge_level'] > 0 else ""),
                inline=True
            )
            
            embed.add_field(
                name="Success Chance",
                value=f"**{final_success_rate:.0f}%**" + 
                      (f" (+{merge_bonus}% merge bonus)" if merge_bonus > 0 else ""),
                inline=True
            )
            
            embed.add_field(
                name="Completion Time",
                value=f"<t:{int(mission_end.timestamp())}:R>",
                inline=True
            )
            
            embed.add_field(
                name="Potential Reward",
                value=f"**{mission['reward_rolled']:,}** credits",
                inline=True
            )
            
            embed.set_footer(text="Your card is now locked until the mission completes!")
            
            await ctx.send(embed=embed)

    @start_mission.autocomplete('mission_name')
    async def mission_name_autocomplete(self, interaction: discord.Interaction, current: str):
        """Autocomplete for mission names"""
        user_id = interaction.user.id
        
        async with self.db_pool.acquire() as conn:
            missions = await conn.fetch(
                """SELECT am.active_mission_id, mt.name, am.rarity_rolled, am.reward_rolled
                   FROM active_missions am
                   JOIN mission_templates mt ON am.mission_template_id = mt.mission_template_id
                   WHERE am.accepted_by = $1 AND am.status = 'active' AND am.started_at IS NULL
                   AND LOWER(mt.name) LIKE LOWER($2)
                   ORDER BY am.accepted_at DESC
                   LIMIT 25""",
                user_id, f"%{current}%"
            )
            
            choices = []
            for m in missions:
                display = f"{m['name']} [{m['rarity_rolled']}] ({m['reward_rolled']:,}cr)"
                value = f"{m['name']}|{m['active_mission_id']}"
                choices.append(app_commands.Choice(name=display[:100], value=value))
            
            return choices

    @start_mission.autocomplete('card_name')
    async def card_name_autocomplete(self, interaction: discord.Interaction, current: str):
        """Autocomplete for card names that qualify for the mission"""
        user_id = interaction.user.id
        guild_id = interaction.guild.id if interaction.guild else None
        
        if not guild_id:
            return []
        
        mission_name = None
        for option in interaction.data.get('options', []):
            if option['name'] == 'mission_name':
                mission_name = option.get('value', '')
                break
        
        target_mission_id = None
        if mission_name and '|' in mission_name:
            parts = mission_name.rsplit('|', 1)
            try:
                target_mission_id = int(parts[1])
            except ValueError:
                pass
        
        async with self.db_pool.acquire() as conn:
            if target_mission_id:
                mission = await conn.fetchrow(
                    """SELECT am.*, mt.requirement_field
                       FROM active_missions am
                       JOIN mission_templates mt ON am.mission_template_id = mt.mission_template_id
                       WHERE am.active_mission_id = $1 AND am.accepted_by = $2 
                       AND am.status = 'active' AND am.started_at IS NULL""",
                    target_mission_id, user_id
                )
            else:
                mission = await conn.fetchrow(
                    """SELECT am.*, mt.requirement_field
                       FROM active_missions am
                       JOIN mission_templates mt ON am.mission_template_id = mt.mission_template_id
                       WHERE am.accepted_by = $1 
                       AND am.status = 'active' AND am.started_at IS NULL
                       ORDER BY am.accepted_at DESC
                       LIMIT 1""",
                    user_id
                )
            
            if not mission:
                return []
            
            cards = await conn.fetch(
                """SELECT DISTINCT c.name, c.rarity, uc.merge_level, 
                          COALESCE(ucfo.effective_numeric_value, CAST(ctf.field_value AS FLOAT)) as effective_value
                   FROM user_cards uc
                   JOIN cards c ON uc.card_id = c.card_id
                   JOIN card_template_fields ctf ON c.card_id = ctf.card_id
                   JOIN card_templates ct ON ctf.template_id = ct.template_id
                   LEFT JOIN user_card_field_overrides ucfo ON uc.instance_id = ucfo.instance_id 
                       AND ct.template_id = ucfo.template_id
                   WHERE uc.user_id = $1 AND uc.recycled_at IS NULL
                   AND ct.field_name = $2 AND ct.field_type = 'number'
                   AND ctf.field_value ~ '^[0-9.]+$'
                   AND COALESCE(ucfo.effective_numeric_value, CAST(ctf.field_value AS FLOAT)) >= $3
                   AND LOWER(c.name) LIKE LOWER($4)
                   AND uc.instance_id NOT IN (
                       SELECT card_instance_id FROM active_missions 
                       WHERE status = 'active' AND started_at IS NOT NULL 
                       AND card_instance_id IS NOT NULL
                   )
                   ORDER BY uc.merge_level DESC, c.name
                   LIMIT 25""",
                user_id, mission['requirement_field'], mission['requirement_rolled'],
                f"%{current}%"
            )
            
            choices = []
            for card in cards:
                merge_display = f" ★{card['merge_level']}" if card['merge_level'] > 0 else ""
                display = f"{card['name']}{merge_display} [{card['rarity']}] ({float(card['effective_value']):,.0f})"
                value = f"{card['name']}|{card['merge_level']}"
                choices.append(app_commands.Choice(name=display[:100], value=value))
            
            return choices

    async def process_mission_lifecycle(self):
        """Process mission completions and expirations"""
        now = datetime.now(timezone.utc)
        
        async with self.db_pool.acquire() as conn:
            expired_starts = await conn.fetch(
                """SELECT * FROM active_missions 
                   WHERE status = 'active' AND started_at IS NULL
                   AND mission_expires_at < $1""",
                now
            )
            
            for mission in expired_starts:
                await conn.execute(
                    "UPDATE active_missions SET status = 'expired' WHERE active_mission_id = $1",
                    mission['active_mission_id']
                )
                await conn.execute(
                    "UPDATE user_missions SET status = 'expired' WHERE active_mission_id = $1",
                    mission['active_mission_id']
                )
            
            completed_missions = await conn.fetch(
                """SELECT am.*, mt.name as template_name
                   FROM active_missions am
                   JOIN mission_templates mt ON am.mission_template_id = mt.mission_template_id
                   WHERE am.status = 'active' AND am.started_at IS NOT NULL 
                   AND am.mission_expires_at < $1""",
                now
            )
            
            for mission in completed_missions:
                try:
                    uc = await conn.fetchrow(
                        """SELECT uc.merge_level, c.rarity 
                           FROM user_cards uc
                           JOIN cards c ON uc.card_id = c.card_id
                           WHERE uc.instance_id = $1""",
                        mission['card_instance_id']
                    )
                    merge_level = uc['merge_level'] if uc else 0
                    card_rarity = uc['rarity'] if uc else 'Common'
                    mission_rarity = mission['rarity_rolled']
                    
                    base_success_rate = get_success_rate(mission_rarity, card_rarity)
                    merge_bonus = merge_level * 5
                    final_success_rate = min(99, base_success_rate + merge_bonus)
                    
                    success = mission['success_roll'] <= final_success_rate
                    
                    if success:
                        credits_earned = mission['reward_rolled']
                        credit_bonus = int(credits_earned * merge_level * 0.05)
                        total_credits = credits_earned + credit_bonus
                        
                        # Award credits to deck-specific balance
                        await update_player_credits(conn, mission['accepted_by'], mission['deck_id'], total_credits)
                        
                        await conn.execute(
                            """UPDATE active_missions 
                               SET status = 'completed', completed_at = $1
                               WHERE active_mission_id = $2""",
                            now, mission['active_mission_id']
                        )
                        
                        await conn.execute(
                            """UPDATE user_missions 
                               SET status = 'completed', completed_at = $1, credits_earned = $2
                               WHERE active_mission_id = $3""",
                            now, total_credits, mission['active_mission_id']
                        )
                        
                        try:
                            user = await self.bot.fetch_user(mission['accepted_by'])
                            guild = self.bot.get_guild(mission['guild_id'])
                            guild_name = guild.name if guild else "Unknown Server"
                            if user:
                                bonus_text = f" (+{credit_bonus:,} merge bonus)" if credit_bonus > 0 else ""
                                await user.send(
                                    f"🎉 Your mission, **{mission['template_name']}** [{mission['rarity_rolled']}], "
                                    f"has completed in **{guild_name}**. It was successful, and you have gained "
                                    f"**{total_credits:,}** credits!{bonus_text}"
                                )
                        except Exception as e:
                            print(f"Failed to send mission success DM: {e}")
                    else:
                        await conn.execute(
                            """UPDATE active_missions 
                               SET status = 'failed', completed_at = $1
                               WHERE active_mission_id = $2""",
                            now, mission['active_mission_id']
                        )
                        
                        await conn.execute(
                            """UPDATE user_missions 
                               SET status = 'failed', completed_at = $1
                               WHERE active_mission_id = $2""",
                            now, mission['active_mission_id']
                        )
                        
                        try:
                            user = await self.bot.fetch_user(mission['accepted_by'])
                            guild = self.bot.get_guild(mission['guild_id'])
                            guild_name = guild.name if guild else "Unknown Server"
                            if user:
                                await user.send(
                                    f"❌ Your mission, **{mission['template_name']}** [{mission['rarity_rolled']}], "
                                    f"has completed in **{guild_name}**. It was a failure."
                                )
                        except Exception as e:
                            print(f"Failed to send mission failure DM: {e}")
                            
                except Exception as e:
                    print(f"Error processing mission {mission['active_mission_id']}: {e}")

    @commands.hybrid_command(name='mymissions', description="View your active and pending missions")
    async def my_missions(self, ctx):
        """View your active missions"""
        if ctx.interaction:
            await ctx.defer()
        
        user_id = ctx.author.id
        
        async with self.db_pool.acquire() as conn:
            missions = await conn.fetch(
                """SELECT am.*, mt.name as template_name, mt.requirement_field,
                          c.name as card_name
                   FROM active_missions am
                   JOIN mission_templates mt ON am.mission_template_id = mt.mission_template_id
                   LEFT JOIN cards c ON am.card_instance_id IS NOT NULL 
                        AND EXISTS (SELECT 1 FROM user_cards uc WHERE uc.instance_id = am.card_instance_id AND uc.card_id = c.card_id)
                   WHERE am.accepted_by = $1 AND am.status IN ('pending', 'active')
                   ORDER BY am.started_at DESC NULLS LAST, am.accepted_at DESC
                   LIMIT 10""",
                user_id
            )
            
            if not missions:
                await ctx.send("📋 You don't have any active missions. Use `/missionboard` to accept one!")
                return
            
            embed = discord.Embed(
                title="📋 Your Missions",
                description=f"**Slots Used:** {len(missions)}/{MAX_PLAYER_MISSIONS}",
                color=0x667EEA
            )
            
            for m in missions:
                if m['started_at'] is None:
                    status = "⏳ Accepted (use /startmission)"
                    time_label = "Expires"
                    expires = m['mission_expires_at']
                else:
                    status = "🚀 In Progress"
                    time_label = "Completes"
                    expires = m['mission_expires_at']
                
                value = f"**ID:** {m['active_mission_id']}\n"
                value += f"**Status:** {status}\n"
                value += f"**Rarity:** {m['rarity_rolled']}\n"
                value += f"**Reward:** {m['reward_rolled']:,} credits\n"
                if expires:
                    value += f"**{time_label}:** <t:{int(expires.timestamp())}:R>"
                
                embed.add_field(
                    name=f"{m['template_name']}",
                    value=value,
                    inline=False
                )
            
            await ctx.send(embed=embed)

    def is_admin(self, user_id: int) -> bool:
        """Check if user is an admin"""
        admin_ids = getattr(self.bot, 'admin_ids', [])
        return user_id in admin_ids or user_id == self.bot.owner_id

    @commands.command(name='refillboard')
    async def refill_board(self, ctx):
        """[ADMIN] Manually refill the mission board"""
        if not self.is_admin(ctx.author.id):
            await ctx.send("This command is only available to DeckForge admins.")
            return
        
        guild_id = ctx.guild.id if ctx.guild else None
        if not guild_id:
            await ctx.send("This command can only be used in a server!")
            return
        
        async with self.db_pool.acquire() as conn:
            deck = await self.bot.get_server_deck(guild_id)
            if not deck:
                await ctx.send("This server doesn't have a deck assigned!")
                return
            
            await self.refill_mission_board(conn, deck['deck_id'])
            await ctx.send(f"✅ Mission board refilled for deck **{deck['name']}**!")

    @commands.command(name='mcomplete')
    async def force_complete_mission(self, ctx, mission_id: int):
        """
        [ADMIN] Set a mission to complete in 10 seconds for natural completion testing.
        Usage: !mcomplete <mission_id>
        """
        if not self.is_admin(ctx.author.id):
            await ctx.send("This command is only available to DeckForge admins.")
            return
        
        now = datetime.now(timezone.utc)
        expires_soon = now + timedelta(seconds=10)
        
        async with self.db_pool.acquire() as conn:
            mission = await conn.fetchrow(
                """SELECT am.*, mt.name as template_name
                   FROM active_missions am
                   JOIN mission_templates mt ON am.mission_template_id = mt.mission_template_id
                   WHERE am.active_mission_id = $1""",
                mission_id
            )
            
            if not mission:
                await ctx.send(f"Mission #{mission_id} not found!")
                return
            
            if mission['status'] not in ('pending', 'active'):
                await ctx.send(f"Mission #{mission_id} is already {mission['status']}!")
                return
            
            await conn.execute(
                """UPDATE active_missions 
                   SET mission_expires_at = $1
                   WHERE active_mission_id = $2""",
                expires_soon, mission_id
            )
            
            await ctx.send(
                f"⏱️ Mission #{mission_id} (**{mission['template_name']}**) will complete naturally in **10 seconds**.\n"
                f"The lifecycle loop runs every 5 minutes, so check back shortly or wait for the DM!"
            )


async def setup(bot):
    await bot.add_cog(MissionCommands(bot))
