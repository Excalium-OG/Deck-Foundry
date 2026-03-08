"""
DeckForge Card Merge System Cog
Handles card merging with perk progression and diminishing returns
"""
import discord
from discord.ext import commands
from discord import app_commands
import asyncpg
import uuid
import json
from typing import Optional, List

from utils.merge_helpers import (
    calculate_merge_cost,
    calculate_perk_boost,
    calculate_cumulative_perk_boost,
    get_merge_perks_for_deck,
    get_card_perk_history,
    validate_merge_eligibility,
    format_merge_level_display
)
from utils.card_helpers import get_player_deck_state, update_player_credits


class MergeCommands(commands.Cog):
    """Cog for card merging operations"""
    
    def __init__(self, bot):
        self.bot = bot
        self.db_pool: asyncpg.Pool = bot.db_pool
    
    async def card_name_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> List[app_commands.Choice[str]]:
        """
        Autocomplete for card names - shows cards the player has 2+ of
        Groups by card_id, merge_level, and locked_perk to find truly mergeable pairs
        """
        user_id = interaction.user.id
        guild_id = interaction.guild_id if interaction.guild else None
        
        if not guild_id:
            return []
        
        # Get server's deck
        deck = await self.bot.get_server_deck(guild_id)
        if not deck:
            return []
        
        deck_id = deck['deck_id']
        
        locked_list = list(getattr(self.bot, 'pvp_locked_cards', set()))
        async with self.db_pool.acquire() as conn:
            # Get cards the player has 2+ of at the same merge level AND locked perk
            # For level 0 cards, locked_perk will be NULL and they can merge together
            # For level 1+, locked_perk must match
            mergeable_cards = await conn.fetch(
                """
                SELECT 
                    c.card_id,
                    c.name,
                    uc.merge_level,
                    uc.locked_perk,
                    COUNT(*) as count
                FROM user_cards uc
                JOIN cards c ON uc.card_id = c.card_id
                WHERE uc.user_id = $1 
                  AND c.deck_id = $2
                  AND uc.recycled_at IS NULL
                  AND c.mergeable = TRUE
                  AND NOT (uc.instance_id::text = ANY($3::text[]))
                GROUP BY c.card_id, c.name, uc.merge_level, uc.locked_perk
                HAVING COUNT(*) >= 2
                ORDER BY c.name, uc.merge_level
                """,
                user_id, deck_id, locked_list
            )
            
            # Build choices with merge level and perk indicator
            choices = []
            for card in mergeable_cards:
                card_name = card['name']
                merge_level = card['merge_level']
                locked_perk = card['locked_perk']
                count = card['count']
                
                # Add merge level indicator to display
                display_level = format_merge_level_display(merge_level)
                
                # Add perk indicator if level > 0
                if merge_level > 0 and locked_perk:
                    display_name = f"{card_name} {display_level} [{locked_perk}] (x{count})"
                    # Store card_name|merge_level|perk for lookup
                    value = f"{card_name}|{merge_level}|{locked_perk}"
                else:
                    display_name = f"{card_name} {display_level} (x{count})"
                    # Store card_name|merge_level for level 0 cards
                    value = f"{card_name}|{merge_level}|"
                
                # Filter based on current input
                if current.lower() in card_name.lower():
                    choices.append(app_commands.Choice(name=display_name, value=value))
            
            # Return max 25 choices (Discord limit)
            return choices[:25]
    
    async def perk_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> List[app_commands.Choice[str]]:
        """Autocomplete for perk names - shows available perks for the deck"""
        guild_id = interaction.guild_id if interaction.guild else None
        
        if not guild_id:
            return []
        
        # Get server's deck
        deck = await self.bot.get_server_deck(guild_id)
        if not deck:
            return []
        
        deck_id = deck['deck_id']
        
        async with self.db_pool.acquire() as conn:
            # Get available perks for this deck
            perks = await conn.fetch(
                """
                SELECT perk_name, base_boost
                FROM deck_merge_perks
                WHERE deck_id = $1
                ORDER BY perk_name
                """,
                deck_id
            )
            
            choices = []
            for perk in perks:
                perk_name = perk['perk_name']
                base_boost = perk['base_boost']
                
                # Filter based on current input
                if current.lower() in perk_name.lower():
                    display_name = f"{perk_name} (+{base_boost})"
                    choices.append(app_commands.Choice(name=display_name, value=perk_name))
            
            return choices[:25]
    
    @commands.hybrid_command(name='merge')
    @app_commands.describe(
        card_name='The card to merge (you need 2+ at the same level)',
        perk='For first merge only - the perk to lock for future merges'
    )
    @app_commands.autocomplete(card_name=card_name_autocomplete, perk=perk_autocomplete)
    async def merge_cards(self, ctx, card_name: str, perk: Optional[str] = None):
        """
        Merge two cards of the same type and level to create a more powerful card
        
        Args:
            card_name: Name of the card to merge (with autocomplete support)
            perk: For first merge only - the perk to lock for future merges
        
        Usage:
            /merge <card_name>
            /merge <card_name> <perk_name>
        """
        # Defer for slash commands
        if ctx.interaction:
            await ctx.defer()
        
        user_id = ctx.author.id
        guild_id = ctx.guild.id if ctx.guild else None
        
        if not guild_id:
            await ctx.send("❌ This command can only be used in a server!")
            return
        
        # Check if server has an assigned deck
        deck = await self.bot.get_server_deck(guild_id)
        if not deck:
            await ctx.send(
                "❌ No deck assigned to this server!\n"
                "Ask a server manager to assign a deck via the web admin portal."
            )
            return
        
        deck_id = deck['deck_id']
        
        # Parse card_name|merge_level|locked_perk from autocomplete value
        # Format: "card_name|merge_level|locked_perk" (locked_perk is empty string for level 0)
        target_merge_level = None
        target_locked_perk = None
        
        if '|' in card_name:
            parts = card_name.rsplit('|', 2)
            if len(parts) == 3:
                actual_card_name, merge_level_str, perk_str = parts
                try:
                    target_merge_level = int(merge_level_str)
                    target_locked_perk = perk_str if perk_str else None
                except ValueError:
                    actual_card_name = card_name
            else:
                actual_card_name = card_name
        else:
            actual_card_name = card_name
        
        async with self.db_pool.acquire() as conn:
            # Get the card info
            card_info = await conn.fetchrow(
                """
                SELECT card_id, name, rarity, mergeable, max_merge_level
                FROM cards
                WHERE deck_id = $1 AND LOWER(name) = LOWER($2)
                """,
                deck_id, actual_card_name
            )
            
            if not card_info:
                await ctx.send(f"❌ Card **{actual_card_name}** not found in this deck!")
                return
            
            if not card_info['mergeable']:
                await ctx.send(f"❌ **{card_info['name']}** is not a mergeable card!")
                return
            
            card_id = card_info['card_id']
            card_name_display = card_info['name']
            rarity = card_info['rarity']
            max_merge_level = card_info['max_merge_level']
            
            # Find all instances of this card the player owns
            # CRITICAL: Must match both merge_level AND locked_perk to avoid consuming mismatched perks
            if target_merge_level is not None:
                # Autocomplete provided specific merge level and locked perk
                if target_locked_perk is not None:
                    # Level 1+ with locked perk
                    instances = await conn.fetch(
                        """
                        SELECT instance_id, merge_level, locked_perk
                        FROM user_cards
                        WHERE user_id = $1 
                          AND card_id = $2 
                          AND recycled_at IS NULL
                          AND merge_level = $3
                          AND locked_perk = $4
                        ORDER BY acquired_at
                        LIMIT 2
                        """,
                        user_id, card_id, target_merge_level, target_locked_perk
                    )
                else:
                    # Level 0 cards (no locked perk yet)
                    instances = await conn.fetch(
                        """
                        SELECT instance_id, merge_level, locked_perk
                        FROM user_cards
                        WHERE user_id = $1 
                          AND card_id = $2 
                          AND recycled_at IS NULL
                          AND merge_level = $3
                          AND locked_perk IS NULL
                        ORDER BY acquired_at
                        LIMIT 2
                        """,
                        user_id, card_id, target_merge_level
                    )
            else:
                # Find the most common merge level+perk combination with 2+ cards
                instances = await conn.fetch(
                    """
                    WITH perk_counts AS (
                        SELECT merge_level, locked_perk, COUNT(*) as count
                        FROM user_cards
                        WHERE user_id = $1 AND card_id = $2 AND recycled_at IS NULL
                        GROUP BY merge_level, locked_perk
                        HAVING COUNT(*) >= 2
                        ORDER BY merge_level DESC, count DESC
                        LIMIT 1
                    )
                    SELECT uc.instance_id, uc.merge_level, uc.locked_perk
                    FROM user_cards uc
                    JOIN perk_counts pc ON uc.merge_level = pc.merge_level 
                        AND (uc.locked_perk = pc.locked_perk OR (uc.locked_perk IS NULL AND pc.locked_perk IS NULL))
                    WHERE uc.user_id = $1 AND uc.card_id = $2 AND uc.recycled_at IS NULL
                    ORDER BY uc.acquired_at
                    LIMIT 2
                    """,
                    user_id, card_id
                )
            
            # Filter out PvP-locked instances
            pvp_locked = getattr(self.bot, 'pvp_locked_cards', set())
            instances = [i for i in instances if str(i['instance_id']) not in pvp_locked]

            if len(instances) < 2:
                # Build helpful error message
                if target_merge_level is not None and target_locked_perk:
                    error_msg = (
                        f"❌ You need at least **2** copies of **{card_name_display}** at merge level **{target_merge_level}** "
                        f"with **{target_locked_perk}** perk locked!\n"
                        f"Use `/mycards` to check your collection."
                    )
                else:
                    error_msg = (
                        f"❌ You need at least **2** copies of **{card_name_display}** at the same merge level "
                        f"(and same locked perk if level 1+) to merge!\n"
                        f"Use `/mycards` to check your collection."
                    )
                await ctx.send(error_msg)
                return
            
            # Get the two instances to merge
            instance_uuid_1 = str(instances[0]['instance_id'])
            instance_uuid_2 = str(instances[1]['instance_id'])
            current_level = instances[0]['merge_level']
            next_level = current_level + 1
            
            # Check if already at max level
            if current_level >= max_merge_level:
                await ctx.send(
                    f"❌ **{card_name_display}** is already at max merge level ({max_merge_level})!"
                )
                return
            
            # Validate that both cards have the same merge level and locked perk (should be guaranteed by query)
            if instances[0]['merge_level'] != instances[1]['merge_level']:
                await ctx.send(
                    f"❌ Cannot merge cards at different merge levels!\n"
                    f"Both cards must be at the same level."
                )
                return
            
            # CRITICAL: For level 1+, ensure locked perks match
            if current_level > 0:
                perk1 = instances[0]['locked_perk']
                perk2 = instances[1]['locked_perk']
                if perk1 != perk2:
                    await ctx.send(
                        f"❌ Cannot merge cards with different locked perks!\n"
                        f"Card 1 has **{perk1}** locked, Card 2 has **{perk2}** locked.\n"
                        f"You can only merge cards that share the same perk progression path."
                    )
                    return
            
            # Calculate merge cost
            merge_cost = calculate_merge_cost(rarity, current_level)
            
            # Check user's deck-specific balance
            state = await get_player_deck_state(conn, user_id, deck_id)
            current_credits = state['credits']
            
            if current_credits < merge_cost:
                await ctx.send(
                    f"❌ Insufficient credits!\n"
                    f"Merge cost: **{merge_cost:,}** credits\n"
                    f"Your balance: **{current_credits:,}** credits\n"
                    f"Need: **{merge_cost - current_credits:,}** more credits"
                )
                return
            
            # For first merge (level 0 -> 1), require perk selection
            if current_level == 0:
                # Get available perks for this deck
                available_perks = await get_merge_perks_for_deck(conn, deck_id)
                
                if not available_perks:
                    await ctx.send(
                        f"❌ No merge perks configured for this deck!\n"
                        f"Contact a server admin to configure merge perks."
                    )
                    return
                
                if not perk:
                    # Show available perks
                    perk_list = "\n".join([f"• **{p['perk_name']}** (Base boost: +{p['base_boost']})" for p in available_perks])
                    await ctx.send(
                        f"🎯 **First Merge - Perk Selection Required**\n\n"
                        f"Choose a perk to lock for all future merges of **{card_name_display}**:\n\n"
                        f"{perk_list}\n\n"
                        f"Usage: `/merge {card_name_display} <perk_name>`"
                    )
                    return
                
                # Validate perk exists
                selected_perk = None
                for p in available_perks:
                    if p['perk_name'].lower() == perk.lower():
                        selected_perk = p
                        break
                
                if not selected_perk:
                    perk_names = ", ".join([f"**{p['perk_name']}**" for p in available_perks])
                    await ctx.send(
                        f"❌ Invalid perk name!\n"
                        f"Available perks: {perk_names}"
                    )
                    return
                
                locked_perk = selected_perk['perk_name']
                # Convert Decimal to float for calculations
                base_boost = float(selected_perk['base_boost'])
                diminishing_factor = float(selected_perk['diminishing_factor'])
            else:
                # Use locked perk from first card
                locked_perk = instances[0]['locked_perk']
                
                if not locked_perk:
                    await ctx.send(
                        f"❌ Card has no locked perk! This should not happen.\n"
                        f"Please contact a server admin."
                    )
                    return
                
                # Get perk configuration
                perk_config = await conn.fetchrow(
                    """SELECT base_boost, diminishing_factor
                       FROM deck_merge_perks
                       WHERE deck_id = $1 AND perk_name = $2""",
                    deck_id, locked_perk
                )
                
                if not perk_config:
                    await ctx.send(
                        f"❌ Perk configuration not found for **{locked_perk}**!\n"
                        f"Contact a server admin."
                    )
                    return
                
                # Convert Decimal to float for calculations
                base_boost = float(perk_config['base_boost'])
                diminishing_factor = float(perk_config['diminishing_factor'])
            
            # Calculate perk boost for new level
            perk_boost = calculate_perk_boost(base_boost, next_level, diminishing_factor)
            cumulative_boost = calculate_cumulative_perk_boost(base_boost, next_level, diminishing_factor)
            
            # Execute merge in transaction
            async with conn.transaction():
                # Deduct credits from deck-specific balance
                await update_player_credits(conn, user_id, deck_id, -merge_cost)
                
                # Keep the first card, update it
                await conn.execute(
                    """UPDATE user_cards
                       SET merge_level = $1, locked_perk = $2
                       WHERE instance_id = $3""",
                    next_level, locked_perk, uuid.UUID(instance_uuid_1)
                )
                
                # Recycle the second card (soft delete)
                await conn.execute(
                    """UPDATE user_cards
                       SET recycled_at = NOW()
                       WHERE instance_id = $1""",
                    uuid.UUID(instance_uuid_2)
                )
                
                # Record perk application
                await conn.execute(
                    """INSERT INTO card_perks (instance_id, level_applied, characteristic_name, perk_value)
                       VALUES ($1, $2, $3, $4)""",
                    uuid.UUID(instance_uuid_1), next_level, locked_perk, perk_boost
                )
                
                # Apply boost to the actual field value
                # Find the template field that matches the locked perk name
                template_field = await conn.fetchrow(
                    """SELECT ct.template_id, ct.field_name, ct.field_type, ctf.field_value
                       FROM card_templates ct
                       JOIN card_template_fields ctf ON ct.template_id = ctf.template_id
                       WHERE ctf.card_id = $1 
                       AND LOWER(ct.field_name) = LOWER($2)
                       AND ct.field_type = 'number'
                       LIMIT 1""",
                    card_id, locked_perk
                )
                
                if template_field:
                    base_value = float(template_field['field_value'])
                    # Apply percentage boost: new_value = base_value * (1 + boost_pct/100)
                    boosted_value = base_value * (1 + cumulative_boost / 100)
                    
                    # Store or update the override
                    from datetime import datetime, timezone
                    metadata_json = json.dumps({
                        'cumulative_boost_pct': cumulative_boost,
                        'merge_level': next_level,
                        'calculation_timestamp': datetime.now(timezone.utc).isoformat()
                    })
                    
                    await conn.execute(
                        """INSERT INTO user_card_field_overrides 
                           (instance_id, template_id, base_value, effective_numeric_value, 
                            overridden_value, metadata, updated_at)
                           VALUES ($1, $2, $3, $4, $5, $6, NOW())
                           ON CONFLICT (instance_id, template_id) 
                           DO UPDATE SET 
                               base_value = EXCLUDED.base_value,
                               effective_numeric_value = EXCLUDED.effective_numeric_value,
                               overridden_value = EXCLUDED.overridden_value,
                               metadata = EXCLUDED.metadata,
                               updated_at = NOW()""",
                        uuid.UUID(instance_uuid_1), 
                        template_field['template_id'],
                        str(base_value),
                        boosted_value,
                        str(round(boosted_value, 2)),
                        metadata_json
                    )
                else:
                    # Warning: perk name doesn't match any numeric template field
                    import logging
                    logging.warning(
                        f"Merge perk '{locked_perk}' for card_id {card_id} doesn't match any numeric template field. "
                        f"Perk will be tracked but won't affect card values."
                    )
            
            # Create success embed
            embed = discord.Embed(
                title="✨ Merge Successful!",
                description=f"**{card_name_display}** {format_merge_level_display(current_level)} → {format_merge_level_display(next_level)}",
                color=discord.Color.green()
            )
            
            embed.add_field(
                name="New Merge Level",
                value=f"Level **{next_level}**",
                inline=True
            )
            
            embed.add_field(
                name="Credits Spent",
                value=f"{merge_cost:,}",
                inline=True
            )
            
            embed.add_field(
                name="Locked Perk",
                value=f"**{locked_perk}**",
                inline=True
            )
            
            embed.add_field(
                name="Perk Boost (This Level)",
                value=f"+{perk_boost}%",
                inline=True
            )
            
            embed.add_field(
                name="Total Cumulative Boost",
                value=f"+{cumulative_boost}%",
                inline=True
            )
            
            # Calculate next merge cost
            if next_level < max_merge_level:
                next_merge_cost = calculate_merge_cost(rarity, next_level)
                embed.add_field(
                    name="Next Merge Cost",
                    value=f"{next_merge_cost:,} credits",
                    inline=True
                )
            else:
                embed.add_field(
                    name="Status",
                    value="🌟 MAX LEVEL REACHED!",
                    inline=False
                )
            
            embed.set_footer(text=f"Merged instances: {instance_uuid_1} + {instance_uuid_2}")
            
            await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(MergeCommands(bot))
