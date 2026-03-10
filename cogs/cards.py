"""
Deck Foundry Card Commands Cog
Handles all card-related commands for the trading card bot
"""
import discord
from discord.ext import commands
import asyncio
import asyncpg
import uuid
import random
from datetime import datetime, timezone
from typing import Optional

from utils.card_helpers import (
    validate_rarity,
    sort_cards_by_rarity,
    check_drop_cooldown,
    format_cooldown_time,
    validate_image_attachment,
    create_card_embed,
    RARITY_HIERARCHY,
    update_player_credits,
    get_inventory_item,
    add_inventory_item,
    remove_inventory_item
)
from utils.merge_helpers import format_merge_level_display

# Recycle credit values by rarity
RECYCLE_VALUES = {
    'Common': 10,
    'Uncommon': 25,
    'Exceptional': 50,
    'Rare': 100,
    'Epic': 250,
    'Legendary': 500,
    'Mythic': 1000
}
from utils.drop_helpers import (
    get_default_drop_rates,
    select_rarity_by_weight,
    format_drop_rates_table
)
from utils.pack_logic import (
    validate_pack_type,
    format_pack_type,
    apply_pack_modifier,
    get_pack_card_count
)


class CardCommands(commands.Cog):
    """Cog for card collection and management commands"""
    
    def __init__(self, bot):
        self.bot = bot
        self.db_pool: asyncpg.Pool = bot.db_pool
        self.admin_ids = bot.admin_ids
    
    def is_admin(self, user_id: int) -> bool:
        """Check if user is an admin"""
        return user_id in self.admin_ids or user_id == self.bot.owner_id
    
    
    async def get_deck_drop_rates(self, conn, deck_id: int) -> dict:
        """Get drop rates for a deck from rarity_ranges table"""
        # Fetch deck-specific rates from rarity_ranges
        rates_rows = await conn.fetch(
            "SELECT rarity, drop_rate FROM rarity_ranges WHERE deck_id = $1",
            deck_id
        )
        
        if not rates_rows:
            return get_default_drop_rates()
        
        rates = {row['rarity']: row['drop_rate'] for row in rates_rows}
        
        # Ensure all rarities are present
        for rarity in RARITY_HIERARCHY:
            if rarity not in rates:
                return get_default_drop_rates()
        
        return rates
    
    @commands.hybrid_command(name='drop', description="Open packs to get cards")
    async def drop_cards(self, ctx, amount: int = 1, pack_type: str = "Normal Pack"):
        """
        Open packs to get cards. Each pack gives 2 cards.
        Usage: /drop [amount] [pack_type]
        Examples: /drop, /drop 2, /drop 1 "Booster Pack"
        """
        # Defer if invoked as slash command to avoid timeout
        if ctx.interaction:
            await ctx.defer()
        
        user_id = ctx.author.id
        guild_id = ctx.guild.id if ctx.guild else None
        
        # Check if server has an assigned deck
        if not guild_id:
            await ctx.send("❌ This command can only be used in a server!")
            return
        
        deck = await self.bot.get_server_deck(guild_id)
        if not deck:
            await ctx.send(
                "❌ No deck assigned to this server!\n"
                "Ask a server manager to assign a deck via the web admin portal."
            )
            return
        
        deck_id = deck['deck_id']
        
        # Validate amount
        if amount < 1 or amount > 10:
            await ctx.send("❌ You can open 1-10 packs at a time!")
            return
        
        # Format and validate pack type
        pack_type = format_pack_type(pack_type)
        if not validate_pack_type(pack_type):
            await ctx.send(
                f"❌ Invalid pack type! Must be one of: Normal Pack, Booster Pack, Booster Pack+"
            )
            return
        
        async with self.db_pool.acquire() as conn:
            # Check if user has enough packs (deck-specific)
            current_qty = await get_inventory_item(conn, user_id, deck_id, 'pack', pack_type)
            
            if current_qty < amount:
                await ctx.send(
                    f"❌ You don't have enough **{pack_type}**s!\n"
                    f"You have: **{current_qty}**, need: **{amount}**\n"
                    f"Use `/mypacks` to see your inventory or `/claimfreepack` for a free pack."
                )
                return
            
            # Remove packs from deck-specific inventory
            success, new_qty = await remove_inventory_item(conn, user_id, deck_id, 'pack', pack_type, amount)
            if not success:
                await ctx.send(f"❌ Failed to open packs - insufficient quantity!")
                return
            
            # Get all available cards from the assigned deck
            all_cards = await conn.fetch(
                "SELECT card_id, name, rarity FROM cards WHERE deck_id = $1",
                deck_id
            )
            
            if len(all_cards) == 0:
                # Refund packs to deck-specific inventory
                await add_inventory_item(conn, user_id, deck_id, 'pack', pack_type, amount)
                await ctx.send(f"❌ No cards in the **{deck['name']}** deck! Contact the deck creator to add cards via the web portal.")
                return
            
            # Group cards by rarity
            cards_by_rarity = {}
            for card in all_cards:
                rarity = card['rarity']
                if rarity not in cards_by_rarity:
                    cards_by_rarity[rarity] = []
                cards_by_rarity[rarity].append(card)
            
            # Get base drop rates for this deck
            base_rates = await self.get_deck_drop_rates(conn, deck_id)
            
            # Apply pack modifier to get modified drop rates
            drop_rates = apply_pack_modifier(base_rates, pack_type)
            
            cards_per_pack = get_pack_card_count(pack_type)
            
            dropped_cards = []
            for _ in range(amount * cards_per_pack):
                # Select rarity based on modified weights
                selected_rarity = select_rarity_by_weight(drop_rates)
                
                # Get cards of this rarity
                available_cards = cards_by_rarity.get(selected_rarity, [])
                
                # If no cards of this rarity exist, try again with any available rarity
                if not available_cards:
                    available_rarities = list(cards_by_rarity.keys())
                    if not available_rarities:
                        continue
                    selected_rarity = random.choice(available_rarities)
                    available_cards = cards_by_rarity[selected_rarity]
                
                # Select random card from this rarity
                selected_card = random.choice(available_cards)
                dropped_cards.append(selected_card)
            
            if not dropped_cards:
                await ctx.send("❌ Unable to drop cards. Please contact an admin.")
                return
            
            # Insert card instances
            instances = []
            for card in dropped_cards:
                instance_id = uuid.uuid4()
                await conn.execute(
                    """INSERT INTO user_cards (instance_id, user_id, card_id, acquired_at, source)
                       VALUES ($1, $2, $3, $4, $5)""",
                    instance_id, user_id, card['card_id'], datetime.now(timezone.utc), 'pack'
                )
                instances.append({
                    'instance_id': str(instance_id),
                    'name': card['name'],
                    'rarity': card['rarity']
                })
        
        # Send response
        pack_emoji = "📦" if pack_type == "Normal Pack" else "🎁"
        embed = discord.Embed(
            title=f"{pack_emoji} Opened {amount} {pack_type}{'s' if amount > 1 else ''}!",
            description=f"{ctx.author.mention} received {len(instances)} cards:",
            color=discord.Color.purple()
        )
        
        # Group cards by rarity for display
        rarity_groups = {}
        for inst in instances:
            rarity = inst['rarity']
            if rarity not in rarity_groups:
                rarity_groups[rarity] = []
            rarity_groups[rarity].append(inst['name'])
        
        # Display by rarity
        for rarity in RARITY_HIERARCHY:
            if rarity in rarity_groups:
                cards = rarity_groups[rarity]
                embed.add_field(
                    name=f"{rarity} ({len(cards)})",
                    value=", ".join(cards),
                    inline=False
                )
        
        await ctx.send(embed=embed)
    
    @commands.hybrid_command(name='mycards', description="View your card collection from this server's deck")
    async def my_cards(self, ctx):
        """
        List all owned cards from this server's deck with pagination. Shows grouped cards by rarity.
        Usage: /mycards
        """
        # Defer if invoked as slash command to avoid timeout
        if ctx.interaction:
            await ctx.defer()
        
        user_id = ctx.author.id
        guild_id = ctx.guild.id if ctx.guild else None
        
        # Check if server has an assigned deck
        if not guild_id:
            await ctx.send("❌ This command can only be used in a server!")
            return
        
        deck = await self.bot.get_server_deck(guild_id)
        if not deck:
            await ctx.send(
                "❌ No deck assigned to this server!\n"
                "Ask a server manager to assign a deck via the web admin portal."
            )
            return
        
        deck_id = deck['deck_id']
        deck_name = deck['name']
        
        async with self.db_pool.acquire() as conn:
            # Get all user cards with card details, filtered by server's deck
            # Group by merge_level to show merged cards separately
            cards = await conn.fetch(
                """SELECT c.card_id, c.name, c.rarity, uc.merge_level, COUNT(*) as quantity
                   FROM user_cards uc
                   JOIN cards c ON uc.card_id = c.card_id
                   WHERE uc.user_id = $1 AND uc.recycled_at IS NULL AND c.deck_id = $2
                   GROUP BY c.card_id, c.name, c.rarity, uc.merge_level
                   ORDER BY c.rarity, c.name, uc.merge_level""",
                user_id, deck_id
            )
        
        if not cards:
            await ctx.send(f"📦 You don't have any cards from **{deck_name}** yet! Use `/claimfreepack` to get a pack, then `/drop` to open it.")
            return
        
        # Convert to list and sort by rarity hierarchy
        cards_list = [dict(card) for card in cards]
        sorted_cards = sort_cards_by_rarity(cards_list)
        
        # Build card lines with quantity and merge level indicator
        from utils.merge_helpers import format_merge_level_display
        
        card_lines = []
        total_count = 0
        for card in sorted_cards:
            qty = card['quantity']
            total_count += qty
            merge_level = card.get('merge_level', 0)
            
            # Format merge level display
            merge_display = format_merge_level_display(merge_level)
            merge_suffix = f" {merge_display}" if merge_display else ""
            
            prefix = f"(x{qty})" if qty > 1 else ""
            card_lines.append(f"{prefix} **{card['name']}**{merge_suffix} (ID: {card['card_id']})".strip())
        
        # Paginate: 8 lines per page
        lines_per_page = 8
        pages = []
        for i in range(0, len(card_lines), lines_per_page):
            page_lines = card_lines[i:i + lines_per_page]
            pages.append(page_lines)
        
        # Create initial embed
        current_page = 0
        embed = discord.Embed(
            title=f"🎴 {ctx.author.name}'s {deck_name} Collection",
            description=f"Total cards: {total_count} | Unique: {len(sorted_cards)}",
            color=discord.Color.green()
        )
        
        # Add page content
        embed.add_field(
            name=f"Page {current_page + 1}/{len(pages)}",
            value="\n".join(pages[current_page]),
            inline=False
        )
        
        message = await ctx.send(embed=embed)
        
        # Only add reactions if there are multiple pages
        if len(pages) > 1:
            await message.add_reaction("⬅️")
            await message.add_reaction("➡️")
            
            def check(reaction, user):
                return (
                    user == ctx.author
                    and str(reaction.emoji) in ["⬅️", "➡️"]
                    and reaction.message.id == message.id
                )
            
            # Handle pagination
            while True:
                try:
                    reaction, user = await self.bot.wait_for("reaction_add", timeout=60.0, check=check)
                    
                    if str(reaction.emoji) == "➡️":
                        current_page = (current_page + 1) % len(pages)
                    elif str(reaction.emoji) == "⬅️":
                        current_page = (current_page - 1) % len(pages)
                    
                    # Update embed
                    embed = discord.Embed(
                        title=f"🎴 {ctx.author.name}'s {deck_name} Collection",
                        description=f"Total cards: {total_count} | Unique: {len(sorted_cards)}",
                        color=discord.Color.green()
                    )
                    embed.add_field(
                        name=f"Page {current_page + 1}/{len(pages)}",
                        value="\n".join(pages[current_page]),
                        inline=False
                    )
                    
                    await message.edit(embed=embed)
                    await message.remove_reaction(reaction, user)
                    
                except asyncio.TimeoutError:
                    await message.clear_reactions()
                    break
    
    async def card_name_autocomplete_for_recycle(
        self,
        interaction: discord.Interaction,
        current: str
    ) -> list[discord.app_commands.Choice[str]]:
        """
        Autocomplete function for recycle command - shows owned cards with merge levels
        """
        user_id = interaction.user.id
        guild_id = interaction.guild_id if interaction.guild else None
        
        if not guild_id:
            return []
        
        # Get server deck
        deck = await self.bot.get_server_deck(guild_id)
        if not deck:
            return []
        
        deck_id = deck['deck_id']
        
        # Get user's cards from this deck, grouped by card_id and merge_level
        locked_list = list(getattr(self.bot, 'pvp_locked_cards', set()))
        async with self.db_pool.acquire() as conn:
            cards = await conn.fetch(
                """SELECT c.name, c.card_id, c.rarity, uc.merge_level, COUNT(*) as count
                   FROM user_cards uc
                   JOIN cards c ON uc.card_id = c.card_id
                   WHERE uc.user_id = $1 
                   AND c.deck_id = $2 
                   AND uc.recycled_at IS NULL
                   AND LOWER(c.name) LIKE LOWER($3)
                   AND NOT (uc.instance_id::text = ANY($4::text[]))
                   GROUP BY c.card_id, c.name, c.rarity, uc.merge_level
                   ORDER BY c.name, uc.merge_level
                   LIMIT 25""",
                user_id, deck_id, f"%{current}%", locked_list
            )
        
        choices = []
        for card in cards:
            merge_display = format_merge_level_display(card['merge_level'])
            count_display = f" (x{card['count']})" if card['count'] > 1 else ""
            
            # Calculate recycle value
            base_value = RECYCLE_VALUES.get(card['rarity'], 10)
            credit_value = int(base_value * (1.25 ** card['merge_level']))
            
            # Format: "Card Name ★ (x3) - 12cr" for level 1+, "Card Name (x3) - 10cr" for level 0
            if merge_display:
                display_name = f"{card['name']} {merge_display}{count_display} - {credit_value}cr"
            else:
                display_name = f"{card['name']}{count_display} - {credit_value}cr"
            
            # Value stores "card_id|merge_level" for lookup
            value = f"{card['card_id']}|{card['merge_level']}"
            choices.append(discord.app_commands.Choice(name=display_name, value=value))
        
        return choices
    
    @commands.hybrid_command(name='recycle', description="Convert duplicate cards into credits based on rarity")
    @discord.app_commands.autocomplete(card_name=card_name_autocomplete_for_recycle)
    async def recycle_cards(self, ctx, card_name: str, amount: int = 1):
        """
        Recycle duplicate cards from this server's deck for credits.
        Usage: /recycle <card_name> [amount]
        Example: /recycle "Rocket ★" 3 (recycles 3 level 1 Rocket cards)
        """
        # Defer if invoked as slash command to avoid timeout
        if ctx.interaction:
            await ctx.defer()
        
        user_id = ctx.author.id
        guild_id = ctx.guild.id if ctx.guild else None
        
        # Check if server has an assigned deck
        if not guild_id:
            await ctx.send("❌ This command can only be used in a server!")
            return
        
        deck = await self.bot.get_server_deck(guild_id)
        if not deck:
            await ctx.send(
                "❌ No deck assigned to this server!\n"
                "Ask a server manager to assign a deck via the web admin portal."
            )
            return
        
        deck_id = deck['deck_id']
        
        # Validate amount
        if amount < 1:
            await ctx.send("❌ Amount must be at least 1!")
            return
        
        if amount > 100:
            await ctx.send("❌ You can only recycle up to 100 cards at once!")
            return
        
        # Parse card_name parameter (format: "card_id|merge_level")
        try:
            card_id_str, merge_level_str = card_name.split('|')
            card_id = int(card_id_str)
            merge_level = int(merge_level_str)
        except (ValueError, AttributeError):
            await ctx.send("❌ Invalid card selection! Please use the autocomplete feature.")
            return
        
        async with self.db_pool.acquire() as conn:
            # Get card info and verify it belongs to this server's deck
            card_info = await conn.fetchrow(
                "SELECT name, rarity, deck_id FROM cards WHERE card_id = $1",
                card_id
            )
            
            if not card_info:
                await ctx.send(f"❌ Card does not exist!")
                return
            
            # Verify card belongs to this server's deck
            if card_info['deck_id'] != deck_id:
                await ctx.send(
                    f"❌ Card **{card_info['name']}** is not part of this server's deck!\n"
                    f"You can only recycle cards from **{deck['name']}** in this server."
                )
                return
            
            # Check how many of this card the user owns at this merge level
            # Fetch extra instances in case some are PvP-locked
            user_instances = await conn.fetch(
                """SELECT instance_id FROM user_cards
                   WHERE user_id = $1 AND card_id = $2 AND merge_level = $3 AND recycled_at IS NULL
                   ORDER BY acquired_at ASC
                   LIMIT $4""",
                user_id, card_id, merge_level, amount + 50
            )

            # Filter out PvP-locked cards
            locked = getattr(self.bot, 'pvp_locked_cards', set())
            unlocked_instances = [i for i in user_instances if str(i['instance_id']) not in locked]

            if len(unlocked_instances) < amount:
                if len(user_instances) >= amount:
                    merge_display = format_merge_level_display(merge_level)
                    card_display = f"{card_info['name']} {merge_display}".strip()
                    await ctx.send(
                        f"❌ Some of your **{card_display}** cards are locked in an active duel and cannot be recycled right now."
                    )
                    return
                user_instances = unlocked_instances

            user_instances = unlocked_instances[:amount]

            if len(user_instances) < amount:
                merge_display = format_merge_level_display(merge_level)
                card_display = f"{card_info['name']} {merge_display}".strip()
                await ctx.send(
                    f"❌ You don't have enough **{card_display}** cards!\n"
                    f"You have: **{len(user_instances)}**, trying to recycle: **{amount}**"
                )
                return
            
            # Calculate credits based on rarity and merge level
            rarity = card_info['rarity']
            base_value = RECYCLE_VALUES.get(rarity, 10)
            
            # Merged cards are worth more: base_value * 1.25^merge_level
            # This matches the merge cost formula, so you get back what it cost to merge
            credit_value = int(base_value * (1.25 ** merge_level))
            total_credits = credit_value * amount
            
            # Use transaction to ensure atomicity
            async with conn.transaction():
                # Mark cards as recycled
                instance_ids = [inst['instance_id'] for inst in user_instances]
                await conn.execute(
                    """UPDATE user_cards
                       SET recycled_at = $1
                       WHERE instance_id = ANY($2)""",
                    datetime.now(timezone.utc),
                    instance_ids
                )
                
                # Credit user (per-deck credits)
                await update_player_credits(conn, user_id, deck_id, total_credits)
        
        # Confirmation
        merge_display = format_merge_level_display(merge_level)
        card_display = f"{card_info['name']} {merge_display}".strip()
        
        embed = discord.Embed(
            title="♻️ Cards Recycled!",
            description=f"Recycled **{amount}x {card_display}** ({rarity})",
            color=discord.Color.gold()
        )
        embed.add_field(name="Credits Earned", value=f"+{total_credits} credits", inline=True)
        embed.add_field(name="Value per Card", value=f"{credit_value} credits", inline=True)
        
        await ctx.send(embed=embed)
    
    @commands.command(name='cardinfo')
    async def card_info(self, ctx, *, search_term: str):
        """
        Show detailed information about a card.
        Usage: /cardinfo [name or ID]
        """
        async with self.db_pool.acquire() as conn:
            # Try to parse as card_id first
            try:
                card_id = int(search_term)
                card = await conn.fetchrow(
                    "SELECT * FROM cards WHERE card_id = $1",
                    card_id
                )
            except ValueError:
                # Search by name (case-insensitive)
                card = await conn.fetchrow(
                    "SELECT * FROM cards WHERE LOWER(name) = LOWER($1)",
                    search_term
                )
        
        if not card:
            await ctx.send(f"❌ No card found with name or ID: `{search_term}`")
            return
        
        # Create embed
        card_dict = dict(card)
        embed = create_card_embed(card_dict)
        
        # Add creation info
        if card_dict.get('created_by'):
            try:
                creator = await self.bot.fetch_user(card_dict['created_by'])
                embed.set_footer(text=f"Created by {creator.name}")
            except:
                pass
        
        await ctx.send(embed=embed)
    
    @commands.command(name='viewdroprates')
    async def view_drop_rates(self, ctx):
        """
        View current drop rates for this server's assigned deck.
        Usage: !viewdroprates
        """
        guild_id = ctx.guild.id if ctx.guild else None
        
        if not guild_id:
            await ctx.send("❌ This command can only be used in a server!")
            return
        
        deck = await self.bot.get_server_deck(guild_id)
        if not deck:
            await ctx.send(
                "❌ No deck assigned to this server!\n"
                "Ask a server manager to assign a deck via the web admin portal."
            )
            return
        
        async with self.db_pool.acquire() as conn:
            drop_rates = await self.get_deck_drop_rates(conn, deck['deck_id'])
            
            rates_rows = await conn.fetch(
                "SELECT COUNT(*) as count FROM rarity_ranges WHERE deck_id = $1",
                deck['deck_id']
            )
            using_defaults = rates_rows[0]['count'] == 0 if rates_rows else True
        
        embed = discord.Embed(
            title="🎲 Drop Rates",
            description=f"Card drop rates for **{deck['name']}**:",
            color=discord.Color.blue()
        )
        
        embed.add_field(
            name="Drop Rates",
            value=format_drop_rates_table(drop_rates),
            inline=False
        )
        
        if using_defaults:
            embed.set_footer(text="Using default rates. Deck creators can customize rates via the web portal.")
        else:
            embed.set_footer(text="Custom rates set by deck creator. Applies to all servers using this deck.")
        
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(CardCommands(bot))
