"""
DeckForge Slash Commands
Slash command implementations for Discord
"""
import discord
from discord import app_commands
from discord.ext import commands
import asyncpg
import uuid
from typing import Optional, List
from datetime import datetime, timezone

from utils.card_helpers import (
    validate_rarity,
    sort_cards_by_rarity,
    create_card_embed,
    RARITY_HIERARCHY,
    get_player_deck_state
)
from utils.drop_helpers import get_default_drop_rates
from utils.pack_logic import validate_pack_type, format_pack_type


class SlashCommands(commands.Cog):
    """Cog for slash command implementations"""
    
    def __init__(self, bot):
        self.bot = bot
        self.db_pool: asyncpg.Pool = bot.db_pool
        self.admin_ids = bot.admin_ids
    
    async def card_name_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> List[app_commands.Choice[str]]:
        """Autocomplete for card names"""
        guild_id = interaction.guild_id
        if not guild_id:
            return []
        
        # Get server's assigned deck
        deck = await self.bot.get_server_deck(guild_id)
        if not deck:
            return []
        
        deck_id = deck['deck_id']
        
        # Search for cards matching the current input
        async with self.db_pool.acquire() as conn:
            cards = await conn.fetch(
                """SELECT card_id, name, rarity 
                   FROM cards 
                   WHERE deck_id = $1 AND LOWER(name) LIKE LOWER($2)
                   ORDER BY name
                   LIMIT 25""",
                deck_id,
                f"%{current}%"
            )
        
        return [
            app_commands.Choice(
                name=f"{card['name']} ({card['rarity']})",
                value=str(card['card_id'])
            )
            for card in cards
        ]
    
    @app_commands.command(name="cardinfo", description="View detailed information about a specific card")
    @app_commands.describe(
        card_name="The name of the card to look up",
        card_id="Or the card ID number to look up",
        merge_level="Optional: Show stats for a specific merge level"
    )
    @app_commands.autocomplete(card_name=card_name_autocomplete)
    async def cardinfo(
        self,
        interaction: discord.Interaction,
        card_name: Optional[str] = None,
        card_id: Optional[int] = None,
        merge_level: Optional[int] = None
    ):
        """View detailed information about a specific card by name or ID"""
        # Defer response to prevent timeout
        await interaction.response.defer()
        
        guild_id = interaction.guild_id
        
        if not guild_id:
            await interaction.followup.send("❌ This command can only be used in a server!", ephemeral=True)
            return
        
        # Check if server has an assigned deck
        deck = await self.bot.get_server_deck(guild_id)
        if not deck:
            await interaction.followup.send(
                "❌ No deck assigned to this server!\n"
                "Ask a server manager to assign a deck via the web admin portal.",
                ephemeral=True
            )
            return
        
        deck_id = deck['deck_id']
        
        # Must provide either card_name or card_id
        if not card_name and not card_id:
            await interaction.followup.send(
                "❌ Please provide either a card name or card ID!",
                ephemeral=True
            )
            return
        
        async with self.db_pool.acquire() as conn:
            if card_name:
                # If card_name is actually a card_id from autocomplete, try parsing it
                try:
                    parsed_id = int(card_name)
                    card = await conn.fetchrow(
                        """SELECT c.*, 
                           (SELECT COUNT(*) FROM user_cards uc 
                            WHERE uc.card_id = c.card_id AND uc.user_id = $2 AND uc.recycled_at IS NULL) as owned_count
                           FROM cards c
                           WHERE c.card_id = $1 AND c.deck_id = $3""",
                        parsed_id, interaction.user.id, deck_id
                    )
                except (ValueError, TypeError):
                    # Search by name
                    card = await conn.fetchrow(
                        """SELECT c.*, 
                           (SELECT COUNT(*) FROM user_cards uc 
                            WHERE uc.card_id = c.card_id AND uc.user_id = $2 AND uc.recycled_at IS NULL) as owned_count
                           FROM cards c
                           WHERE LOWER(c.name) = LOWER($1) AND c.deck_id = $3
                           LIMIT 1""",
                        card_name, interaction.user.id, deck_id
                    )
            else:
                # Search by card_id
                card = await conn.fetchrow(
                    """SELECT c.*, 
                       (SELECT COUNT(*) FROM user_cards uc 
                        WHERE uc.card_id = c.card_id AND uc.user_id = $2 AND uc.recycled_at IS NULL) as owned_count
                       FROM cards c
                       WHERE c.card_id = $1 AND c.deck_id = $3""",
                    card_id, interaction.user.id, deck_id
                )
            
            if not card:
                await interaction.followup.send(
                    "❌ Card not found in this deck!",
                    ephemeral=True
                )
                return
            
            # Get custom template fields for this card
            template_fields = await conn.fetch(
                """SELECT ctf.field_value, ct.field_name, ct.field_type, ct.template_id
                   FROM card_template_fields ctf
                   JOIN card_templates ct ON ctf.template_id = ct.template_id
                   WHERE ctf.card_id = $1
                   ORDER BY ct.field_order""",
                card['card_id']
            )
            
            # If merge_level specified, get a sample instance at that level to show boosted values
            sample_instance = None
            if merge_level is not None and merge_level > 0:
                sample_instance = await conn.fetchrow(
                    """SELECT instance_id 
                       FROM user_cards 
                       WHERE user_id = $1 AND card_id = $2 AND merge_level = $3 AND recycled_at IS NULL
                       LIMIT 1""",
                    interaction.user.id, card['card_id'], merge_level
                )
        
        # Create embed
        embed = create_card_embed(card)
        
        # Add custom template fields (with overrides if viewing specific merge level)
        if template_fields:
            for field in template_fields:
                field_value = field['field_value'] or 'N/A'
                field_name = field['field_name']
                
                # Check if we should show boosted value for this field
                if sample_instance:
                    async with self.db_pool.acquire() as conn:
                        override = await conn.fetchrow(
                            """SELECT overridden_value, metadata
                               FROM user_card_field_overrides
                               WHERE instance_id = $1 AND template_id = $2""",
                            sample_instance['instance_id'], field['template_id']
                        )
                        
                        if override:
                            boost_pct = override['metadata'].get('cumulative_boost_pct', 0)
                            field_value = f"{override['overridden_value']} ✨ (+{boost_pct}%)"
                
                embed.add_field(
                    name=field_name,
                    value=field_value,
                    inline=True
                )
        
        # Add merge information if card is mergeable
        async with self.db_pool.acquire() as conn:
            if card.get('mergeable'):
                # Get merge level breakdown
                merge_counts = await conn.fetch(
                    """SELECT merge_level, COUNT(*) as count
                       FROM user_cards
                       WHERE user_id = $1 AND card_id = $2 AND recycled_at IS NULL
                       GROUP BY merge_level
                       ORDER BY merge_level""",
                    interaction.user.id, card['card_id']
                )
                
                from utils.merge_helpers import format_merge_level_display, calculate_cumulative_perk_boost
                
                if merge_counts:
                    merge_text = "\n".join([
                        f"Level {mc['merge_level']} {format_merge_level_display(mc['merge_level'])}: {mc['count']}x"
                        for mc in merge_counts
                    ])
                    embed.add_field(
                        name=f"You Own ({card['owned_count']} total)",
                        value=merge_text,
                        inline=False
                    )
                    
                    # If merge_level specified, show perk boost information
                    if merge_level is not None:
                        # Get perk boost for this merge level
                        from utils.merge_helpers import get_merge_perks_for_deck
                        
                        merge_perks = await get_merge_perks_for_deck(conn, deck_id)
                        
                        if merge_perks and merge_level > 0:
                            embed.add_field(
                                name=f"🌟 Merge Level {merge_level} Boosts",
                                value="Shows potential boosts if merged to this level",
                                inline=False
                            )
                            
                            for perk in merge_perks:
                                perk_name = perk['perk_name']
                                # Convert Decimal to float for calculations
                                base_boost = float(perk['base_boost'])
                                diminishing_factor = float(perk['diminishing_factor'])
                                
                                cumulative_boost = calculate_cumulative_perk_boost(
                                    base_boost, merge_level, diminishing_factor
                                )
                                
                                embed.add_field(
                                    name=f"• {perk_name}",
                                    value=f"+{cumulative_boost}%",
                                    inline=True
                                )
                        elif merge_level == 0:
                            embed.add_field(
                                name="📝 Note",
                                value="This is the base card (no merge boosts)",
                                inline=False
                            )
                else:
                    embed.add_field(
                        name="You Own",
                        value=f"{card['owned_count']} copies",
                        inline=False
                    )
            else:
                embed.add_field(
                    name="You Own",
                    value=f"{card['owned_count']} copies",
                    inline=False
                )
        
        await interaction.followup.send(embed=embed)
    
    @app_commands.command(name="balance", description="Check your credit balance for this server's deck")
    async def balance(self, interaction: discord.Interaction):
        """Check your credit balance for this server's deck"""
        if not interaction.guild:
            await interaction.response.send_message("❌ This command must be used in a server!", ephemeral=True)
            return
        
        user_id = interaction.user.id
        guild_id = interaction.guild.id
        
        deck = await self.bot.get_server_deck(guild_id)
        if not deck:
            await interaction.response.send_message("❌ No deck assigned to this server!", ephemeral=True)
            return
        
        async with self.db_pool.acquire() as conn:
            state = await get_player_deck_state(conn, user_id, deck['deck_id'])
        
        credits = state['credits']
        
        embed = discord.Embed(
            title="💰 Credit Balance",
            description=f"You have **{credits:,}** credits for **{deck['name']}**",
            color=discord.Color.gold()
        )
        embed.add_field(
            name="💡 How to Earn Credits",
            value="• Recycle duplicate cards with `/recycle`\n• Complete missions successfully\n• Microtransactions coming soon!",
            inline=False
        )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @app_commands.command(name="help", description="Get help with DeckForge commands")
    async def help_command(self, interaction: discord.Interaction):
        """Display help information about available commands"""
        embed = discord.Embed(
            title="🚀 DeckForge Help",
            description="Collect rocket-themed trading cards and build your collection!",
            color=discord.Color.blue()
        )
        
        embed.add_field(
            name="📦 Pack Commands",
            value=(
                "`/drop [amount] [pack_type]` - Open packs to get cards\n"
                "`/claimfreepack` - Claim a free Normal Pack (cooldown based on deck)\n"
                "`/buypack [pack_type] [amount]` - Purchase packs with credits"
            ),
            inline=False
        )
        
        embed.add_field(
            name="🎴 Collection Commands",
            value=(
                "`/mycards [page]` - View your card collection\n"
                "`/cardinfo` - View detailed info about a card (with autocomplete)\n"
                "`/recycle` - Convert duplicate cards into credits"
            ),
            inline=False
        )
        
        embed.add_field(
            name="💰 Economy Commands",
            value=(
                "`/balance` - Check your credit balance\n"
                "`/buycredits` - Info about purchasing credits"
            ),
            inline=False
        )
        
        embed.add_field(
            name="🔄 Trading Commands",
            value=(
                "`/requesttrade @user` - Start a trade with another player\n"
                "`/tradeadd [instance_id]` - Add a card to active trade\n"
                "`/traderemove [instance_id]` - Remove a card from trade\n"
                "`/accepttrade` - Accept the current trade offer\n"
                "`/finalize` - Complete and finalize the trade"
            ),
            inline=False
        )
        
        embed.set_footer(text="Use autocomplete to easily find cards by name!")
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @app_commands.command(name="buycredits", description="Information about purchasing credits")
    async def buycredits(self, interaction: discord.Interaction):
        """Get information about buying credits"""
        embed = discord.Embed(
            title="💳 Purchase Credits",
            description="Credit purchases are not yet available!\n\n"
                       "**How to earn credits:**\n"
                       "• Recycle duplicate cards using `/recycle`\n"
                       "• Microtransactions coming soon via Stripe integration",
            color=discord.Color.gold()
        )
        embed.set_footer(text="Credits can only be earned by recycling cards for now")
        
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(SlashCommands(bot))
