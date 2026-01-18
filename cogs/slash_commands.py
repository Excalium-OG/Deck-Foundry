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
    RARITY_COLORS,
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
        """Autocomplete for card names from player's inventory only"""
        guild_id = interaction.guild_id
        user_id = interaction.user.id
        if not guild_id:
            return []
        
        deck = await self.bot.get_server_deck(guild_id)
        if not deck:
            return []
        
        deck_id = deck['deck_id']
        
        async with self.db_pool.acquire() as conn:
            owned_cards = await conn.fetch(
                """SELECT DISTINCT ON (c.card_id, uc.merge_level) 
                          uc.instance_id, c.card_id, c.name, c.rarity, uc.merge_level, uc.locked_perk
                   FROM user_cards uc
                   JOIN cards c ON uc.card_id = c.card_id
                   WHERE uc.user_id = $1 AND c.deck_id = $2 
                         AND uc.recycled_at IS NULL
                         AND LOWER(c.name) LIKE LOWER($3)
                   ORDER BY c.card_id, uc.merge_level, uc.instance_id
                   LIMIT 25""",
                user_id,
                deck_id,
                f"%{current}%"
            )
        
        from utils.merge_helpers import format_merge_level_display
        
        choices = []
        for card in owned_cards:
            merge_display = format_merge_level_display(card['merge_level']) if card['merge_level'] > 0 else ""
            perk_display = f" [{card['locked_perk']}]" if card['locked_perk'] else ""
            display_name = f"{card['name']} ({card['rarity']}){merge_display}{perk_display}"
            if len(display_name) > 100:
                display_name = display_name[:97] + "..."
            choices.append(
                app_commands.Choice(
                    name=display_name,
                    value=str(card['instance_id'])
                )
            )
        
        return choices
    
    @app_commands.command(name="cardinfo", description="View detailed information about a card from your inventory")
    @app_commands.describe(
        card_name="Select a card from your inventory"
    )
    @app_commands.autocomplete(card_name=card_name_autocomplete)
    async def cardinfo(
        self,
        interaction: discord.Interaction,
        card_name: str
    ):
        """View detailed information about a card from your inventory"""
        print(f"[CARDINFO] Starting for user {interaction.user.id}, card_name={card_name}")
        await interaction.response.defer()
        print("[CARDINFO] Deferred response")
        
        guild_id = interaction.guild_id
        user_id = interaction.user.id
        
        if not guild_id:
            await interaction.followup.send("❌ This command can only be used in a server!", ephemeral=True)
            return
        
        print("[CARDINFO] Getting deck...")
        deck = await self.bot.get_server_deck(guild_id)
        if not deck:
            await interaction.followup.send(
                "❌ No deck assigned to this server!\n"
                "Ask a server manager to assign a deck via the web admin portal.",
                ephemeral=True
            )
            return
        
        deck_id = deck['deck_id']
        print(f"[CARDINFO] Got deck_id={deck_id}")
        
        async with self.db_pool.acquire() as conn:
            instance = None
            
            try:
                print(f"[CARDINFO] Parsing UUID from: {card_name}")
                instance_id = uuid.UUID(card_name)
                print(f"[CARDINFO] Parsed UUID: {instance_id}, querying...")
                instance = await conn.fetchrow(
                    """SELECT uc.*, c.name, c.rarity, c.image_url, c.mergeable, c.max_merge_level
                       FROM user_cards uc
                       JOIN cards c ON uc.card_id = c.card_id
                       WHERE uc.instance_id = $1 AND uc.user_id = $2 AND uc.recycled_at IS NULL""",
                    instance_id, user_id
                )
                print(f"[CARDINFO] Query done, instance={instance is not None}")
            except (ValueError, TypeError) as e:
                print(f"[CARDINFO] UUID parse failed: {e}, trying name search")
                instance = await conn.fetchrow(
                    """SELECT uc.*, c.name, c.rarity, c.image_url, c.mergeable, c.max_merge_level
                       FROM user_cards uc
                       JOIN cards c ON uc.card_id = c.card_id
                       WHERE LOWER(c.name) = LOWER($1) AND uc.user_id = $2 
                             AND c.deck_id = $3 AND uc.recycled_at IS NULL
                       LIMIT 1""",
                    card_name, user_id, deck_id
                )
            
            if not instance:
                await interaction.followup.send(
                    "❌ You don't own that card! Use `/mycards` to see your collection.",
                    ephemeral=True
                )
                return
            
            print("[CARDINFO] Extracting instance data...")
            card_id = instance['card_id']
            merge_level = instance['merge_level']
            locked_perk = instance['locked_perk']
            instance_id = instance['instance_id']
            print(f"[CARDINFO] card_id={card_id}, merge_level={merge_level}, locked_perk={locked_perk}")
            
            print("[CARDINFO] Fetching owned_count...")
            owned_count = await conn.fetchval(
                """SELECT COUNT(*) FROM user_cards 
                   WHERE user_id = $1 AND card_id = $2 AND recycled_at IS NULL""",
                user_id, card_id
            )
            print(f"[CARDINFO] owned_count={owned_count}")
            
            print("[CARDINFO] Fetching template_fields...")
            template_fields = await conn.fetch(
                """SELECT ctf.field_value, ct.field_name, ct.field_type, ct.template_id
                   FROM card_template_fields ctf
                   JOIN card_templates ct ON ctf.template_id = ct.template_id
                   WHERE ctf.card_id = $1
                   ORDER BY ct.field_order""",
                card_id
            )
            print(f"[CARDINFO] template_fields count={len(template_fields)}")
            
            overrides = {}
            if merge_level > 0:
                print("[CARDINFO] Fetching overrides...")
                override_rows = await conn.fetch(
                    """SELECT template_id, overridden_value, metadata
                       FROM user_card_field_overrides
                       WHERE instance_id = $1""",
                    instance_id
                )
                overrides = {row['template_id']: row for row in override_rows}
                print(f"[CARDINFO] overrides count={len(overrides)}")
        
        print("[CARDINFO] Building embed...")
        from utils.merge_helpers import format_merge_level_display, calculate_cumulative_perk_boost
        
        merge_display = format_merge_level_display(merge_level) if merge_level > 0 else ""
        title = f"{instance['name']} {merge_display}".strip()
        print(f"[CARDINFO] title={title}")
        
        print("[CARDINFO] Creating embed object...")
        color = RARITY_COLORS.get(instance['rarity'], discord.Color.default())
        embed = discord.Embed(title=title, color=color)
        embed.add_field(name="Rarity", value=instance['rarity'], inline=True)
        print("[CARDINFO] Added rarity field")
        
        if merge_level > 0:
            embed.add_field(name="Merge Level", value=str(merge_level), inline=True)
            if locked_perk:
                embed.add_field(name="Locked Perk", value=f"🔒 {locked_perk}", inline=True)
            print("[CARDINFO] Added merge fields")
        
        if instance['image_url']:
            embed.set_thumbnail(url=instance['image_url'])
            print("[CARDINFO] Set thumbnail")
        
        print(f"[CARDINFO] Processing {len(template_fields)} template fields...")
        if template_fields:
            for i, field in enumerate(template_fields):
                print(f"[CARDINFO] Field {i}: {field['field_name']}")
                field_name = field['field_name']
                base_value = field['field_value'] or 'N/A'
                
                if field['template_id'] in overrides:
                    override = overrides[field['template_id']]
                    boosted_value = override['overridden_value']
                    metadata = override['metadata']
                    # Parse JSON string if needed
                    if isinstance(metadata, str):
                        import json
                        try:
                            metadata = json.loads(metadata)
                        except:
                            metadata = {}
                    boost_pct = metadata.get('cumulative_boost_pct', 0) if metadata else 0
                    display_value = f"**{boosted_value}** ✨ ({base_value} + {boost_pct}%)"
                else:
                    display_value = base_value
                
                embed.add_field(name=field_name, value=display_value, inline=True)
                print(f"[CARDINFO] Added field {field_name}")
        
        is_mergeable = instance['mergeable'] if 'mergeable' in instance.keys() else False
        max_merge = instance['max_merge_level'] if 'max_merge_level' in instance.keys() else 10
        
        if is_mergeable:
            async with self.db_pool.acquire() as conn:
                merge_counts = await conn.fetch(
                    """SELECT merge_level, COUNT(*) as count
                       FROM user_cards
                       WHERE user_id = $1 AND card_id = $2 AND recycled_at IS NULL
                       GROUP BY merge_level
                       ORDER BY merge_level""",
                    user_id, card_id
                )
            
            if merge_counts:
                merge_text = "\n".join([
                    f"Level {mc['merge_level']} {format_merge_level_display(mc['merge_level'])}: {mc['count']}x"
                    for mc in merge_counts
                ])
                embed.add_field(
                    name=f"Your Collection ({owned_count} total)",
                    value=merge_text,
                    inline=False
                )
                
                if merge_level < max_merge:
                    embed.set_footer(text=f"Merge two Level {merge_level} cards to create Level {merge_level + 1}")
        else:
            embed.add_field(
                name="Your Collection",
                value=f"{owned_count} copies",
                inline=False
            )
        
        print("[CARDINFO] Sending embed...")
        await interaction.followup.send(embed=embed)
        print("[CARDINFO] Done!")
    
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
